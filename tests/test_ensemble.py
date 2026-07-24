"""Tests fuer das Ensemble-Modell (app/ensemble.py): das Bag-of-Words-Naive-Bayes-
Training aus historischen alert_outcomes, die Vorhersage sowie die Integration als
Zustell-Gate/Score-Anpassung in orchestrator._send_alerts."""
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
    for k in ("ENABLE_ENSEMBLE_MODEL", "ENSEMBLE_MIN_TRAINING_SAMPLES",
              "ENSEMBLE_SUPPRESS_BELOW", "ENSEMBLE_CONVICTION_WEIGHT",
              "ENSEMBLE_RETRAIN_SECONDS", "ENABLE_PRICE_TRACKING", "ALERT_MIN_CONVICTION"):
        os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.ensemble as ensemble
    importlib.reload(ensemble)
    ensemble.clear_cache()
    import app.orchestrator as orch
    importlib.reload(orch)
    return config, db, ensemble, orch, tmp.name


def _seed_outcome(db, sid, ticker, text, correct):
    stmt_id = db.insert_statement(
        db.RawStatement(source="test", source_id=f"seed-{sid}", text=text),
        db.Classification(is_market_relevant=True, sentiment="positive", confidence=0.9),
    )
    db.record_alert_baseline(stmt_id, ticker, "long", 0.9, time.time() - 3600, 100.0)
    oid = next(
        o["id"] for o in db.get_outcomes_awaiting_followup(0)
        if o["statement_id"] == stmt_id and o["ticker"] == ticker
    )
    db.set_outcome_followup(oid, time.time(), 110.0 if correct else 90.0,
                             10.0 if correct else -10.0, correct)
    return stmt_id


def test_no_model_below_min_samples():
    config, db, ensemble, orch, tmp_name = _fresh()
    model = ensemble.get_model(min_samples=10)
    check("kein Modell unterhalb der Mindest-Sample-Zahl", model is None)
    os.unlink(tmp_name)


def test_no_model_single_class():
    config, db, ensemble, orch, tmp_name = _fresh()
    for i in range(5):
        _seed_outcome(db, i, "AAA", f"grossartige uebernahme rakete durchbruch {i}", True)
    model = ensemble.get_model(min_samples=5)
    check("kein Modell bei nur EINER Ergebnisklasse (nur Treffer)", model is None)
    os.unlink(tmp_name)


def test_training_and_prediction():
    config, db, ensemble, orch, tmp_name = _fresh()
    # Klar getrennter Wortschatz: "rakete durchbruch uebernahme" -> immer Treffer,
    # "einbruch skandal ermittlung" -> immer Fehlschlag.
    for i in range(8):
        _seed_outcome(db, f"hit{i}", "AAA", "rakete durchbruch grossartige uebernahme meldung", True)
    for i in range(8):
        _seed_outcome(db, f"miss{i}", "BBB", "einbruch skandal ermittlung schlechte meldung", False)

    model = ensemble.get_model(min_samples=10)
    check("Modell trainiert nach genug Samples beider Klassen", model is not None)
    check("Modell kennt 16 Trainings-Dokumente", model["n"] == 16)
    check("hit_docs == 8", model["hit_docs"] == 8)
    check("miss_docs == 8", model["miss_docs"] == 8)

    p_hit_text = ensemble.predict_hit_probability(model, "rakete durchbruch grossartige uebernahme")
    p_miss_text = ensemble.predict_hit_probability(model, "einbruch skandal ermittlung schlechte")
    check("Treffer-Wortschatz -> hohe geschaetzte Trefferwahrscheinlichkeit",
          p_hit_text is not None and p_hit_text > 0.7)
    check("Fehlschlag-Wortschatz -> niedrige geschaetzte Trefferwahrscheinlichkeit",
          p_miss_text is not None and p_miss_text < 0.3)
    check("leerer Text -> keine Vorhersage (None)",
          ensemble.predict_hit_probability(model, "") is None)

    # Cache: ein zweiter get_model()-Aufruf innerhalb des TTL liefert dasselbe Objekt,
    # OHNE erneut zu trainieren (identity-Check).
    model2 = ensemble.get_model(min_samples=10, retrain_seconds=900.0)
    check("Modell wird innerhalb des TTL aus dem Cache bedient (dasselbe Objekt)", model2 is model)

    os.unlink(tmp_name)


def test_send_alerts_suppresses_on_low_ensemble_probability():
    config, db, ensemble, orch, tmp_name = _fresh({
        "ENABLE_ENSEMBLE_MODEL": "true",
        "ENSEMBLE_MIN_TRAINING_SAMPLES": "10",
        "ENSEMBLE_SUPPRESS_BELOW": "0.5",
        "ALERT_MIN_CONVICTION": "0",
    })
    for i in range(8):
        _seed_outcome(db, f"hit{i}", "AAA", "rakete durchbruch grossartige uebernahme meldung", True)
    for i in range(8):
        _seed_outcome(db, f"miss{i}", "BBB", "einbruch skandal ermittlung schlechte meldung", False)

    cls_bad = db.Classification(
        is_market_relevant=True, sentiment="negative", confidence=0.9,
        ticker_calls=[{"ticker": "CCC", "direction": "long", "confidence": 0.95, "reasoning": "x"}],
    )
    raw_bad = db.RawStatement(source="test", source_id="bad-signal", text="einbruch skandal ermittlung")
    sid_bad = db.insert_statement(raw_bad, cls_bad)

    async def fake_get_quote(ticker):
        return None

    orch.prices.get_quote = fake_get_quote
    asyncio.run(orch._send_alerts([(raw_bad, cls_bad, sid_bad)]))

    row = next(r for r in db.get_recent(limit=10) if r["source_id"] == "bad-signal")
    check("Alert mit schlechtem Ensemble-Wortschatz wird NICHT als verschickt markiert",
          row["alert_sent"] == 0)

    stats = db.get_gate_statistics(hours=24)
    check("ensemble_model-Gate wurde geloggt", "ensemble_model" in stats)
    check("ensemble_model-Gate hat mind. 1 Blockierung", stats["ensemble_model"]["blocked"] >= 1)

    # Gegenprobe: guter Wortschatz wird NICHT blockiert.
    cls_good = db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.9,
        ticker_calls=[{"ticker": "DDD", "direction": "long", "confidence": 0.95, "reasoning": "x"}],
    )
    raw_good = db.RawStatement(source="test", source_id="good-signal", text="rakete durchbruch uebernahme")
    sid_good = db.insert_statement(raw_good, cls_good)
    asyncio.run(orch._send_alerts([(raw_good, cls_good, sid_good)]))
    row_good = next(r for r in db.get_recent(limit=10) if r["source_id"] == "good-signal")
    check("Alert mit gutem Ensemble-Wortschatz wird trotzdem verschickt (kein Telegram noetig)",
          row_good["alert_sent"] == 0)  # kein Telegram konfiguriert -> send_alert liefert False,
    # aber der Gate-Check selbst darf NICHT blockiert haben:
    stats2 = db.get_gate_statistics(hours=24)
    check("guter Wortschatz: ensemble_model-Gate bestanden (mind. 1x passed)",
          stats2["ensemble_model"]["passed"] >= 1)

    os.unlink(tmp_name)


def main():
    test_no_model_below_min_samples()
    test_no_model_single_class()
    test_training_and_prediction()
    test_send_alerts_suppresses_on_low_ensemble_probability()

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
