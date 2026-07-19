"""Tests fuer die zweite Verbesserungs-Runde (12 neue Features):
Money-Gates (Min-Conviction #1, Alerts/Stunde #6), Alert-Kontext (Richtungswechsel #2,
Sektor-Cluster #4, Kurs-Divergenz #3, Kelly #5), Analytics (Quellen-Reliability #7,
Schwellen-Empfehlung #8, CSV-Export #9) sowie Robustheit/Observability (Config-
Validierung #10, Zyklus-Timing #11, Gates-Uebersicht #12)."""
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
    "ALERT_MIN_CONVICTION", "MAX_ALERTS_PER_HOUR", "SECTOR_CLUSTER_MIN",
    "SECTOR_CLUSTER_WINDOW_HOURS", "DIVERGENCE_WARN_PCT", "ENABLE_KELLY_SUGGESTION",
    "ENABLE_PRICE_TRACKING", "QUIET_HOURS", "WEEKLY_DIGEST_WEEKDAY",
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
    for k, v in (env or {}).items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.telegram_alert as tg
    importlib.reload(tg)
    import app.prices as prices
    importlib.reload(prices)
    import app.orchestrator as orch
    importlib.reload(orch)
    return config, db, orch, prices, tmp.name


def _cls(db, tickers, **kw):
    return db.Classification(is_market_relevant=True, sentiment="positive", confidence=0.9,
                             ticker_calls=tickers, **kw)


def _nvda_long(db):
    return _cls(db, [{"ticker": "NVDA", "direction": "long", "confidence": 0.95, "reasoning": "x"}])


def _evaluate(db, statement_id, ticker, direction, return_pct, correct):
    db.record_alert_baseline(statement_id, ticker, direction, 0.9, time.time(), 100.0)
    oid = next(o["id"] for o in db.get_outcomes_awaiting_followup(0)
               if o["statement_id"] == statement_id and o["ticker"] == ticker)
    db.set_outcome_followup(oid, time.time(), 100.0 * (1 + return_pct / 100.0), return_pct, correct)


# --- #5 Kelly (rein) ---------------------------------------------------------------
def test_kelly_fraction():
    from app.scoring import kelly_fraction

    f = kelly_fraction(0.6, 3.0, 2.0)  # b=1.5, edge=0.6-0.4/1.5=0.333, half=0.1667
    check("Kelly mit Edge > 0", f is not None and 0.16 < f < 0.17)
    check("Kelly ohne Edge -> 0", kelly_fraction(0.4, 2.0, 2.0) == 0.0)
    check("Kelly gedeckelt bei cap", kelly_fraction(0.99, 100.0, 0.1) <= 0.25)
    check("Kelly None-Eingabe -> None", kelly_fraction(None, 1, 1) is None)
    check("Kelly nicht-positiver Verlust -> None", kelly_fraction(0.6, 3.0, 0) is None)


# --- #1 Min-Conviction-Gate + #6 Ratelimit ----------------------------------------
def test_min_conviction_gate():
    async def run(threshold):
        config, db, orch, prices, name = _fresh({"ALERT_MIN_CONVICTION": str(threshold)})
        sent = {"n": 0}

        async def fake_send(r, c, extras=None):
            sent["n"] += 1
            return True
        orch.send_alert = fake_send
        await orch._send_alerts([(db.RawStatement(source="news", source_id="a", text="Fed cuts rates"),
                                  _nvda_long(db), 1)])
        os.unlink(name)
        return sent["n"]

    check("hohe Schwelle (99) unterdrueckt den Alert", asyncio.run(run(99)) == 0)
    check("niedrige Schwelle (50) laesst den Alert durch", asyncio.run(run(50)) == 1)


def test_rate_limit():
    async def run():
        config, db, orch, prices, name = _fresh({"MAX_ALERTS_PER_HOUR": "1"})
        sent = {"n": 0}

        async def fake_send(r, c, extras=None):
            sent["n"] += 1
            return True
        orch.send_alert = fake_send
        worthy = [
            (db.RawStatement(source="news", source_id="a", text="Fed cuts rates"), _nvda_long(db), 1),
            (db.RawStatement(source="news", source_id="b", text="ECB hikes rates"),
             _cls(db, [{"ticker": "AMD", "direction": "long", "confidence": 0.95}]), 2),
        ]
        await orch._send_alerts(worthy)
        os.unlink(name)
        return sent["n"]

    check("Ratelimit 1/Std: nur EIN Alert von zweien wird verschickt", asyncio.run(run()) == 1)


# --- #2 Richtungswechsel + #4 Sektor-Cluster + #5 Kelly (in Extras) ---------------
def test_direction_flip_extra():
    config, db, orch, prices, name = _fresh()
    db.record_ticker_alert(1, "NVDA", "long", time.time())  # zuletzt long alarmiert
    short = _cls(db, [{"ticker": "NVDA", "direction": "short", "confidence": 0.95}])
    extras = asyncio.run(orch._build_alert_extras(
        db.RawStatement(source="news", source_id="x", text="Nvidia stürzt ab"),
        short, 2, 90, 1, False))
    check("Richtungswechsel erkannt (vorher long)", extras.get("direction_flip") == "long")

    # gleiche Richtung -> kein Flip
    same = _nvda_long(db)
    extras2 = asyncio.run(orch._build_alert_extras(
        db.RawStatement(source="news", source_id="y", text="Nvidia legt zu"),
        same, 3, 90, 1, False))
    check("kein Flip bei gleicher Richtung", "direction_flip" not in extras2)
    os.unlink(name)


def test_sector_cluster_extra():
    config, db, orch, prices, name = _fresh({"SECTOR_CLUSTER_MIN": "2", "SECTOR_CLUSTER_WINDOW_HOURS": "6"})
    # Eine bereits alarmierte Halbleiter-Meldung.
    prior = _cls(db, [{"ticker": "AMD", "direction": "long", "confidence": 0.95}])
    prior.sectors = ["Halbleiter"]
    sid = db.insert_statement(db.RawStatement(source="news", source_id="p", text="AMD Rekord"), prior)
    db.mark_alert_sent(sid)

    new = _nvda_long(db)
    new.sectors = ["Halbleiter"]
    extras = asyncio.run(orch._build_alert_extras(
        db.RawStatement(source="news", source_id="n", text="Nvidia Rekord"), new, 2, 90, 1, False))
    cl = extras.get("sector_cluster")
    check("Sektor-Cluster erkannt", isinstance(cl, dict) and cl.get("sector") == "Halbleiter")
    check("Cluster-Zaehler = 2 (1 vorher + diese)", cl and cl.get("count") == 2)
    os.unlink(name)


def test_kelly_extra():
    config, db, orch, prices, name = _fresh({"ENABLE_KELLY_SUGGESTION": "true"})
    # 12 ausgewertete Ergebnisse mit klarem Edge.
    for i in range(9):
        _evaluate(db, i + 1, "NVDA", "long", return_pct=3.0, correct=True)
    for i in range(3):
        _evaluate(db, 100 + i, "NVDA", "long", return_pct=-1.0, correct=False)
    extras = asyncio.run(orch._build_alert_extras(
        db.RawStatement(source="news", source_id="k", text="Signal"), _nvda_long(db), 5, 90, 1, False))
    check("Kelly-Anteil in Extras (genug Historie)", isinstance(extras.get("kelly_fraction"), float))
    check("Kelly-Anteil positiv und <= cap", 0 < extras.get("kelly_fraction", 0) <= 0.25)
    os.unlink(name)


def test_divergence_extra():
    config, db, orch, prices, name = _fresh({"ENABLE_PRICE_TRACKING": "true", "DIVERGENCE_WARN_PCT": "2.0"})

    async def fake_quote(ticker):
        # long-These, aber Kurs heute -5% -> laeuft gegen die These.
        return {"price": 95.0, "open": 100.0, "high": 101.0, "low": 94.0, "change_pct": -5.0}
    orch.prices.get_quote = fake_quote

    extras = asyncio.run(orch._build_alert_extras(
        db.RawStatement(source="news", source_id="d", text="Nvidia long"), _nvda_long(db), 10, 90, 1, False))
    check("Kurs-Divergenz erkannt (long, aber heute -5%)",
          isinstance(extras.get("divergence"), str) and "NVDA" in extras["divergence"])
    os.unlink(name)


# --- #7/#8/#9 Analytics ------------------------------------------------------------
def test_source_reliability_and_kelly_inputs():
    config, db, orch, prices, name = _fresh()
    p1 = db.insert_statement(db.RawStatement(source="news", source_id="s1", text="a"),
                             _cls(db, []))
    p2 = db.insert_statement(db.RawStatement(source="rss", source_id="s2", text="b"),
                             _cls(db, []))
    _evaluate(db, p1, "NVDA", "long", 4.0, True)
    _evaluate(db, p2, "AMD", "long", -2.0, False)

    rel = {r["source"]: r for r in db.get_source_reliability()}
    check("Quellen-Reliability enthaelt beide Quellen", "news" in rel and "rss" in rel)
    check("news 1/1 richtig", rel["news"]["hits"] == 1 and rel["news"]["n"] == 1)
    check("rss 0/1 richtig", rel["rss"]["hits"] == 0 and rel["rss"]["n"] == 1)

    ki = db.get_kelly_inputs()
    check("Kelly-Inputs: hit_rate 50%", abs(ki["hit_rate"] - 0.5) < 1e-9)
    check("Kelly-Inputs: avg_win = 4%", abs(ki["avg_win_pct"] - 4.0) < 1e-9)
    check("Kelly-Inputs: avg_loss = 2%", abs(ki["avg_loss_pct"] - 2.0) < 1e-9)
    os.unlink(name)


def test_threshold_recommendation():
    _, db, orch, _, name = _fresh()
    import app.dashboard as dash
    importlib.reload(dash)
    calib = {"by_confidence_bucket": [
        {"bucket": "90-100%", "n": 8, "hits": 7, "hit_rate": 0.875},
        {"bucket": "70-80%", "n": 6, "hits": 3, "hit_rate": 0.5},
        {"bucket": "50-60%", "n": 2, "hits": 1, "hit_rate": 0.5},  # zu wenige Daten
    ]}
    rec = dash._recommend_min_confidence(calib, min_samples=5)
    check("Empfehlung aus bestem Bucket (90-100%)", rec and rec["based_on_bucket"] == "90-100%")
    check("Empfohlene Untergrenze 0.9", rec and rec["recommended_min_ticker_confidence"] == 0.9)
    check("keine Empfehlung ohne Daten", dash._recommend_min_confidence({"by_confidence_bucket": []}) is None)
    os.unlink(name)


def test_outcomes_csv_and_performance_endpoint():
    _, db, orch, _, name = _fresh()
    p1 = db.insert_statement(db.RawStatement(source="news", source_id="s1", text="a"), _cls(db, []))
    _evaluate(db, p1, "NVDA", "long", 4.0, True)
    import app.dashboard as dash
    importlib.reload(dash)

    resp = dash.api_outcomes_csv()
    body = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
    check("CSV hat Header", "ticker" in body and "return_pct" in body)
    check("CSV enthaelt NVDA-Zeile", "NVDA" in body)

    perf = dash.api_performance()
    check("/api/performance hat by_source", isinstance(perf.get("by_source"), list))
    check("/api/performance hat kelly-Block", "kelly" in perf)
    os.unlink(name)


# --- #10 Config-Validierung + #11 Timing + #12 Gates ------------------------------
def test_config_validation():
    config, db, orch, prices, name = _fresh({"QUIET_HOURS": "kaputt", "ALERT_MIN_CONVICTION": "150",
                                             "WEEKLY_DIGEST_WEEKDAY": "9"})
    errors, warnings = config.validate()
    joined = " ".join(warnings)
    check("Warnung bei ungueltigem QUIET_HOURS", "QUIET_HOURS" in joined)
    check("Warnung bei ALERT_MIN_CONVICTION > 100", "ALERT_MIN_CONVICTION" in joined)
    check("Warnung bei ungueltigem WEEKLY_DIGEST_WEEKDAY", "WEEKLY_DIGEST_WEEKDAY" in joined)
    os.unlink(name)


def test_cycle_timing_and_gates():
    config, db, orch, prices, name = _fresh({"MAX_ALERTS_PER_HOUR": "5", "QUIET_HOURS": "23-7"})
    orch._record_cycle_duration(1.5)
    orch._record_cycle_duration(2.5)
    check("letzte Zyklusdauer gesetzt", orch.run_health["last_cycle_seconds"] == 2.5)
    check("Zyklus-Samples gesammelt", len(orch.run_health["recent_cycle_seconds"]) == 2)

    gates = orch.active_gates()
    check("active_gates zeigt Ratelimit", gates["max_alerts_per_hour"] == 5)
    check("active_gates zeigt Ruhezeit", gates["quiet_hours"] == "23-7")
    check("active_gates: nicht gesetztes Gate ist None/false",
          gates["min_conviction"] is None)
    os.unlink(name)


def main():
    test_kelly_fraction()
    test_min_conviction_gate()
    test_rate_limit()
    test_direction_flip_extra()
    test_sector_cluster_extra()
    test_kelly_extra()
    test_divergence_extra()
    test_source_reliability_and_kelly_inputs()
    test_threshold_recommendation()
    test_outcomes_csv_and_performance_endpoint()
    test_config_validation()
    test_cycle_timing_and_gates()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
