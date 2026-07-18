"""EXPERIMENTELL: Transkribiert Audio aus manuell konfigurierten Livestream-URLs
(z.B. YouTube-Livestreams von Pressekonferenzen/Reden) und speist den Text in
dieselbe Klassifikations-Pipeline ein.

Einschraenkungen (bewusst, siehe Plan):
- Es gibt keine automatische Erkennung "eine relevante Person spricht gerade" - die
  Stream-URLs muessen manuell in LIVE_AUDIO_STREAM_URLS gepflegt werden (z.B. ein
  24/7 News-Kanal oder der Link zu einem konkret angekuendigten Event/einer
  Pressekonferenz).
- Braucht zusaetzliche Dependencies (yt-dlp, faster-whisper) und ffmpeg im System
  ("pip install yt-dlp faster-whisper" + ffmpeg-Binary). Diese sind bewusst nicht
  in requirements.txt Pflicht, damit der Rest des Systems ohne sie laeuft.
- Transkription in Chunks (Standard: 30s) statt echtem Streaming -> Latenz von
  bis zu einer Chunk-Laenge, plus Rechenzeit fuer die Transkription selbst.
- CPU-Transkription ist langsam; kleineres Whisper-Modell (tiny/base) fuer
  Geschwindigkeit statt Genauigkeit empfohlen.
"""
import asyncio
import logging
import os
import tempfile
import time

from app.db import RawStatement
from app.sources.base import Source

logger = logging.getLogger(__name__)

YTDLP_EXTRACT_TIMEOUT_SECONDS = 30


def _safe_remove(path: str | None):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


class LiveAudioSource(Source):
    name = "live_audio"

    def __init__(self, stream_urls: list[str], chunk_seconds: int = 30, model_size: str = "base"):
        self.stream_urls = stream_urls
        self.chunk_seconds = chunk_seconds
        self.model_size = model_size
        self._model = None
        self._disabled = not stream_urls

    def _ensure_deps(self):
        try:
            import yt_dlp  # noqa: F401
            from faster_whisper import WhisperModel  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Live-Audio benoetigt 'pip install yt-dlp faster-whisper' sowie "
                "ffmpeg im System-PATH."
            ) from exc

    async def poll(self) -> list[RawStatement]:
        if self._disabled or not self.stream_urls:
            return []

        try:
            self._ensure_deps()
        except RuntimeError:
            logger.warning(
                "Live-Audio-Dependencies fehlen, Quelle wird deaktiviert.",
                exc_info=True,
            )
            self._disabled = True
            return []

        results: list[RawStatement] = []
        for stream_url in self.stream_urls:
            chunk_path = None
            try:
                chunk_path = await self._capture_chunk(stream_url)
                if chunk_path is None:
                    continue
                text = await asyncio.get_running_loop().run_in_executor(
                    None, self._transcribe, chunk_path
                )
                text = text.strip()
                if not text:
                    continue
                source_id = f"{stream_url}:{int(time.time())}"
                results.append(
                    RawStatement(
                        source=self.name,
                        source_id=source_id,
                        text=text,
                        url=stream_url,
                        published_at=time.time(),
                    )
                )
            except Exception:
                logger.warning(
                    "Live-Audio-Capture/Transkription fehlgeschlagen fuer %s",
                    stream_url,
                    exc_info=True,
                )
            finally:
                # Immer aufraeumen, auch wenn die Transkription fehlschlaegt -
                # sonst sammeln sich bei wiederholten Fehlern Temp-Dateien an.
                _safe_remove(chunk_path)
        return results

    async def _capture_chunk(self, stream_url: str) -> str | None:
        import yt_dlp

        loop = asyncio.get_running_loop()

        def _extract_audio_url() -> str | None:
            ydl_opts = {"quiet": True, "format": "bestaudio/best", "noplaylist": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(stream_url, download=False)
                return info.get("url")

        try:
            # Timeout schuetzt davor, dass ein haengender yt-dlp-Aufruf (totes
            # Stream/Netzwerk-Stall) diesen Poll-Zyklus unbegrenzt blockiert. Der
            # zugrundeliegende Thread selbst laesst sich nicht abbrechen (Python-
            # Limitation), laeuft im Hintergrund aber irgendwann von selbst aus.
            audio_url = await asyncio.wait_for(
                loop.run_in_executor(None, _extract_audio_url),
                timeout=YTDLP_EXTRACT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "yt-dlp-Extraktion fuer %s hat zu lange gedauert (>%ss), ueberspringe.",
                stream_url,
                YTDLP_EXTRACT_TIMEOUT_SECONDS,
            )
            return None
        if not audio_url:
            return None

        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        cmd = [
            "ffmpeg", "-y", "-i", audio_url,
            "-t", str(self.chunk_seconds),
            "-ar", "16000", "-ac", "1",
            tmp_path,
        ]
        try:
            # create_subprocess_exec selbst (nicht nur proc.wait()) kann werfen, z.B.
            # FileNotFoundError falls ffmpeg nicht im PATH ist - dann existiert
            # tmp_path bereits auf der Platte, aber "proc" wuerde nie zugewiesen.
            # Ohne dieses try/except wuerde die Exception hier direkt raus- und in
            # poll() (das chunk_path als lokale Variable nie zugewiesen bekaeme, da
            # der await nie zurueckkehrt) an _safe_remove(None) vorbeipropagieren -
            # ein Leak leerer .wav-Dateien bei jedem Poll-Zyklus ohne ffmpeg.
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.chunk_seconds + 30)
            except asyncio.TimeoutError:
                proc.kill()
                _safe_remove(tmp_path)
                return None

            if proc.returncode != 0 or not os.path.exists(tmp_path):
                _safe_remove(tmp_path)
                return None
            return tmp_path
        except Exception:
            _safe_remove(tmp_path)
            raise

    def _transcribe(self, path: str) -> str:
        from faster_whisper import WhisperModel

        if self._model is None:
            self._model = WhisperModel(self.model_size, compute_type="int8")
        segments, _ = self._model.transcribe(path, language="en")
        return " ".join(seg.text for seg in segments)
