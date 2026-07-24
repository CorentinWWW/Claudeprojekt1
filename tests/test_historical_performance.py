"""Tests fuer das Historische-Performance-Feedback (ENABLE_HISTORICAL_PERFORMANCE_GATE):
ein Ticker "lernt" aus seiner eigenen bisherigen Erfolgsbilanz (ausgewertete
alert_outcomes) statt jede neue Meldung unabhaengig davon gleich zu behandeln.

Deckt ab: die reine Score-Formel (app/scoring.py: historical_performance_adjust),
den Score-Zu-/Abschlag im Orchestrator (_apply_historical_performance) sowie das
optionale harte Unterdrueckungs-Gate in _send_alerts - inklusive der Kontrolle, dass
das Verhalten bei deaktiviertem Gate (Default) unveraendert bleibt.
"""
import asyncio
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []

_KEYS = [
    "ENABLE_HISTORICAL_PERFORMANCE_GATE", "HISTORICAL_PERFORMANCE_MIN_SAMPLES",
    "HISTORICAL_PERFORMANCE_WEIGHT", "HISTORICAL_PERFORMANCE_SUPPRESS_BELOW",
    "ALERT_MIN_TICKER_CONFIDENCE", "ENABLE_PRICE_TRACKING", "ENABLE_TECHNICALS",
]


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _fresh(env=None):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    for k in _KEYS:
        os.environ.pop(k, None)
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.85"
    for k, v in (env or {}).items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)
    return db, orch, tmp.name


def _cls(db, ticker, direction="long", confidence=0.9):
    return db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.9,
        ticker_calls=[{"ticker": ticker, "direction": direction, "confidence": confidence, "reasoning": "x"}],
    )


def _evaluate(db, statement_id, ticker, direction, return_pct, correct):
    """Legt einen ausgewerteten alert_outcomes-Eintrag an (Baseline + Nachmessung) -
    dieselbe Fixture wie in tests/test_new_round.py fuer Kelly/Quellen-Reliability."""
    db.record_alert_baseline(statement_id, ticker, direction, 0.9, time.time(), 100.0)
    oid = next(o["id"] for o in db.get_outcomes_awaiting_followup(0)
               if o["statement_id"] == statement_id and o["ticker"] == ticker)
    db.set_outcome_followup(oid, time.time(), 100.0 * (1 + return_pct / 100.0), return_pct, correct)


def test_historical_performance_adjust_pure():
    from app.scoring import historical_performance_adjust as hpa

    check("kein hit_rate -> kein Effekt", hpa(None, 10, 5, 15) == 0.0)
    check("zu wenige Samples -> kein Effekt", hpa(1.0, 2, 5, 15) == 0.0)
    check("weight<=0 -> kein Effekt (Feature aus)", hpa(1.0, 10, 5, 0) == 0.0)
    check("50% Trefferquote (Coinflip) -> neutral", hpa(0.5, 10, 5, 15) == 0.0)
    check("100% Trefferquote -> voller Zuschlag", hpa(1.0, 10, 5, 15) == 15.0)
    check("0% Trefferquote -> voller Abschlag", hpa(0.0, 10, 5, 15) == -15.0)
    check("80% Trefferquote -> anteiliger Zuschlag", abs(hpa(0.8, 10, 5, 15) - 9.0) < 1e-9)
    check("n genau auf min_samples zaehlt noch", hpa(1.0, 5, 5, 15) == 15.0)


def test_apply_historical_performance_helper():
    db, orch, tmp = _fresh()
    check("kein hitrate-Dict -> Score unveraendert", orch._apply_historical_performance(70, None) == 70)

    good = {"n": 10, "hits": 9, "hit_rate": 0.9}
    bad = {"n": 10, "hits": 1, "hit_rate": 0.1}
    check("gute Bilanz hebt den Score", orch._apply_historical_performance(70, good) > 70)
    check("schlechte Bilanz senkt den Score", orch._apply_historical_performance(70, bad) < 70)
    check("Score bleibt bei 100 gedeckelt", orch._apply_historical_performance(95, good) <= 100)
    check("Score bleibt bei 0 gedeckelt", orch._apply_historical_performance(5, bad) >= 0)
    os.unlink(tmp)


def test_send_alerts_suppresses_ticker_with_bad_history():
    db, orch, tmp = _fresh(env={
        "ENABLE_HISTORICAL_PERFORMANCE_GATE": "true",
        "HISTORICAL_PERFORMANCE_MIN_SAMPLES": "5",
        "HISTORICAL_PERFORMANCE_SUPPRESS_BELOW": "0.4",
    })
    # AAPL: 6 ausgewertete Alerts, nur 2 richtig (Trefferquote 1/3 < 0.4) -> soll
    # kuenftig unterdrueckt werden. NVDA bleibt ohne Historie (kein Effekt erwartet).
    for i in range(4):
        _evaluate(db, i + 1, "AAPL", "long", return_pct=-1.0, correct=False)
    for i in range(2):
        _evaluate(db, 100 + i, "AAPL", "long", return_pct=1.0, correct=True)

    sent = []

    async def fake_send_alert(raw, classification, extras=None):
        sent.append(raw.source_id)
        return True

    orig = orch.send_alert
    orch.send_alert = fake_send_alert
    try:
        raw_bad = db.RawStatement(source="news", source_id="bad-history", text="Apple News")
        raw_ok = db.RawStatement(source="news", source_id="fresh-ticker", text="Nvidia News")
        asyncio.run(orch._send_alerts([
            (raw_bad, _cls(db, "AAPL"), 9001),
            (raw_ok, _cls(db, "NVDA"), 9002),
        ]))
    finally:
        orch.send_alert = orig

    check("Ticker mit belegt schlechter Bilanz wird unterdrueckt", "bad-history" not in sent)
    check("Ticker ohne Historie laeuft normal durch", "fresh-ticker" in sent)
    os.unlink(tmp)


def test_gate_default_off_changes_nothing():
    """Ohne ENABLE_HISTORICAL_PERFORMANCE_GATE (Default aus) darf dieselbe schlechte
    Bilanz nichts am Verhalten aendern - wie alle Zustell-Gates in diesem Projekt
    aendert es das Verhalten NUR auf Wunsch."""
    db, orch, tmp = _fresh()
    for i in range(6):
        _evaluate(db, i + 1, "AAPL", "long", return_pct=-1.0, correct=False)

    sent = []

    async def fake_send_alert(raw, classification, extras=None):
        sent.append(raw.source_id)
        return True

    orig = orch.send_alert
    orch.send_alert = fake_send_alert
    try:
        raw_bad = db.RawStatement(source="news", source_id="bad-history", text="Apple News")
        asyncio.run(orch._send_alerts([(raw_bad, _cls(db, "AAPL"), 9001)]))
    finally:
        orch.send_alert = orig

    check("Gate aus (Default) -> keine Unterdrueckung trotz schlechter Bilanz", "bad-history" in sent)
    os.unlink(tmp)


def test_active_gates_reports_state():
    db, orch, tmp = _fresh(env={
        "ENABLE_HISTORICAL_PERFORMANCE_GATE": "true",
        "HISTORICAL_PERFORMANCE_SUPPRESS_BELOW": "0.4",
    })
    gates = orch.active_gates()
    check("active_gates zeigt Gate als aktiv", gates.get("historical_performance_gate") is True)
    check("active_gates zeigt Suppress-Schwelle", gates.get("historical_performance_suppress_below") == 0.4)
    os.unlink(tmp)


def main():
    test_historical_performance_adjust_pure()
    test_apply_historical_performance_helper()
    test_send_alerts_suppresses_ticker_with_bad_history()
    test_gate_default_off_changes_nothing()
    test_active_gates_reports_state()

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
