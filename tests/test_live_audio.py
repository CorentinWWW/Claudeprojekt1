"""Tests fuer app/sources/live_audio.py - die reinen Bausteine (Wiederholungs-Dedup,
Backoff, Queue-Drain, Segment-Verarbeitung, Task-Lifecycle) OHNE echte yt-dlp/ffmpeg/
faster-whisper-Abhaengigkeiten (die sind im CI bewusst nicht installiert, siehe
requirements.txt). Die eigentliche Stream-Extraktion/Transkription bleibt daher
Integrationscode - konsistent mit dem Rest des Projekts wird hier nur getestet, was
ohne die schweren optionalen Deps testbar ist.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def test_backoff_sequence():
    from app.sources.live_audio import _next_backoff

    seq = []
    b = 0.0
    for _ in range(8):
        b = _next_backoff(b)
        seq.append(b)
    check("erster Backoff = Basis (5s)", seq[0] == 5.0)
    check("Backoff verdoppelt sich", seq[1] == 10.0 and seq[2] == 20.0 and seq[3] == 40.0)
    check("Backoff ist gedeckelt (300s)", seq[-1] == 300.0)
    check("Backoff faellt nie unter die Basis", all(v >= 5.0 for v in seq))


def test_is_repeat():
    from app.sources.live_audio import _is_repeat

    check("identischer Text -> Wiederholung", _is_repeat("hello world", ["hello world"]))
    check(
        "sehr aehnlicher Text -> Wiederholung",
        _is_repeat("Thank you for watching", ["Thank you for watching!"]),
    )
    check(
        "voellig anderer Text -> keine Wiederholung",
        not _is_repeat("Fed cuts interest rates today", ["Thank you for watching"]),
    )
    check("leere recent-Liste -> nie Wiederholung", not _is_repeat("irgendein Text", []))


def test_disabled_without_stream_urls():
    from app.sources.live_audio import LiveAudioSource

    src = LiveAudioSource([])
    check("leere Stream-Liste -> disabled", src._disabled is True)

    def _boom():
        raise AssertionError("_ensure_deps haette bei disabled=True nie aufgerufen werden duerfen")
    src._ensure_deps = _boom

    result = asyncio.run(src.poll())
    check("poll() liefert bei disabled sofort [] (ohne Deps-Check)", result == [])


def test_poll_returns_empty_without_deps():
    from app.sources.live_audio import LiveAudioSource

    src = LiveAudioSource(["http://example.com/stream"])
    src._deps_ok = False  # simuliert fehlende yt-dlp/faster-whisper Installation

    def _boom(_url):
        raise AssertionError("Ohne Deps darf kein Capture-Task gestartet werden")
    src._ensure_capture_task = _boom

    result = asyncio.run(src.poll())
    check("poll() liefert [] wenn Deps fehlen (kein Task-Start)", result == [])


def test_poll_drains_queue():
    from app.db import RawStatement
    from app.sources.live_audio import LiveAudioSource

    url = "http://example.com/stream"
    src = LiveAudioSource([url])
    src._deps_ok = True  # Deps simuliert vorhanden, ohne yt-dlp installieren zu muessen

    started = {"n": 0}

    def fake_ensure(u):
        started["n"] += 1
    src._ensure_capture_task = fake_ensure

    stmt = RawStatement(source="live_audio", source_id=f"{url}:1", text="Fed cuts rates",
                        url=url, published_at=time.time())
    src._queues[url].put_nowait(stmt)

    result = asyncio.run(src.poll())
    check("poll() liefert das in der Queue liegende Statement", result == [stmt])
    check("poll() versucht den Capture-Task sicherzustellen", started["n"] == 1)

    result2 = asyncio.run(src.poll())
    check("zweiter poll()-Aufruf liefert nichts Neues (Queue leer)", result2 == [])


async def _aclose_scenario():
    from app.sources.live_audio import LiveAudioSource

    url = "http://example.com/stream"
    src = LiveAudioSource([url])

    async def never_ending():
        await asyncio.sleep(3600)

    src._tasks[url] = asyncio.create_task(never_ending())
    await asyncio.sleep(0)  # Task tatsaechlich anlaufen lassen

    await src.aclose()
    check("aclose() bricht den laufenden Hintergrund-Task ab", src._tasks[url].cancelled())
    check("aclose() setzt das closed-Flag", src._closed is True)

    src._deps_ok = True

    def _boom(_url):
        raise AssertionError("Nach aclose() darf kein neuer Task gestartet werden")
    src._ensure_capture_task = _boom

    result = await src.poll()
    check("poll() liefert nach aclose() [] und startet nichts neu", result == [])


def test_aclose_cancels_background_tasks():
    asyncio.run(_aclose_scenario())


async def _restart_scenario():
    from app.sources.live_audio import LiveAudioSource

    url = "http://example.com/stream"
    src = LiveAudioSource([url])

    async def quick():
        return None

    old_task = asyncio.create_task(quick())
    await old_task  # laesst den Task VOR dem Ensure-Aufruf fertig werden (simuliert Absturz)

    async def fake_capture_loop(_url):
        await asyncio.sleep(3600)
    src._capture_loop = fake_capture_loop

    src._tasks[url] = old_task
    src._ensure_capture_task(url)
    new_task = src._tasks[url]
    check("abgeschlossener Task wird durch einen neuen ersetzt", new_task is not old_task)

    # Ein NOCH LAUFENDER Task darf dagegen nicht ersetzt werden.
    src._ensure_capture_task(url)
    check("laufender Task wird NICHT ersetzt", src._tasks[url] is new_task)

    new_task.cancel()
    try:
        await new_task
    except asyncio.CancelledError:
        pass


def test_ensure_capture_task_restart_behavior():
    asyncio.run(_restart_scenario())


async def _process_segments_scenario():
    from app.sources.live_audio import LiveAudioSource

    url = "http://example.com/stream"
    src = LiveAudioSource([url])
    tmp_dir = tempfile.mkdtemp(prefix="test_live_audio_")
    try:
        names = [f"chunk_{i:09d}.wav" for i in range(3)]
        for n in names:
            open(os.path.join(tmp_dir, n), "wb").close()

        texts = {
            os.path.join(tmp_dir, names[0]): "Fed cuts interest rates today",
            os.path.join(tmp_dir, names[1]): "Fed cuts interest rates today.",  # Wiederholung
            os.path.join(tmp_dir, names[2]): "New tariffs announced on steel imports",
        }
        src._transcribe = lambda path: texts.get(path, "")
        recent = src._recent_texts[url]

        last_idx = await src._process_ready_segments(tmp_dir, -1, url, recent, final=False)
        check(
            "nicht-final: letztes (evtl. noch offenes) Haeppchen bleibt unverarbeitet "
            "(hoechster verarbeiteter Index = 1, nicht 2)",
            last_idx == 1,
        )
        queued = []
        while not src._queues[url].empty():
            queued.append(src._queues[url].get_nowait())
        check("nur EIN Statement in der Queue (Wiederholung von Haeppchen 2 verworfen)",
              len(queued) == 1)
        check("Text stammt vom ersten Haeppchen", queued[0].text == "Fed cuts interest rates today")
        check("verarbeitete Datei wurde geloescht",
              not os.path.exists(os.path.join(tmp_dir, names[0])))
        check("noch offene (letzte) Datei bleibt liegen",
              os.path.exists(os.path.join(tmp_dir, names[2])))

        last_idx2 = await src._process_ready_segments(tmp_dir, last_idx, url, recent, final=True)
        check("final=True verarbeitet auch das letzte Haeppchen", last_idx2 == 2)
        queued2 = []
        while not src._queues[url].empty():
            queued2.append(src._queues[url].get_nowait())
        check("neuer, unterschiedlicher Text wird NICHT als Wiederholung verworfen",
              len(queued2) == 1)
        check("Text stammt vom dritten Haeppchen",
              queued2[0].text == "New tariffs announced on steel imports")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_process_ready_segments():
    asyncio.run(_process_segments_scenario())


def main():
    test_backoff_sequence()
    test_is_repeat()
    test_disabled_without_stream_urls()
    test_poll_returns_empty_without_deps()
    test_poll_drains_queue()
    test_aclose_cancels_background_tasks()
    test_ensure_capture_task_restart_behavior()
    test_process_ready_segments()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
