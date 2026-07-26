"""Tests dafuer, dass eine kaputte Quelle als kaputt SICHTBAR wird.

Hintergrund (live gefunden): alle drei Quellen fangen ihre Netzwerk-/Parse-Fehler
selbst ab und liefern dann eine leere Liste - bewusst, damit ein einzelner kaputter
Feed nicht den ganzen Poll-Zyklus mitreisst. Der Orchestrator setzte
source_health["last_error"] aber nur, wenn eine Ausnahme bis zu ihm durchdrang.
Dadurch war eine DAUERHAFT tote Quelle von "gerade keine passenden Meldungen" nicht
zu unterscheiden: last_error blieb null, total_fetched 0, und Dashboard wie
taegliches Live-Signal zeigten unveraendert ein gruenes Haekchen.

Kein Netz (HTTP-Aufrufe gemockt)."""
import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path

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
    for k, v in env.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    return config, db


def test_gdelt_meldet_fehlschlag():
    _fresh()
    import app.sources.news_gdelt as g
    importlib.reload(g)
    import httpx

    src = g.GdeltNewsSource()

    class BoomClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw):
            raise httpx.ConnectError("Verbindung abgelehnt")

    g.httpx.AsyncClient = BoomClient
    out = asyncio.run(src.poll())
    check("GDELT-Ausfall: liefert leere Liste (Zyklus laeuft weiter)", out == [])
    check("GDELT-Ausfall: wird als Fehlschlag gemeldet", src.last_failure is not None)
    check("GDELT-Ausfall: Grund nennt den Fehlertyp",
          src.last_failure and "ConnectError" in src.last_failure)


def test_truth_social_ohne_token_und_ohne_fallback():
    """Genau die Konfiguration der Produktions-VM: kein Bearer-Token, Browser-Fallback
    wegen 1 GB RAM abgeschaltet. Diese Quelle liefert dauerhaft nichts - das muss
    sichtbar sein statt wie ein ruhiger Tag auszusehen."""
    _fresh(TRUTH_SOCIAL_BROWSER_FALLBACK="false", TRUTH_SOCIAL_BEARER_TOKEN="")
    import app.sources.truth_social as ts
    importlib.reload(ts)

    src = ts.TruthSocialSource()

    async def fail_direct():
        return None
    src._fetch_via_direct_api = fail_direct

    out = asyncio.run(src.poll())
    check("Truth Social tot: liefert leere Liste", out == [])
    check("Truth Social tot: wird als Fehlschlag gemeldet", src.last_failure is not None)
    check("Truth Social tot: Grund nennt die Ursache",
          src.last_failure and "BROWSER_FALLBACK" in src.last_failure)


def test_leere_antwort_ist_KEIN_fehlschlag():
    """Gegenprobe - sonst waere die Meldung wertlos: eine erreichbare Quelle ohne neue
    Posts darf NICHT als gestoert gelten."""
    _fresh(TRUTH_SOCIAL_BROWSER_FALLBACK="false")
    import app.sources.truth_social as ts
    importlib.reload(ts)

    src = ts.TruthSocialSource()

    async def empty_ok():
        return []
    src._fetch_via_direct_api = empty_ok

    out = asyncio.run(src.poll())
    check("keine neuen Posts: liefert leere Liste", out == [])
    check("keine neuen Posts: KEIN Fehlschlag gemeldet", src.last_failure is None)


def test_rss_einzelner_feed_kaputt_ist_kein_quellenausfall():
    """Bei sieben Feeds ist ein einzelner Ausfall Normalbetrieb - erst wenn KEIN Feed
    mehr durchkommt, gilt die Quelle als gestoert."""
    _fresh()
    import app.sources.news_rss as r
    importlib.reload(r)

    src = r.RssNewsSource()
    calls = {"n": 0}

    async def one_bad(feed_url, client):
        calls["n"] += 1
        return None if calls["n"] == 1 else []

    src._fetch_feed = one_bad
    asyncio.run(src.poll())
    check("ein kaputter Feed von vielen: KEIN Quellen-Fehlschlag", src.last_failure is None)

    src2 = r.RssNewsSource()

    async def all_bad(feed_url, client):
        return None

    src2._fetch_feed = all_bad
    asyncio.run(src2.poll())
    check("ALLE Feeds kaputt: Quellen-Fehlschlag gemeldet", src2.last_failure is not None)


def test_orchestrator_uebernimmt_den_fehlschlag():
    """Der eigentliche Zweck: der gemeldete Fehlschlag muss in source_health landen,
    denn daraus speisen sich Dashboard-Punkt und taegliches Live-Signal."""
    _fresh()
    import app.orchestrator as orch
    importlib.reload(orch)
    from app.sources.base import Source

    class DeadSource(Source):
        name = "kaputte_quelle"

        async def poll(self):
            self.note_failure(RuntimeError("Endpoint weg"))
            return []

    class HealthySource(Source):
        name = "gesunde_quelle"

        async def poll(self):
            return []

    dead, alive = DeadSource(), HealthySource()
    orch.source_health.clear()
    for s in (dead, alive):
        orch.source_health[s.name] = {
            "last_poll_at": None, "last_success_at": None, "last_error": None,
            "last_error_at": None, "total_fetched": 0, "prefiltered": 0, "stale": 0,
        }

    asyncio.run(orch.poll_once([dead, alive], asyncio.Semaphore(1)))

    dh = orch.source_health["kaputte_quelle"]
    ah = orch.source_health["gesunde_quelle"]
    check("kaputte Quelle: last_error in source_health gesetzt", dh["last_error"] is not None)
    check("kaputte Quelle: last_error_at gesetzt", dh["last_error_at"] is not None)
    check("kaputte Quelle: KEIN last_success_at", dh["last_success_at"] is None)
    check("gesunde Quelle: last_success_at gesetzt", ah["last_success_at"] is not None)
    check("gesunde Quelle: kein last_error", ah["last_error"] is None)

    # Und damit zeigt das Live-Signal die Stoerung auch tatsaechlich an.
    signal = "\n".join(orch._format_live_signal())
    check("Live-Signal markiert die kaputte Quelle mit ⚠️", "kaputte_quelle ⚠️" in signal)
    check("Live-Signal markiert die gesunde Quelle mit ✅", "gesunde_quelle ✅" in signal)


def main():
    test_gdelt_meldet_fehlschlag()
    test_truth_social_ohne_token_und_ohne_fallback()
    test_leere_antwort_ist_KEIN_fehlschlag()
    test_rss_einzelner_feed_kaputt_ist_kein_quellenausfall()
    test_orchestrator_uebernimmt_den_fehlschlag()

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
