"""Tests fuer Feedback/Observability: Multi-Quellen-Korroboration (#15), historische
Pro-Ticker-Trefferquote (#9), Performance-Aggregation (#11), Alerts-heute-Metrik (#12)
und den woechentlichen Performance-Digest (#10)."""
import asyncio
import datetime
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
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _fresh(env=None):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    for k, v in (env or {}).items():
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


def _relevant(db):
    return db.Classification(is_market_relevant=True, sentiment="positive", confidence=0.9)


def _evaluate(db, statement_id, ticker, direction, return_pct, correct, confidence=0.9):
    """Legt einen Baseline-Outcome an und schreibt die Nachmessung - erzeugt so einen
    fertig ausgewerteten alert_outcomes-Datensatz."""
    db.record_alert_baseline(statement_id, ticker, direction, confidence, time.time(), 100.0)
    pending = db.get_outcomes_awaiting_followup(0)
    oid = next(o["id"] for o in pending if o["statement_id"] == statement_id and o["ticker"] == ticker)
    db.set_outcome_followup(oid, time.time(), 100.0 * (1 + return_pct / 100.0), return_pct, correct)


def test_corroboration():
    config, db, orch, name = _fresh()
    primary_id = db.insert_statement(
        db.RawStatement(source="news", source_id="p1", text="Fed cuts rates"), _relevant(db)
    )
    check("Alleinstehende Meldung -> 1 Quelle", db.get_corroboration_count(primary_id) == 1)

    db.insert_statement(
        db.RawStatement(source="rss", source_id="d1", text="Fed cuts rates"), None,
        duplicate_of_id=primary_id,
    )
    db.insert_statement(
        db.RawStatement(source="news", source_id="d2", text="Fed cuts rates"), None,
        duplicate_of_id=primary_id,
    )
    check("zwei DISTINKTE Quellen (news+rss) -> 2", db.get_corroboration_count(primary_id) == 2)
    os.unlink(name)


def test_ticker_hitrate():
    config, db, orch, name = _fresh()
    check("kein Ergebnis -> None", db.get_ticker_hitrate("NVDA") is None)

    _evaluate(db, 1, "NVDA", "long", return_pct=2.0, correct=True)
    _evaluate(db, 2, "NVDA", "long", return_pct=-1.0, correct=False)
    hr = db.get_ticker_hitrate("NVDA")
    check("Trefferquote n=2", hr["n"] == 2)
    check("Trefferquote hits=1", hr["hits"] == 1)
    check("Trefferquote 50%", abs(hr["hit_rate"] - 0.5) < 1e-9)
    check("case-insensitiv (nvda)", db.get_ticker_hitrate("nvda") is not None)
    os.unlink(name)


def test_performance_stats():
    config, db, orch, name = _fresh()
    _evaluate(db, 1, "NVDA", "long", return_pct=5.0, correct=True)
    _evaluate(db, 2, "NVDA", "long", return_pct=3.0, correct=True)
    _evaluate(db, 3, "INTC", "short", return_pct=-4.0, correct=False)

    stats = db.get_performance_stats()
    check("insgesamt 3 ausgewertet", stats["evaluated"] == 3)
    check("2 Treffer", stats["hits"] == 2)
    check("Trefferquote 2/3", abs(stats["hit_rate"] - 2 / 3) < 1e-9)
    check("bester Ticker ist NVDA (hoechste Ø-Rendite)",
          stats["best_tickers"][0]["ticker"] == "NVDA")
    # Mit nur 2 Tickern insgesamt (< limit=5) sind BEIDE bereits in best_tickers
    # enthalten - worst_tickers darf sie dann nicht nochmal (ueberlappend) zeigen.
    check("weniger Ticker als Limit -> worst_tickers leer (kein Overlap mit best_tickers)",
          stats["worst_tickers"] == [])
    os.unlink(name)


def test_performance_stats_best_worst_no_overlap():
    """Bei MEHR Tickern als dem Limit (Standard 5) duerfen sich best_tickers und
    worst_tickers nicht ueberschneiden - vorher tauchte derselbe Ticker (Rang genau am
    Limit) gleichzeitig als 'bester' und 'schlechtester' auf."""
    config, db, orch, name = _fresh()
    returns = {
        "NVDA": 10.0, "AAPL": 8.0, "MSFT": 6.0, "GOOG": 4.0, "AMZN": 2.0,
        "TSLA": -1.0, "META": -3.0,
    }
    for i, (ticker, ret) in enumerate(returns.items(), start=1):
        _evaluate(db, i, ticker, "long", return_pct=ret, correct=ret > 0)

    stats = db.get_performance_stats()
    best = [t["ticker"] for t in stats["best_tickers"]]
    worst = [t["ticker"] for t in stats["worst_tickers"]]
    check("best_tickers = Top 5 nach Rendite", best == ["NVDA", "AAPL", "MSFT", "GOOG", "AMZN"])
    check("worst_tickers = die 2 verbleibenden, schlechtestes zuerst", worst == ["META", "TSLA"])
    check("keine Ueberschneidung zwischen best_tickers und worst_tickers",
          not (set(best) & set(worst)))
    os.unlink(name)


def test_alerts_today_metric():
    config, db, orch, name = _fresh()
    check("Start: 0 Alerts heute", db.get_alerts_sent_today() == 0)
    now = time.time()
    db.record_ticker_alert(1, "NVDA", "long", now)
    db.record_ticker_alert(1, "AMD", "long", now)
    check("nach 2 Alerts: Zaehler 2", db.get_alerts_sent_today() == 2)
    # Ein Alert von gestern zaehlt nicht mit.
    db.record_ticker_alert(2, "XOM", "long", now - 2 * 86400)
    check("gestriger Alert zaehlt nicht", db.get_alerts_sent_today() == 2)
    os.unlink(name)


def test_weekly_digest_format():
    config, db, orch, name = _fresh()
    from app.telegram_alert import format_weekly_digest

    stats = {
        "evaluated": 5, "hits": 3, "hit_rate": 0.6, "avg_return_pct": 1.23,
        "best_tickers": [{"ticker": "NVDA", "n": 3, "hits": 3, "hit_rate": 1.0, "avg_return_pct": 4.5}],
        "worst_tickers": [{"ticker": "INTC", "n": 2, "hits": 0, "hit_rate": 0.0, "avg_return_pct": -3.1}],
    }
    text = format_weekly_digest(stats)
    check("Digest hat Titel", "Wochen-Performance" in text)
    check("Digest nennt Trefferquote", "60%" in text)
    check("Digest nennt Ø-Rendite", "+1.23%" in text)
    check("Digest listet besten Ticker", "NVDA" in text)
    check("Digest listet schlechtesten Ticker", "INTC" in text)
    os.unlink(name)


def test_weekly_digest_trigger():
    today_weekday = datetime.datetime.now(datetime.timezone.utc).weekday()
    config, db, orch, name = _fresh({
        "ENABLE_WEEKLY_DIGEST": "true",
        "WEEKLY_DIGEST_WEEKDAY": str(today_weekday),
        "WEEKLY_DIGEST_MIN_HOUR": "0",
    })
    _evaluate(db, 1, "NVDA", "long", return_pct=5.0, correct=True)

    sent = {"n": 0}

    async def fake_send_weekly(stats):
        sent["n"] += 1
        return True

    orch.send_weekly_digest = fake_send_weekly
    asyncio.run(orch._maybe_send_weekly_digest())
    asyncio.run(orch._maybe_send_weekly_digest())
    check("Wochen-Digest wird genau einmal verschickt (Meta-Sperre)", sent["n"] == 1)
    os.unlink(name)


def test_weekly_digest_skips_without_data():
    today_weekday = datetime.datetime.now(datetime.timezone.utc).weekday()
    config, db, orch, name = _fresh({
        "ENABLE_WEEKLY_DIGEST": "true",
        "WEEKLY_DIGEST_WEEKDAY": str(today_weekday),
        "WEEKLY_DIGEST_MIN_HOUR": "0",
    })
    sent = {"n": 0}

    async def fake_send_weekly(stats):
        sent["n"] += 1
        return True

    orch.send_weekly_digest = fake_send_weekly
    asyncio.run(orch._maybe_send_weekly_digest())
    check("ohne ausgewertete Ergebnisse: kein Versand", sent["n"] == 0)
    os.unlink(name)


def test_hourly_performance():
    """Time-of-Day-Tracking (db.get_hourly_performance): Trefferquote/Rendite je
    Alarm-STUNDE (UTC), aus alert_ts extrahiert."""
    config, db, orch, name = _fresh()

    def _ts_for_hour(hour):
        return datetime.datetime(2020, 1, 1, hour, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

    def _seed(sid_suffix, ticker, hour, return_pct, correct):
        sid = db.insert_statement(
            db.RawStatement(source="news", source_id=f"hp-{sid_suffix}", text="x"), _relevant(db)
        )
        ts = _ts_for_hour(hour)
        db.record_alert_baseline(sid, ticker, "long", 0.9, ts, 100.0)
        oid = next(
            o["id"] for o in db.get_outcomes_awaiting_followup(0)
            if o["statement_id"] == sid and o["ticker"] == ticker
        )
        db.set_outcome_followup(oid, ts + 3600, 100.0 * (1 + return_pct / 100.0), return_pct, correct)

    _seed(1, "AAA", 9, 10.0, True)
    _seed(2, "BBB", 9, -10.0, False)
    _seed(3, "CCC", 22, 5.0, True)

    by_hour = {r["hour_utc"]: r for r in db.get_hourly_performance()}
    check("Stunde 9 hat 2 ausgewertete Alerts", by_hour[9]["n"] == 2)
    check("Stunde 9: Trefferquote 50%", abs(by_hour[9]["hit_rate"] - 0.5) < 1e-9)
    check("Stunde 22 hat 1 ausgewerteten Alert mit 100% Trefferquote",
          by_hour[22]["n"] == 1 and by_hour[22]["hit_rate"] == 1.0)
    check("nur Stunden mit Daten erscheinen (kein Eintrag fuer Stunde 5)", 5 not in by_hour)


def main():
    test_corroboration()
    test_ticker_hitrate()
    test_performance_stats()
    test_performance_stats_best_worst_no_overlap()
    test_alerts_today_metric()
    test_weekly_digest_format()
    test_weekly_digest_trigger()
    test_weekly_digest_skips_without_data()
    test_hourly_performance()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
