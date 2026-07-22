"""EXPERIMENTELL: Hoert kontinuierlich in manuell konfigurierte Livestream-URLs hinein
(z.B. YouTube-Livestreams von Pressekonferenzen/Reden), transkribiert im Hintergrund und
speist den Text in dieselbe Klassifikations-Pipeline ein.

WICHTIG - funktioniert nur im DAUERBETRIEB (run_forever, siehe README: Docker/systemd/
main.py), NICHT im GitHub-Actions-Cron-Modus (run_once.py): dort beendet sich der
Prozess nach einem einzigen poll()-Aufruf sofort wieder, die Hintergrund-Aufnahme kommt
also nie ueber die ersten paar Sekunden Extraktion hinaus und liefert praktisch nie
Text. run_once.py ruft deshalb aclose() im finally auf, um keine ffmpeg-Prozesse zu
hinterlassen, aber das Feature ist im Cron-Modus im Ergebnis wirkungslos.

Architektur (kontinuierlich statt Snapshot-pro-Poll-Zyklus):
- Pro Stream-URL laeuft EIN Hintergrund-Task, solange der Prozess lebt (nicht nur
  waehrend eines poll()-Aufrufs). ffmpeg liest den Stream DURCHGEHEND und schreibt per
  "segment"-Muxer rollierende WAV-Haeppchen (LIVE_AUDIO_CHUNK_SECONDS lang) ohne Luecken
  dazwischen - im Gegensatz zur fruehen Variante, die pro Poll-Zyklus die Stream-
  Extraktion neu gestartet hat und damit zwischen den Haeppchen Audio verpasste.
- Jedes fertige Haeppchen wird transkribiert (faster-whisper, mit VAD-Filter gegen
  Halluzinationen bei Stille) und in eine In-Memory-Queue gelegt.
- poll() liest nur noch die Queue leer - blockiert nie, liefert einfach, was seit dem
  letzten Aufruf neu transkribiert wurde.
- Stirbt ffmpeg (Stream-Ende, abgelaufene signierte URL o.ae.) oder laeuft ein Segment-
  Fenster ab: Re-Extraktion mit exponentiellem Backoff, damit ein dauerhaft totes
  Stream nicht die CPU verheizt.

Einschraenkungen (weiterhin bewusst, siehe Plan):
- Keine automatische Erkennung "eine relevante Person spricht gerade" - die Stream-URLs
  muessen manuell in LIVE_AUDIO_STREAM_URLS gepflegt werden.
- Braucht zusaetzliche Dependencies (yt-dlp, faster-whisper) und ffmpeg im System
  ("pip install yt-dlp faster-whisper" + ffmpeg-Binary). Bewusst nicht in
  requirements.txt Pflicht, damit der Rest des Systems ohne sie laeuft.
- CPU-Transkription ist langsam; kleineres Whisper-Modell (tiny/base) fuer
  Geschwindigkeit statt Genauigkeit empfohlen (WHISPER_MODEL_SIZE).
"""
import asyncio
import logging
import os
import shutil
import tempfile
import time
from collections import deque
from typing import Optional

from app.db import RawStatement
from app.sources.base import Source
from app.util import text_similarity

logger = logging.getLogger(__name__)

YTDLP_EXTRACT_TIMEOUT_SECONDS = 30
# Ab welcher Textaehnlichkeit (0-1) ein neu transkribiertes Haeppchen als blosse
# Wiederholung des UNMITTELBAR vorherigen gilt und verworfen wird - faengt Whisper-
# Halluzinationen bei Stille/Musik ab (typischerweise wiederholte Standardphrasen).
_REPEAT_SIMILARITY_THRESHOLD = 0.85
# Wie viele der letzten Transkripte pro Stream fuer den Wiederholungs-Check vorgehalten
# werden.
_RECENT_TEXTS_PER_STREAM = 3
# Wie oft (Sekunden) das Ausgabeverzeichnis auf fertige Segment-Dateien geprueft wird.
_WATCH_INTERVAL_SECONDS = 2.0
# Nach so vielen Sekunden wird die Verbindung proaktiv erneuert (frische yt-dlp-
# Extraktion) - signierte Stream-URLs laufen bei manchen Anbietern nach einiger Zeit ab,
# ffmpeg wuerde dann irgendwann mitten im Stream mit einem Fehler abbrechen. Ein
# geplanter Refresh ist sauberer als auf den Fehler zu warten.
_MAX_STREAM_RUNTIME_SECONDS = 3600
_BACKOFF_BASE_SECONDS = 5.0
_BACKOFF_CAP_SECONDS = 300.0


def _safe_remove(path: Optional[str]):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _next_backoff(current: float) -> float:
    """Exponentielles Backoff (5s -> 10 -> 20 -> ... -> gedeckelt bei 300s) fuer
    wiederholte Verbindungsfehler zu einem Stream - verhindert, dass ein dauerhaft
    totes/falsches Stream-URL die CPU mit Dauer-Neuversuchen verheizt."""
    return min(_BACKOFF_CAP_SECONDS, max(_BACKOFF_BASE_SECONDS, current * 2))


def _is_repeat(text: str, recent: list[str], threshold: float = _REPEAT_SIMILARITY_THRESHOLD) -> bool:
    """True, wenn text einem der zuletzt transkribierten Haeppchen desselben Streams
    stark aehnelt - typisches Whisper-Verhalten bei Stille/Musik/Loops (immer dieselbe
    Standardphrase). Leere recent-Liste -> nie ein Wiederholungs-Treffer."""
    return any(text_similarity(text, prev) >= threshold for prev in recent)


class LiveAudioSource(Source):
    name = "live_audio"

    def __init__(
        self,
        stream_urls: list[str],
        chunk_seconds: int = 20,
        model_size: str = "base",
        language: Optional[str] = "en",
    ):
        self.stream_urls = stream_urls
        self.chunk_seconds = max(5, chunk_seconds)
        self.model_size = model_size
        # Leerer String/None -> Whisper-Sprach-Autoerkennung statt fest "en" -
        # relevant, da Reden/Pressekonferenzen nicht zwangslaeufig englisch sind.
        self.language = language or None
        self._model = None
        self._disabled = not stream_urls
        self._deps_ok: Optional[bool] = None
        self._closed = False

        self._tasks: dict[str, asyncio.Task] = {}
        self._queues: dict[str, "asyncio.Queue[RawStatement]"] = {
            url: asyncio.Queue() for url in stream_urls
        }
        self._recent_texts: dict[str, deque] = {
            url: deque(maxlen=_RECENT_TEXTS_PER_STREAM) for url in stream_urls
        }

    def _ensure_deps(self) -> bool:
        if self._deps_ok is not None:
            return self._deps_ok
        try:
            import yt_dlp  # noqa: F401
            from faster_whisper import WhisperModel  # noqa: F401
            self._deps_ok = True
        except ImportError:
            logger.warning(
                "Live-Audio-Dependencies fehlen ('pip install yt-dlp faster-whisper' "
                "sowie ffmpeg im PATH) - Quelle liefert keine Ergebnisse.",
                exc_info=True,
            )
            self._deps_ok = False
        return self._deps_ok

    def _ensure_capture_task(self, stream_url: str) -> None:
        """Startet (falls noch nicht laufend oder zuvor abgestuerzt) den Dauer-
        Hintergrund-Task fuer diesen Stream. Idempotent - sicher bei jedem poll()
        aufzurufen."""
        if self._closed:
            return
        task = self._tasks.get(stream_url)
        if task is not None and not task.done():
            return
        if task is not None and task.done():
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                logger.error(
                    "[%s] Hintergrund-Aufnahme unerwartet beendet, starte neu: %s",
                    stream_url, exc,
                )
        self._tasks[stream_url] = asyncio.create_task(self._capture_loop(stream_url))

    async def poll(self) -> list[RawStatement]:
        if self._disabled or self._closed:
            return []
        if not self._ensure_deps():
            return []

        for stream_url in self.stream_urls:
            self._ensure_capture_task(stream_url)

        results: list[RawStatement] = []
        for stream_url in self.stream_urls:
            queue = self._queues[stream_url]
            while not queue.empty():
                results.append(queue.get_nowait())
        return results

    async def aclose(self) -> None:
        """Beendet alle Hintergrund-Aufnahmen und deren ffmpeg-Kindprozesse sauber -
        MUSS im Cron-/Einzellauf-Modus (run_once.py) aufgerufen werden, sonst bleiben
        ffmpeg-Prozesse nach Prozessende verwaist."""
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _capture_loop(self, stream_url: str) -> None:
        """Laeuft (bis abgebrochen) fuer die gesamte Prozesslaufzeit: extrahiert die
        direkte Audio-URL, laesst ffmpeg kontinuierlich rollierende Haeppchen
        schreiben, transkribiert jedes fertige Haeppchen und legt das Ergebnis in die
        Queue. Bei Fehlern/Stream-Ende: Backoff, dann von vorn."""
        backoff = 0.0
        recent = self._recent_texts[stream_url]
        while not self._closed:
            tmp_dir = tempfile.mkdtemp(prefix="live_audio_")
            proc = None
            planned_refresh = False
            try:
                audio_url = await self._extract_audio_url(stream_url)
                if audio_url is None:
                    raise RuntimeError("yt-dlp lieferte keine Audio-URL")

                proc = await self._start_ffmpeg_segmenter(audio_url, tmp_dir)
                backoff = 0.0  # erfolgreich verbunden - Backoff zuruecksetzen
                start_ts = time.monotonic()
                last_processed_index = -1

                while True:
                    if self._closed:
                        break
                    if proc.returncode is not None:
                        break  # ffmpeg ist beendet (Fehler oder Stream-Ende)
                    if time.monotonic() - start_ts >= _MAX_STREAM_RUNTIME_SECONDS:
                        planned_refresh = True
                        break  # geplanter Refresh, keine Stoerung
                    last_processed_index = await self._process_ready_segments(
                        tmp_dir, last_processed_index, stream_url, recent
                    )
                    await asyncio.sleep(_WATCH_INTERVAL_SECONDS)

                # Sauber beenden + letzte(s) verbliebene(s) Segment(e) noch verarbeiten
                # (nach Prozessende schreibt niemand mehr an der letzten Datei weiter).
                await self._terminate(proc)
                await self._process_ready_segments(
                    tmp_dir, last_processed_index, stream_url, recent, final=True
                )
            except asyncio.CancelledError:
                if proc is not None:
                    await self._terminate(proc)
                raise
            except Exception:
                logger.warning(
                    "[%s] Live-Audio-Aufnahme fehlgeschlagen, versuche erneut.",
                    stream_url, exc_info=True,
                )
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            if self._closed:
                return
            if planned_refresh:
                continue  # sofort neu verbinden, kein Backoff (kein Fehler)
            backoff = _next_backoff(backoff)
            logger.info("[%s] Naechster Verbindungsversuch in %.0fs.", stream_url, backoff)
            await asyncio.sleep(backoff)

    async def _extract_audio_url(self, stream_url: str) -> Optional[str]:
        import yt_dlp

        loop = asyncio.get_running_loop()

        def _extract() -> Optional[str]:
            ydl_opts = {"quiet": True, "format": "bestaudio/best", "noplaylist": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(stream_url, download=False)
                return info.get("url")

        try:
            # Timeout schuetzt davor, dass ein haengender yt-dlp-Aufruf (totes
            # Stream/Netzwerk-Stall) diesen Verbindungsversuch unbegrenzt blockiert.
            return await asyncio.wait_for(
                loop.run_in_executor(None, _extract), timeout=YTDLP_EXTRACT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] yt-dlp-Extraktion hat zu lange gedauert (>%ss).",
                stream_url, YTDLP_EXTRACT_TIMEOUT_SECONDS,
            )
            return None

    async def _start_ffmpeg_segmenter(self, audio_url: str, tmp_dir: str):
        """Startet einen LANGLEBIGEN ffmpeg-Prozess, der den Stream durchgehend liest
        und per 'segment'-Muxer rollierende WAV-Haeppchen ohne Luecken dazwischen
        schreibt (statt wie zuvor pro Haeppchen eine neue, kurze Verbindung mit fixer
        Laenge zu oeffnen - das verpasste systematisch die Zeit zwischen den Haeppchen)."""
        out_pattern = os.path.join(tmp_dir, "chunk_%09d.wav")
        cmd = [
            "ffmpeg", "-y", "-i", audio_url,
            "-vn", "-ar", "16000", "-ac", "1",
            "-f", "segment", "-segment_time", str(self.chunk_seconds),
            "-reset_timestamps", "1",
            out_pattern,
        ]
        try:
            return await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffmpeg ist nicht im PATH gefunden - fuer Live-Audio installieren."
            ) from exc

    async def _terminate(self, proc) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        except Exception:
            logger.debug("ffmpeg-Terminate-Fehler (ignoriert).", exc_info=True)

    async def _process_ready_segments(
        self, tmp_dir: str, last_processed_index: int, stream_url: str,
        recent: deque, final: bool = False,
    ) -> int:
        """Verarbeitet alle Segment-Dateien, die ffmpeg mit Sicherheit fertig
        geschrieben hat: normalerweise alle bis auf die zuletzt begonnene (an der
        ffmpeg vermutlich noch schreibt) - AUSSER final=True (ffmpeg ist bereits
        beendet, dann ist auch die letzte Datei fertig). Gibt den hoechsten
        verarbeiteten Index zurueck."""
        try:
            names = sorted(f for f in os.listdir(tmp_dir) if f.startswith("chunk_"))
        except OSError:
            return last_processed_index

        if not names:
            return last_processed_index
        ready = names if final else names[:-1]

        loop = asyncio.get_running_loop()
        new_last = last_processed_index
        for name in ready:
            try:
                idx = int(name[len("chunk_"):-len(".wav")])
            except ValueError:
                idx = new_last + 1
            path = os.path.join(tmp_dir, name)
            try:
                text = await loop.run_in_executor(None, self._transcribe, path)
            except Exception:
                logger.warning(
                    "[%s] Transkription fehlgeschlagen fuer Haeppchen, ueberspringe.",
                    stream_url, exc_info=True,
                )
                text = ""
            finally:
                _safe_remove(path)
            new_last = max(new_last, idx)

            text = text.strip()
            if not text or _is_repeat(text, list(recent)):
                continue
            recent.append(text)
            self._queues[stream_url].put_nowait(
                RawStatement(
                    source=self.name,
                    source_id=f"{stream_url}:{int(time.time() * 1000)}:{idx}",
                    text=text,
                    url=stream_url,
                    published_at=time.time(),
                )
            )
        return new_last

    def _transcribe(self, path: str) -> str:
        from faster_whisper import WhisperModel

        if self._model is None:
            self._model = WhisperModel(self.model_size, compute_type="int8")
        # vad_filter=True ueberspringt Stille/Musik ohne Sprache - reduziert sowohl
        # unnoetige Transkriptions-Kosten als auch Whisper-typische Halluzinationen
        # (wiederholte Standardphrasen) bei laengeren stillen Passagen.
        segments, _ = self._model.transcribe(path, language=self.language, vad_filter=True)
        return " ".join(seg.text for seg in segments)
