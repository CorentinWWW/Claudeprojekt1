"""Tests fuer Preis-Feedback/Backtesting (#2) und Konfidenz-Kalibrierung (#3):
Baseline-Erfassung, Outcome-Auswertung (mit gemocktem Kursdienst) und die
aggregierten Kalibrierungs-Statistiken. Kein echtes Netz."""
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


def _fresh():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["ENABLE_PRICE_TRACKING"] = "true"
    os.environ["PRICE_OUTCOME_HORIZON_MINUTES"] = "1"
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)
    return tmp.name, db, orch


def main():
    tmp_name, db, orch = _fresh()

    now = time.time()
    old = now - 3600  # aelter als der 1-Minuten-Horizont

    # Zwei alarmierte Ticker mit Ausgangskurs.
    db.record_alert_baseline(101, "NVDA", "long", 0.95, old, 100.0)
    db.record_alert_baseline(102, "TSLA", "short", 0.92, old, 200.0)
    # Doppelte Erfassung desselben (statement_id, ticker) darf nicht ueberschreiben.
    db.record_alert_baseline(101, "NVDA", "long", 0.95, now, 999.0)

    awaiting = db.get_outcomes_awaiting_followup(60)
    check("beide offenen Outcomes warten auf Nachmessung", len(awaiting) == 2)
    nvda_row = next(o for o in awaiting if o["ticker"] == "NVDA")
    check("Baseline nicht ueberschrieben (alter Kurs 100 bleibt)", nvda_row["alert_price"] == 100.0)

    # Gemockter Kursdienst: NVDA steigt (long -> korrekt), TSLA steigt (short -> falsch).
    async def fake_get_quote(ticker):
        return {"NVDA": {"price": 110.0}, "TSLA": {"price": 210.0}}.get(ticker)

    orig = orch.prices.get_quote
    orch.prices.get_quote = fake_get_quote
    try:
        asyncio.run(orch._evaluate_alert_outcomes())
    finally:
        orch.prices.get_quote = orig

    check("keine offenen Outcomes mehr", db.get_outcomes_awaiting_followup(60) == [])

    stats = db.get_calibration_stats()
    check("2 Outcomes ausgewertet", stats["evaluated"] == 2)
    check("1 Treffer (NVDA long korrekt)", stats["hits"] == 1)
    check("Trefferquote 50%", abs(stats["hit_rate"] - 0.5) < 1e-9)
    check("nach Richtung: long korrekt",
          stats["by_direction"].get("long", {}).get("hit_rate") == 1.0)
    check("nach Richtung: short falsch",
          stats["by_direction"].get("short", {}).get("hit_rate") == 0.0)
    check("keine offenen mehr in pending", stats["pending"] == 0)

    # --- Unveraenderter Kurs bei geschlossenem Markt ist KEINE Messung ---------------
    # Regressionstest fuer einen Live-Fund: bei geschlossenem Markt liefert die
    # Kursquelle beim Nachmessen exakt denselben Kurs wie beim Alarm. return_pct ist
    # dann 0.0 und fiel durch beide correct-Raster (>0 / <0) - das Nicht-Ereignis wurde
    # also als FALSCHE Vorhersage gebucht. In der Produktion waren dadurch 8 von 10
    # ausgewerteten Alerts Schein-Fehlschlaege (Trefferquote scheinbar 10%).
    tmp_c, db_c, orch_c = _fresh()
    old_c = time.time() - 3600
    db_c.record_alert_baseline(201, "AAPL", "long", 0.95, old_c, 150.0)
    db_c.record_alert_baseline(202, "MSFT", "long", 0.95, old_c, 300.0)

    async def frozen_quote(ticker):
        # AAPL exakt unveraendert (eingefrorener Schlusskurs), MSFT hat sich bewegt.
        return {"AAPL": {"price": 150.0}, "MSFT": {"price": 303.0}}.get(ticker)

    orig_c = orch_c.prices.get_quote
    orig_session = orch_c.us_market_session
    orch_c.prices.get_quote = frozen_quote
    orch_c.us_market_session = lambda *a, **kw: "closed"
    try:
        asyncio.run(orch_c._evaluate_alert_outcomes())
    finally:
        orch_c.prices.get_quote = orig_c
        orch_c.us_market_session = orig_session

    stats_c = db_c.get_calibration_stats()
    check("Markt zu + Kurs unveraendert: NICHT als Fehlschlag gebucht",
          stats_c["evaluated"] == 1)
    check("Markt zu + Kurs unveraendert: bleibt offen fuer den naechsten Versuch",
          len(db_c.get_outcomes_awaiting_followup(60)) == 1)
    check("bewegter Kurs wird trotz geschlossenem Markt normal ausgewertet",
          stats_c["hits"] == 1 and stats_c["hit_rate"] == 1.0)

    # Gegenprobe: bei OFFENEM Markt ist ein unveraenderter Kurs eine echte Aussage
    # (keine Bewegung in die erwartete Richtung) und wird ganz normal gebucht.
    tmp_o, db_o, orch_o = _fresh()
    db_o.record_alert_baseline(301, "AAPL", "long", 0.95, time.time() - 3600, 150.0)
    orig_o = orch_o.prices.get_quote
    orig_session_o = orch_o.us_market_session
    orch_o.prices.get_quote = frozen_quote
    orch_o.us_market_session = lambda *a, **kw: "open"
    try:
        asyncio.run(orch_o._evaluate_alert_outcomes())
    finally:
        orch_o.prices.get_quote = orig_o
        orch_o.us_market_session = orig_session_o
    check("Markt OFFEN + Kurs unveraendert: wird normal als Fehlschlag gebucht",
          db_o.get_calibration_stats()["evaluated"] == 1)

    # --- _fetch_ticker_context: Baseline erfassen + heutige Bewegung liefern ---
    tmp2, db2, orch2 = _fresh()
    from app.db import Classification

    async def fake_quote2(ticker):
        return (
            {"price": 50.0, "open": 40.0, "high": 52.0, "low": 39.0, "change_pct": 25.0}
            if ticker == "AMD" else None
        )

    orig2 = orch2.prices.get_quote
    orch2.prices.get_quote = fake_quote2
    try:
        cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.9,
                             ticker_calls=[{"ticker": "AMD", "direction": "long", "confidence": 0.95}])
        ctx = asyncio.run(orch2._fetch_ticker_context(cls, statement_id=500))
    finally:
        orch2.prices.get_quote = orig2

    # _fetch_ticker_context liefert jetzt {'change': {...}, 'risk': {...}}.
    check("_fetch_ticker_context liefert heutige Bewegung", ctx["change"].get("AMD") == 25.0)
    check("_fetch_ticker_context hat Baseline gespeichert",
          any(o["ticker"] == "AMD" and o["alert_price"] == 50.0
              for o in db2.get_outcomes_awaiting_followup(0)))

    # Deaktiviert -> keine Netzcalls, leeres Ergebnis.
    os.environ["ENABLE_PRICE_TRACKING"] = "false"
    import app.config as config
    importlib.reload(config)
    importlib.reload(orch2)
    called = {"n": 0}

    async def should_not_call(ticker):
        called["n"] += 1
        return None

    orch2.prices.get_quote = should_not_call
    cls2 = Classification(is_market_relevant=True, sentiment="positive", confidence=0.9,
                          ticker_calls=[{"ticker": "AMD", "direction": "long", "confidence": 0.95}])
    ctx2 = asyncio.run(orch2._fetch_ticker_context(cls2, statement_id=600))
    check("deaktiviert: kein Kurs-Call",
          called["n"] == 0 and ctx2["change"] == {} and ctx2["risk"] == {})

    os.unlink(tmp_name)
    os.unlink(tmp2)

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
