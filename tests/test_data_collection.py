"""Tests fuer die Datensammlungs-Verbesserungen: Gate-Evaluation Logging,
Claude-Model-Tracking, Pipeline-Timing und Gap-Tracking. Quellen-Performance
(get_source_reliability) wird bereits in test_new_round.py abgedeckt."""
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


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def _fresh(env=None):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    for k in ("ALERT_MIN_CONVICTION", "TICKER_ALERT_COOLDOWN_MINUTES", "QUIET_HOURS",
              "MAX_ALERTS_PER_HOUR", "ENABLE_HISTORICAL_PERFORMANCE_GATE",
              "HISTORICAL_PERFORMANCE_SUPPRESS_BELOW", "ENABLE_TECHNICALS",
              "TECHNICALS_REQUIRE_AGREEMENT", "ALERT_MIN_TICKER_CONFIDENCE",
              "ENABLE_PRICE_TRACKING"):
        os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)
    return config, db, orch, tmp.name


def test_gate_evaluation_logging():
    config, db, orch, tmp_name = _fresh({
        "ALERT_MIN_CONVICTION": "70",
        "TICKER_ALERT_COOLDOWN_MINUTES": "0",
        "QUIET_HOURS": "",
        "MAX_ALERTS_PER_HOUR": "0",
        "ENABLE_HISTORICAL_PERFORMANCE_GATE": "false",
        "ENABLE_TECHNICALS": "false",
    })

    db.log_gate_evaluation(1, "conviction_score", 70, 65, False, "65 < 70")
    db.log_gate_evaluation(2, "conviction_score", 70, 80, True, "80 >= 70")
    db.log_gate_evaluation(3, "ticker_cooldown", None, None, True, "kein Cooldown")

    stats = db.get_gate_statistics(hours=24)
    check("gate_statistics enthaelt conviction_score", "conviction_score" in stats)
    check("conviction_score: 1 blockiert", stats["conviction_score"]["blocked"] == 1)
    check("conviction_score: 1 bestanden", stats["conviction_score"]["passed"] == 1)
    check("conviction_score: block_rate 0.5", abs(stats["conviction_score"]["block_rate"] - 0.5) < 1e-9)
    check("ticker_cooldown: 0 blockiert", stats["ticker_cooldown"]["blocked"] == 0)

    # --- Integration: _send_alerts loggt tatsaechlich ein conviction_score-Gate ---
    from app.db import Classification

    async def fake_get_quote(ticker):
        return None  # kein Preis-Tracking noetig fuer diesen Test

    orch.prices.get_quote = fake_get_quote
    cls_weak = Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.5,
        ticker_calls=[{"ticker": "AMD", "direction": "long", "confidence": 0.5, "reasoning": "x"}],
    )
    sid = db.insert_statement(
        db.RawStatement(source="test", source_id="gate-1", text="schwacher Alert AMD"),
        cls_weak,
    )
    asyncio.run(orch._send_alerts([(
        db.RawStatement(source="test", source_id="gate-1", text="schwacher Alert AMD"),
        cls_weak, sid,
    )]))
    stats2 = db.get_gate_statistics(hours=24)
    check(
        "echter conviction_score-Check wurde geloggt",
        stats2["conviction_score"]["checks"] >= 3,
    )

    os.unlink(tmp_name)


def test_claude_model_tracking():
    config, db, orch, tmp_name = _fresh()

    sid1 = db.insert_statement(
        db.RawStatement(source="test", source_id="model-1", text="a"),
        db.Classification(is_market_relevant=True, sentiment="positive", confidence=0.9),
        claude_model="claude-haiku-4-5",
    )
    sid2 = db.insert_statement(
        db.RawStatement(source="test", source_id="model-2", text="b"),
        db.Classification(is_market_relevant=True, sentiment="positive", confidence=0.9),
        claude_model="claude-sonnet-5",
    )

    db.record_alert_baseline(sid1, "NVDA", "long", 0.9, time.time() - 3600, 100.0)
    db.record_alert_baseline(sid2, "TSLA", "long", 0.9, time.time() - 3600, 200.0)
    oid1 = next(o["id"] for o in db.get_outcomes_awaiting_followup(0) if o["ticker"] == "NVDA")
    oid2 = next(o["id"] for o in db.get_outcomes_awaiting_followup(0) if o["ticker"] == "TSLA")
    db.set_outcome_followup(oid1, time.time(), 110.0, 10.0, True)
    db.set_outcome_followup(oid2, time.time(), 190.0, -5.0, False)

    perf = db.compare_model_performance()
    check("Haiku im Vergleich enthalten", "claude-haiku-4-5" in perf)
    check("Sonnet im Vergleich enthalten", "claude-sonnet-5" in perf)
    check("Haiku Trefferquote 100%", perf["claude-haiku-4-5"]["hit_rate"] == 1.0)
    check("Sonnet Trefferquote 0%", perf["claude-sonnet-5"]["hit_rate"] == 0.0)

    # --- _classify_and_store markiert Eskalationen korrekt mit dem Eskalations-Modell ---
    os.environ["ENABLE_BORDERLINE_ESCALATION"] = "true"
    os.environ["CLAUDE_ESCALATION_MODEL"] = "claude-sonnet-5"
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["ESCALATION_BAND"] = "0.1"
    importlib.reload(config)
    importlib.reload(orch)

    first_call = {"n": 0}

    async def fake_classify(text, recent_context=None, _bypass_daily_cap=False, priority=False, model=None):
        first_call["n"] += 1
        if model == config.CLAUDE_ESCALATION_MODEL:
            return db.Classification(
                is_market_relevant=True, sentiment="positive", confidence=0.95,
                ticker_calls=[{"ticker": "AMD", "direction": "long", "confidence": 0.95, "reasoning": "x"}],
            )
        return db.Classification(
            is_market_relevant=True, sentiment="positive", confidence=0.9,
            ticker_calls=[{"ticker": "AMD", "direction": "long", "confidence": 0.85, "reasoning": "x"}],
        )

    orch.classify = fake_classify
    sem = asyncio.Semaphore(1)
    raw = db.RawStatement(source="test", source_id="escalate-1", text="Grenzfall AMD")
    result = asyncio.run(orch._classify_and_store(raw, sem, []))
    check("Eskalations-Statement wurde gespeichert", result is not None and result[2] is not None)
    row = next(r for r in db.get_recent(limit=10) if r["source_id"] == "escalate-1")
    check("claude_model auf Eskalations-Modell gesetzt", row["claude_model"] == "claude-sonnet-5")

    os.unlink(tmp_name)


def test_pipeline_timing():
    config, db, orch, tmp_name = _fresh()

    db.log_pipeline_timing("cycle-1", "classify:rss", time.time(), 2500.0)
    db.log_pipeline_timing("cycle-1", "classify:rss", time.time(), 1500.0)
    db.log_pipeline_timing("cycle-1", "send_alerts:rss", time.time(), 100.0)

    stats = db.get_pipeline_statistics(hours=24)
    check("classify:rss in Statistik", "classify:rss" in stats)
    check("classify:rss: 2 Messungen", stats["classify:rss"]["count"] == 2)
    check("classify:rss: avg 2000ms", abs(stats["classify:rss"]["avg_ms"] - 2000.0) < 1e-6)
    check("classify:rss: min 1500ms", stats["classify:rss"]["min_ms"] == 1500.0)
    check("classify:rss: max 2500ms", stats["classify:rss"]["max_ms"] == 2500.0)

    os.unlink(tmp_name)


def test_gap_tracking():
    config, db, orch, tmp_name = _fresh({"ENABLE_PRICE_TRACKING": "true"})

    now = time.time()
    db.record_alert_baseline(1, "NVDA", "long", 0.9, now, 100.0, gap_pct=5.0)
    db.record_alert_baseline(2, "TSLA", "long", 0.9, now, 200.0, gap_pct=0.5)
    oid1 = next(o["id"] for o in db.get_outcomes_awaiting_followup(0) if o["ticker"] == "NVDA")
    oid2 = next(o["id"] for o in db.get_outcomes_awaiting_followup(0) if o["ticker"] == "TSLA")
    db.set_outcome_followup(oid1, now, 90.0, -10.0, False)
    db.set_outcome_followup(oid2, now, 210.0, 5.0, True)

    impact = db.analyze_gap_impact(large_gap_threshold_pct=3.0)
    check("grosser Gap: 1 Alert", impact["with_large_gap"]["n"] == 1)
    check("grosser Gap: 0% Trefferquote", impact["with_large_gap"]["hit_rate"] == 0.0)
    check("kleiner Gap: 1 Alert", impact["without_large_gap"]["n"] == 1)
    check("kleiner Gap: 100% Trefferquote", impact["without_large_gap"]["hit_rate"] == 1.0)

    # --- _overnight_gap_pct: Formel + None bei fehlender Historie ---
    async def fake_history(ticker):
        return {"date": ["2026-07-22", "2026-07-23"], "close": [100.0, 100.0]}

    orch.prices.get_history = fake_history
    orch.current_market_date = lambda: "2026-07-23"
    gap = asyncio.run(orch._overnight_gap_pct("NVDA", {"open": 103.0}))
    check("Gap-Berechnung: +3%", gap is not None and abs(gap - 3.0) < 1e-6)

    async def no_history(ticker):
        return None

    orch.prices.get_history = no_history
    gap_none = asyncio.run(orch._overnight_gap_pct("NVDA", {"open": 103.0}))
    check("Gap-Berechnung ohne Historie: None", gap_none is None)

    gap_no_open = asyncio.run(orch._overnight_gap_pct("NVDA", {}))
    check("Gap-Berechnung ohne Open-Kurs: None", gap_no_open is None)

    os.unlink(tmp_name)


def main():
    test_gate_evaluation_logging()
    test_claude_model_tracking()
    test_pipeline_timing()
    test_gap_tracking()

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
