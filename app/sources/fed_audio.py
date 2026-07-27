"""FED-Live-Audio: hoert waehrend konfigurierter Zeitfenster (FED_MEETING_WINDOWS,
i.d.R. FOMC-Pressekonferenzen) einen Livestream mit, transkribiert per Whisper und
speist den Text in dieselbe Klassifikations-Pipeline wie jede andere Quelle ein.

Sicherheitshinweis: Eine frueherer Live-Audio-Quelle fuer beliebige Streams wurde
komplett entfernt, weil der transkribierte Text direkt in ein GitHub-Actions-Skript
interpoliert wurde (Script-Injection, CWE-94). Diese Quelle hat dieselbe
Schwachstellen-Klasse strukturell nicht: sie laeuft in-process im Dauerbetrieb (kein
Workflow-Trigger), der transkribierte Text landet nur als RawStatement.text in der DB
und durchlaeuft die normale Claude-Klassifikation - keine Shell-/Workflow-Interpolation
an irgendeiner Stelle. Alle Subprozess-Aufrufe hier nutzen Argument-Listen
(create_subprocess_exec), nie eine interpolierte Shell-Zeile.

Bewusst simples Design (Nutzerwunsch: "vorsichtig auf aktueller VM" mit nur 1GB RAM):
pro Poll-Zyklus wird ein einzelner, zeitlich hart begrenzter ffmpeg-Mitschnitt
gestartet und sofort transkribiert - kein dauerhaft laufender Hintergrundprozess, kein
Zustand, der ueber einen Neustart hinweg ueberlebt oder aufgeraeumt werden muesste.
Trade-off: zwischen zwei Mitschnitten (Chunk-Dauer < Poll-Intervall) entsteht eine
kurze Luecke, in der gesprochene Woerter verloren gehen koennen - fuer den Zweck hier
(grobe inhaltliche Fruehwarnung, keine woertliche Abschrift) akzeptabel.
"""
import asyncio
import datetime
import logging
import shutil
import tempfile
import time
from pathlib import Path

from app.config import (
    FED_AUDIO_CHUNK_SECONDS,
    FED_AUDIO_MAX_WINDOW_MINUTES,
    FED_AUDIO_STREAM_URL,
    FED_AUDIO_WHISPER_MODEL,
    FED_MEETING_WINDOWS,
    _parse_fed_meeting_windows,
)
from app.db import RawStatement
from app.sources.base import Source

logger = logging.getLogger(__name__)

# Modell wird lazy und genau EINMAL pro Prozess geladen (~75MB Download beim allerersten
# Fenster), nicht pro poll() - ein Neuladen bei jedem Zyklus waere unnoetige CPU-/
# Netzlast genau dann, wenn die Quelle am dringendsten gebraucht wird.
_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        # int8 + cpu: kleinster Speicher-Fussabdruck, siehe FED_AUDIO_WHISPER_MODEL in
        # app/config.py zur Modellgroessen-Begruendung (1GB-Referenz-VM).
        _whisper_model = WhisperModel(FED_AUDIO_WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper_model


def _resolve_stream_media_url(page_url: str) -> str | None:
    """Loest eine YouTube-/Livestream-Seiten-URL in eine direkte, von ffmpeg lesbare
    Medien-URL auf. Synchron/blockierend (Netzwerk-Call) - MUSS ueber asyncio.to_thread
    aufgerufen werden, sonst blockiert das den gesamten Event-Loop."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best", "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(page_url, download=False)
        return info.get("url") if info else None


def _transcribe(wav_path: str) -> str:
    """Blockierend/CPU-gebunden - MUSS ueber asyncio.to_thread aufgerufen werden."""
    model = _get_whisper_model()
    segments, _info = model.transcribe(wav_path, language="en", vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


class FedAudioSource(Source):
    name = "fed_audio"

    def __init__(self):
        self._missing_deps_warned = False
        self._last_emitted_text = None

    def _dependencies_available(self) -> bool:
        if shutil.which("ffmpeg") is None:
            if not self._missing_deps_warned:
                logger.warning(
                    "ffmpeg nicht installiert - FED-Live-Audio-Quelle deaktiviert. "
                    "Im mitgelieferten Dockerfile bereits enthalten; bei manueller "
                    "Installation 'apt-get install ffmpeg'."
                )
                self._missing_deps_warned = True
            return False
        try:
            import faster_whisper  # noqa: F401
            import yt_dlp  # noqa: F401
        except ImportError:
            if not self._missing_deps_warned:
                logger.warning(
                    "faster-whisper/yt-dlp nicht installiert - FED-Live-Audio-Quelle "
                    "deaktiviert. 'pip install faster-whisper yt-dlp'."
                )
                self._missing_deps_warned = True
            return False
        return True

    def _active_window(self, now: datetime.datetime) -> tuple | None:
        for start, end in _parse_fed_meeting_windows(FED_MEETING_WINDOWS):
            if start <= now <= end:
                return (start, end)
        return None

    async def _capture_chunk(self, media_url: str, wav_path: str) -> bool:
        """Schneidet FED_AUDIO_CHUNK_SECONDS Sekunden Live-Audio mit, mit einem harten
        Timeout als Sicherheitsnetz (ein haengender Stream darf den Poll-Zyklus nicht
        auf unbestimmte Zeit blockieren). Liste-Argumente statt Shell-String - siehe
        Modul-Docstring zur Sicherheitsbegruendung."""
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", media_url,
            "-t", str(FED_AUDIO_CHUNK_SECONDS),
            "-ar", "16000", "-ac", "1", "-f", "wav",
            wav_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=FED_AUDIO_CHUNK_SECONDS + 20
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("[fed_audio] ffmpeg-Mitschnitt haengt - abgebrochen.")
            return False
        if proc.returncode != 0:
            logger.warning(
                "[fed_audio] ffmpeg-Mitschnitt fehlgeschlagen (Code %s): %s",
                proc.returncode, (stderr or b"").decode(errors="replace")[:300],
            )
            return False
        return True

    async def poll(self) -> list[RawStatement]:
        if not self._dependencies_available():
            return []
        if not FED_AUDIO_STREAM_URL:
            return []

        now = datetime.datetime.now(datetime.timezone.utc)
        window = self._active_window(now)
        if window is None:
            return []

        try:
            media_url = await asyncio.to_thread(_resolve_stream_media_url, FED_AUDIO_STREAM_URL)
        except Exception as exc:
            logger.warning("[fed_audio] Stream-URL konnte nicht aufgeloest werden.", exc_info=True)
            self.note_failure(exc)
            return []
        if not media_url:
            self.note_failure(RuntimeError("Stream-URL lieferte keine abspielbare Medien-URL"))
            return []

        with tempfile.TemporaryDirectory(prefix="fed_audio_") as tmpdir:
            wav_path = str(Path(tmpdir) / "chunk.wav")
            try:
                ok = await self._capture_chunk(media_url, wav_path)
                if not ok:
                    self.note_failure(RuntimeError("ffmpeg-Mitschnitt fehlgeschlagen"))
                    return []
                text = await asyncio.to_thread(_transcribe, wav_path)
            except Exception as exc:
                logger.warning("[fed_audio] Mitschnitt/Transkription fehlgeschlagen.", exc_info=True)
                self.note_failure(exc)
                return []

        if not text:
            # Stille/keine erkennbare Sprache in diesem Chunk - kein Fehler, die
            # Pressekonferenz hat z.B. noch nicht begonnen oder es ist gerade eine Pause.
            return []
        if text == self._last_emitted_text:
            # Whisper kann bei sehr kurzer/unveraenderter Stille denselben Fuellsatz
            # wiederholen - ohne diese Sperre gaebe es identische Alerts im Minutentakt.
            return []
        self._last_emitted_text = text

        return [
            RawStatement(
                source=self.name,
                source_id=f"fed_audio:{int(time.time())}",
                text=f"[FED Live-Audio] {text}",
                published_at=time.time(),
            )
        ]
