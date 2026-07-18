"""Tests fuer die neuen, standardmaessig AUSGESCHALTETEN Zustell-/Ingestion-Gates:
Universum/Liquiditaet (#8), Mindest-Erwartungswert (#3), Ticker-Cooldown (#6),
Ruhezeiten (#7) und Stale-News-Filter (#14). Jedes Szenario laeuft mit frischer DB und
frisch geladener Config, damit die per-Wert importierten Schwellen im Orchestrator die
jeweilige Einstellung sehen."""
import asyncio
import datetime
import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []

# Config-Schluessel, die zwischen Szenarien nicht durchsickern duerfen (explizit ruecksetzen).
_GATE_KEYS = [
    "TICKER_UNIVERSE", "ALERT_MIN_EXPECTED_MOVE_PCT", "TICKER_ALERT_COOLDOWN_MINUTES",
    "QUIET_HOURS", "QUIET_HOURS_MIN_CONVICTION", "MAX_NEWS_AGE_MINUTES", "ENABLE_PREFILTER",
]


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _fresh(env):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    for k in _GATE_KEYS:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.telegram_alert as tg
    importlib.reload(tg)
    import app.orchestrator as orch
    importlib.reload(orch)
    return config, db, orch, tmp.name


def _cls(db, tickers, **kw):
    return db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.9,
        ticker_calls=tickers, **kw,
    )


def test_universe_gate():
    config, db, orch, name = _fresh({"TICKER_UNIVERSE": "AAPL,MSFT"})
    nvda = _cls(db, [{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    aapl = _cls(db, [{"ticker": "AAPL", "direction": "long", "confidence": 0.95}])
    check("Universum: Ticker ausserhalb -> keine handelbaren Ticker",
          orch.actionable_tickers(nvda) == [])
    check("Universum: Ticker ausserhalb -> nicht alarmwuerdig", orch.is_alert_worthy(nvda) is False)
    check("Universum: Ticker innerhalb -> handelbar",
          len(orch.actionable_tickers(aapl)) == 1)
    check("Universum: Ticker innerhalb -> alarmwuerdig", orch.is_alert_worthy(aapl) is True)
    os.unlink(name)


def test_expected_move_gate():
    config, db, orch, name = _fresh({"ALERT_MIN_EXPECTED_MOVE_PCT": "2.0"})
    t = [{"ticker": "AAPL", "direction": "long", "confidence": 0.95}]
    small = _cls(db, t, expected_move_pct=1.0)
    big = _cls(db, t, expected_move_pct=3.0)
    none = _cls(db, t, expected_move_pct=None)
    check("Erwartungswert unter Schwelle -> nicht alarmwuerdig", orch.is_alert_worthy(small) is False)
    check("Erwartungswert ueber Schwelle -> alarmwuerdig", orch.is_alert_worthy(big) is True)
    check("fehlender Erwartungswert bei aktiver Schwelle -> nicht alarmwuerdig",
          orch.is_alert_worthy(none) is False)
    os.unlink(name)


def test_cooldown_gate():
    config, db, orch, name = _fresh({"TICKER_ALERT_COOLDOWN_MINUTES": "60"})
    cls = _cls(db, [{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    check("kein vorheriger Alert -> Cooldown passiert", orch._passes_cooldown(cls) is True)

    orch._record_ticker_alerts(cls, statement_id=1)
    check("nach Alert: derselbe Ticker+Richtung im Cooldown -> unterdrueckt",
          orch._passes_cooldown(cls) is False)

    # Andere Richtung ist nicht im Cooldown.
    short = _cls(db, [{"ticker": "NVDA", "direction": "short", "confidence": 0.95}])
    check("andere Richtung nicht im Cooldown", orch._passes_cooldown(short) is True)

    # Zwei Ticker, einer frisch -> es gibt Neues zu melden -> nicht unterdrueckt.
    mixed = _cls(db, [
        {"ticker": "NVDA", "direction": "long", "confidence": 0.95},
        {"ticker": "AMD", "direction": "long", "confidence": 0.95},
    ])
    check("gemischt (ein frischer Ticker) -> nicht unterdrueckt", orch._passes_cooldown(mixed) is True)
    os.unlink(name)


def test_quiet_hours_gate():
    # Fenster so waehlen, dass die AKTUELLE Berliner Stunde sicher drin bzw. draussen liegt.
    try:
        from zoneinfo import ZoneInfo
        h = datetime.datetime.now(ZoneInfo("Europe/Berlin")).hour
    except Exception:
        h = datetime.datetime.now(datetime.timezone.utc).hour
    inside = f"{(h - 1) % 24}-{(h + 2) % 24}"
    outside = f"{(h + 3) % 24}-{(h + 5) % 24}"

    config, db, orch, name = _fresh({"QUIET_HOURS": inside, "QUIET_HOURS_MIN_CONVICTION": "90"})
    check("Ruhezeit aktiv: schwacher Score wird zurueckgestellt", orch._passes_quiet_hours(50) is False)
    check("Ruhezeit aktiv: sehr hoher Score kommt durch", orch._passes_quiet_hours(95) is True)
    check("_in_quiet_hours_now erkennt das Fenster", orch._in_quiet_hours_now() is True)
    os.unlink(name)

    config, db, orch, name = _fresh({"QUIET_HOURS": outside, "QUIET_HOURS_MIN_CONVICTION": "90"})
    check("ausserhalb der Ruhezeit: auch schwacher Score kommt durch",
          orch._passes_quiet_hours(50) is True)
    check("_in_quiet_hours_now: ausserhalb -> False", orch._in_quiet_hours_now() is False)
    os.unlink(name)

    config, db, orch, name = _fresh({})  # keine Ruhezeit gesetzt
    check("keine Ruhezeit konfiguriert -> immer durch", orch._passes_quiet_hours(1) is True)
    os.unlink(name)


def test_stale_filter_integration():
    config, db, orch, name = _fresh({"MAX_NEWS_AGE_MINUTES": "60", "ENABLE_PREFILTER": "false"})
    import time
    now = time.time()

    orch.source_health["fake"] = {
        "last_poll_at": None, "last_success_at": None, "last_error": None,
        "last_error_at": None, "total_fetched": 0, "prefiltered": 0, "stale": 0,
    }

    class FakeSource:
        name = "fake"

        async def poll(self):
            return [
                db.RawStatement(source="fake", source_id="fresh",
                                text="Fed cuts rates now", published_at=now - 5 * 60),
                db.RawStatement(source="fake", source_id="old",
                                text="Old news from days ago", published_at=now - 3 * 3600),
                db.RawStatement(source="fake", source_id="nots",
                                text="No timestamp headline", published_at=None),
            ]

    classified = []

    async def fake_classify(text, recent_context=None, priority=False, model=None, _bypass_daily_cap=False):
        classified.append(text)
        return db.Classification(is_market_relevant=False, sentiment="neutral", confidence=0.1)

    orig = orch.classify
    orch.classify = fake_classify
    try:
        asyncio.run(orch.poll_once([FakeSource()], asyncio.Semaphore(1)))
    finally:
        orch.classify = orig

    check("frische Meldung wird klassifiziert", "Fed cuts rates now" in classified)
    check("alte Meldung wird verworfen (kein Claude-Call)", "Old news from days ago" not in classified)
    check("Meldung ohne Zeitstempel wird NICHT verworfen (konservativ)",
          "No timestamp headline" in classified)
    check("stale-Zaehler steht auf 1", orch.source_health["fake"]["stale"] == 1)
    os.unlink(name)


def main():
    test_universe_gate()
    test_expected_move_gate()
    test_cooldown_gate()
    test_quiet_hours_gate()
    test_stale_filter_integration()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
