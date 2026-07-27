"""Tests fuer die FED-Live-Audio-Quelle (Nutzerwunsch: "wenn die FED spricht, besonders
gut aufpassen"). Deckt ab: Zeitfenster-Parsing inkl. Sicherheitsdeckel (siehe auch
app/config.py fuer die reinen Parser-Tests), dass die Quelle NUR innerhalb konfigurierter
Fenster aktiv wird, dass Fehlschlaege ueber note_failure() sichtbar werden (siehe
tests/test_source_failure_visibility.py) und dass Wiederholungen/Stille nicht als
Alerts durchschlagen. ffmpeg/yt-dlp/faster-whisper sind an ihren Grenzen gemockt - kein
Netz, kein echtes Audio noetig.

Sicherheitsaspekt: prueft zusaetzlich, dass der ffmpeg-Aufruf ueber eine Argument-Liste
laeuft (create_subprocess_exec) statt eine interpolierte Shell-Zeile - genau die
Schwachstellen-Klasse (CWE-94), wegen der die fruehere Live-Audio-Quelle entfernt wurde,
siehe app/sources/fed_audio.py Modul-Docstring."""
import asyncio
import datetime
import importlib
import inspect
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _fresh(**env):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    # Explizit (nicht setdefault): os.environ ist prozessweit geteilt - haette ein
    # frueherer Test FED_AUDIO_STREAM_URL schon einmal explizit auf "" gesetzt, wuerde
    # setdefault das fuer alle folgenden Tests dieses Laufs so belassen statt auf den
    # Standard zurueckzusetzen.
    merged = {"FED_AUDIO_STREAM_URL": "https://example.com/live"}
    merged.update(env)
    for k, v in merged.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.sources.fed_audio as fed_audio
    importlib.reload(fed_audio)
    return config, fed_audio


def _windows_around_now(offset_minutes=0, duration=90):
    """Baut FED_MEETING_WINDOWS relativ zu JETZT, damit der Test nicht von einem
    Stichtag in der Vergangenheit/Zukunft abhaengt."""
    start = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=offset_minutes)
    return f"{start.strftime('%Y-%m-%dT%H:%M')}/{duration}"


def test_ausserhalb_fenster_bleibt_stumm():
    """Kein konfiguriertes Fenster trifft JETZT - die Quelle darf ueberhaupt nichts tun
    (kein ffmpeg-Start, kein Netzwerk-Call) - das ist der Normalzustand 99% der Zeit."""
    _, fed_audio = _fresh(FED_MEETING_WINDOWS=_windows_around_now(offset_minutes=+600))
    src = fed_audio.FedAudioSource()

    with patch.object(src, "_dependencies_available", return_value=True), \
         patch("app.sources.fed_audio.asyncio.to_thread", new=AsyncMock(
             side_effect=AssertionError("haette NICHT aufgerufen werden duerfen"))):
        result = asyncio.run(src.poll())
    check("ausserhalb Fenster: leere Liste", result == [])
    check("ausserhalb Fenster: kein Fehlschlag gemeldet", src.last_failure is None)


def test_fehlende_abhaengigkeiten_meldet_kein_fehlschlag():
    """Fehlende ffmpeg/faster-whisper/yt-dlp sind ein KONFIGURATIONSZUSTAND (z.B. altes
    Image, manuelle Installation ohne Docker), kein akuter Ausfall waehrend eines
    laufenden Fensters - soll NICHT wie eine gestoerte Live-Quelle im Live-Signal
    aufschlagen, sondern still inaktiv bleiben (aehnlich ENABLE_FED_AUDIO=false)."""
    _, fed_audio = _fresh(FED_MEETING_WINDOWS=_windows_around_now())
    src = fed_audio.FedAudioSource()
    with patch.object(src, "_dependencies_available", return_value=False):
        result = asyncio.run(src.poll())
    check("fehlende Abhaengigkeiten: leere Liste", result == [])
    check("fehlende Abhaengigkeiten: kein Fehlschlag gemeldet", src.last_failure is None)


def test_leere_stream_url_bleibt_inaktiv():
    _, fed_audio = _fresh(FED_AUDIO_STREAM_URL="", FED_MEETING_WINDOWS=_windows_around_now())
    src = fed_audio.FedAudioSource()
    with patch.object(src, "_dependencies_available", return_value=True):
        result = asyncio.run(src.poll())
    check("leere Stream-URL: leere Liste, kein Crash", result == [])


def test_innerhalb_fenster_transkribiert_und_liefert_statement():
    _, fed_audio = _fresh(FED_MEETING_WINDOWS=_windows_around_now())
    src = fed_audio.FedAudioSource()

    with patch.object(src, "_dependencies_available", return_value=True), \
         patch("app.sources.fed_audio._resolve_stream_media_url",
               return_value="https://cdn.example.com/live.m3u8"), \
         patch.object(src, "_capture_chunk", new=AsyncMock(return_value=True)), \
         patch("app.sources.fed_audio._transcribe",
               return_value="We expect inflation to moderate over the coming quarters."):
        result = asyncio.run(src.poll())

    check("innerhalb Fenster: genau ein Statement", len(result) == 1)
    check("Statement-Text enthaelt die Transkription",
          result and "inflation to moderate" in result[0].text)
    check("Statement ist als FED-Live-Audio gekennzeichnet",
          result and result[0].text.startswith("[FED Live-Audio]"))
    check("Quelle ist korrekt gesetzt", result and result[0].source == "fed_audio")


def test_stille_liefert_kein_statement_und_keinen_fehlschlag():
    """Keine erkennbare Sprache in einem Chunk (z.B. Pause in der Pressekonferenz) ist
    NORMAL, kein Fehler - darf im Live-Signal nicht als gestoerte Quelle erscheinen."""
    _, fed_audio = _fresh(FED_MEETING_WINDOWS=_windows_around_now())
    src = fed_audio.FedAudioSource()

    with patch.object(src, "_dependencies_available", return_value=True), \
         patch("app.sources.fed_audio._resolve_stream_media_url",
               return_value="https://cdn.example.com/live.m3u8"), \
         patch.object(src, "_capture_chunk", new=AsyncMock(return_value=True)), \
         patch("app.sources.fed_audio._transcribe", return_value=""):
        result = asyncio.run(src.poll())
    check("Stille: leere Liste", result == [])
    check("Stille: kein Fehlschlag gemeldet", src.last_failure is None)


def test_wiederholter_text_wird_nicht_doppelt_gemeldet():
    """Ohne diese Sperre koennte ein Whisper-Fuellsatz bei kurzer/unveraenderter Stille
    minuetlich als 'neuer' Alert durchschlagen."""
    _, fed_audio = _fresh(FED_MEETING_WINDOWS=_windows_around_now())
    src = fed_audio.FedAudioSource()

    with patch.object(src, "_dependencies_available", return_value=True), \
         patch("app.sources.fed_audio._resolve_stream_media_url",
               return_value="https://cdn.example.com/live.m3u8"), \
         patch.object(src, "_capture_chunk", new=AsyncMock(return_value=True)), \
         patch("app.sources.fed_audio._transcribe", return_value="Thank you."):
        first = asyncio.run(src.poll())
        second = asyncio.run(src.poll())
    check("erster Aufruf liefert das Statement", len(first) == 1)
    check("identischer Folgetext wird NICHT erneut gemeldet", second == [])


def test_stream_aufloesung_fehlgeschlagen_meldet_fehlschlag():
    _, fed_audio = _fresh(FED_MEETING_WINDOWS=_windows_around_now())
    src = fed_audio.FedAudioSource()
    with patch.object(src, "_dependencies_available", return_value=True), \
         patch("app.sources.fed_audio._resolve_stream_media_url", return_value=None):
        result = asyncio.run(src.poll())
    check("Stream nicht aufloesbar: leere Liste", result == [])
    check("Stream nicht aufloesbar: wird als Fehlschlag gemeldet", src.last_failure is not None)


def test_ffmpeg_mitschnitt_fehlgeschlagen_meldet_fehlschlag():
    _, fed_audio = _fresh(FED_MEETING_WINDOWS=_windows_around_now())
    src = fed_audio.FedAudioSource()
    with patch.object(src, "_dependencies_available", return_value=True), \
         patch("app.sources.fed_audio._resolve_stream_media_url",
               return_value="https://cdn.example.com/live.m3u8"), \
         patch.object(src, "_capture_chunk", new=AsyncMock(return_value=False)):
        result = asyncio.run(src.poll())
    check("ffmpeg fehlgeschlagen: leere Liste", result == [])
    check("ffmpeg fehlgeschlagen: wird als Fehlschlag gemeldet", src.last_failure is not None)


def test_subprocess_nutzt_argument_liste_keine_shell_interpolation():
    """Sicherheits-Regressionstest: der genaue Grund, warum die fruehere Live-Audio-
    Quelle komplett entfernt wurde (CWE-94, siehe Modul-Docstring). create_subprocess_exec
    nimmt eine Argument-Liste entgegen und interpretiert KEINE Shell-Metazeichen - anders
    als create_subprocess_shell oder subprocess.run(..., shell=True)."""
    import app.sources.fed_audio as fed_audio
    src_code = inspect.getsource(fed_audio)
    check("kein shell=True im Modul", "shell=True" not in src_code)
    check("keine create_subprocess_shell-Nutzung", "create_subprocess_shell" not in src_code)
    check("ffmpeg-Aufruf nutzt create_subprocess_exec (Argument-Liste)",
          "create_subprocess_exec" in src_code)


def test_orchestrator_wiring():
    """ENABLE_FED_AUDIO=false (Standard) darf die Quelle gar nicht erst instanziieren -
    kein yt-dlp/faster-whisper-Import-Versuch im Normalbetrieb."""
    _fresh(ENABLE_FED_AUDIO="false")
    import app.orchestrator as orch
    importlib.reload(orch)
    sources = orch.build_sources()
    check("ENABLE_FED_AUDIO=false: keine fed_audio-Quelle in build_sources()",
          not any(s.name == "fed_audio" for s in sources))

    _fresh(ENABLE_FED_AUDIO="true")
    importlib.reload(orch)
    sources = orch.build_sources()
    check("ENABLE_FED_AUDIO=true: fed_audio-Quelle ist dabei",
          any(s.name == "fed_audio" for s in sources))


def main():
    test_ausserhalb_fenster_bleibt_stumm()
    test_fehlende_abhaengigkeiten_meldet_kein_fehlschlag()
    test_leere_stream_url_bleibt_inaktiv()
    test_innerhalb_fenster_transkribiert_und_liefert_statement()
    test_stille_liefert_kein_statement_und_keinen_fehlschlag()
    test_wiederholter_text_wird_nicht_doppelt_gemeldet()
    test_stream_aufloesung_fehlgeschlagen_meldet_fehlschlag()
    test_ffmpeg_mitschnitt_fehlgeschlagen_meldet_fehlschlag()
    test_subprocess_nutzt_argument_liste_keine_shell_interpolation()
    test_orchestrator_wiring()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
