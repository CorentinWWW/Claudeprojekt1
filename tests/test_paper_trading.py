"""Tests fuer app/paper_trading.py (virtuelles Depot): die reinen Rechenbausteine
(Sizing, PnL, Rendite, Stop/Ziel-Treffer) sowie ein DB-gestuetztes End-to-End-Szenario
(Position eroeffnen, Gegensignal-Umkehr, Stop-Loss-Schliessung, Depot-Status) mit
gemocktem Kursdienst und gemocktem Telegram-Versand. Kein echtes Netz.
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


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _fresh(**env):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["PAPER_TRADING"] = "true"
    os.environ["PAPER_STARTING_CAPITAL"] = "500"
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["PRICE_CACHE_TTL_SECONDS"] = "0"  # kein Cache in Tests
    for k, v in env.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.prices as prices
    importlib.reload(prices)
    import app.paper_trading as pt
    importlib.reload(pt)
    import app.orchestrator as orch
    importlib.reload(orch)
    return tmp.name, config, db, prices, pt, orch


# ---------------------------------------------------------------------------
# Reine Bausteine
# ---------------------------------------------------------------------------

def test_pure_helpers():
    _, config, db, prices, pt, orch = _fresh()

    check("Sizing: hoher Score -> 30%", pt.position_fraction(90) == 0.30)
    check("Sizing: mittel -> 22%", pt.position_fraction(75) == 0.22)
    check("Sizing: Score None -> 16%", pt.position_fraction(None) == 0.16)
    check("Sizing: niedrig -> 12%", pt.position_fraction(10) == 0.12)

    # compute_position: 500 Depot, Score 90 -> 30% = 150 Einsatz, qty = 150/100 = 1.5
    plan = pt.compute_position(500.0, 500.0, 90, 100.0, "long", day_high=105, day_low=95)
    check("compute_position: Einsatz 150", plan["stake"] == 150.0)
    check("compute_position: qty = 1.5", abs(plan["qty"] - 1.5) < 1e-9)
    check("compute_position: Stop < Einstieg (long)", plan["stop"] < 100.0)
    check("compute_position: Ziel > Einstieg (long)", plan["target"] > 100.0)

    # free_cash deckelt den Einsatz
    plan2 = pt.compute_position(500.0, 40.0, 90, 100.0, "long")
    check("compute_position: durch free_cash gedeckelt (40)", plan2["stake"] == 40.0)

    # unter Mindesteinsatz -> None
    check("compute_position: unter Mindesteinsatz -> None",
          pt.compute_position(500.0, 5.0, 90, 100.0, "long") is None)
    check("compute_position: ungueltiger Kurs -> None",
          pt.compute_position(500.0, 500.0, 90, 0.0, "long") is None)
    check("compute_position: ungueltige Richtung -> None",
          pt.compute_position(500.0, 500.0, 90, 100.0, "sideways") is None)

    # PnL / Rendite
    check("upnl long +", pt.unrealized_pnl("long", 100.0, 2.0, 110.0) == 20.0)
    check("upnl short + bei fallendem Kurs", pt.unrealized_pnl("short", 100.0, 2.0, 90.0) == 20.0)
    check("upnl short - bei steigendem Kurs", pt.unrealized_pnl("short", 100.0, 2.0, 110.0) == -20.0)
    check("return_pct long", abs(pt.return_pct("long", 100.0, 110.0) - 10.0) < 1e-9)
    check("return_pct short (fallend = positiv)", abs(pt.return_pct("short", 100.0, 90.0) - 10.0) < 1e-9)

    # Stop/Ziel-Treffer
    check("long: Stop getroffen", pt.stop_or_target_hit("long", 89.0, 90.0, 110.0) == "stop")
    check("long: Ziel getroffen", pt.stop_or_target_hit("long", 111.0, 90.0, 110.0) == "target")
    check("long: dazwischen -> None", pt.stop_or_target_hit("long", 100.0, 90.0, 110.0) is None)
    check("short: Stop (Kurs steigt) getroffen", pt.stop_or_target_hit("short", 111.0, 110.0, 90.0) == "stop")
    check("short: Ziel (Kurs faellt) getroffen", pt.stop_or_target_hit("short", 89.0, 110.0, 90.0) == "target")
    check("Stop hat Vorrang vor Ziel bei Gleichstand-Konflikt",
          pt.stop_or_target_hit("long", 89.0, 90.0, 88.0) == "stop")


# ---------------------------------------------------------------------------
# DB + Kontostand
# ---------------------------------------------------------------------------

def test_account_and_close():
    _, config, db, prices, pt, orch = _fresh()

    snap0 = pt.account_snapshot()
    check("Start: Depotwert 500", snap0["account_value"] == 500.0)
    check("Start: frei 500", snap0["free_cash"] == 500.0)

    pid = db.insert_paper_position(1, "NVDA", "long", 100.0, time.time(), 1.5, 150.0, 90.0, 115.0)
    check("Position angelegt (ID > 0)", pid > 0)
    snap1 = pt.account_snapshot()
    check("nach Eroeffnung: frei = 500 - 150 = 350", snap1["free_cash"] == 350.0)
    check("nach Eroeffnung: Depotwert unveraendert 500 (Cash-Basis)", snap1["account_value"] == 500.0)
    check("eine offene Position", db.count_open_paper_positions() == 1)
    check("offene Position fuer NVDA gefunden",
          db.get_open_paper_position_for_ticker("NVDA")["id"] == pid)

    # Schliessen mit +30 EUR Gewinn (Kurs 100 -> 120, qty 1.5)
    db.close_paper_position(pid, 120.0, time.time(), 30.0, "target")
    check("keine offene Position mehr", db.count_open_paper_positions() == 0)
    check("realisierter PnL = 30", db.get_paper_realized_pnl() == 30.0)
    snap2 = pt.account_snapshot()
    check("nach Schliessung: Depotwert 530", snap2["account_value"] == 530.0)
    check("nach Schliessung: frei 530 (nichts mehr gebunden)", snap2["free_cash"] == 530.0)
    stats = db.get_paper_closed_stats()
    check("closed-Stats: 1 Trade, 1 Gewinn", stats["closed"] == 1 and stats["wins"] == 1)

    # Idempotenz: erneutes Schliessen aendert nichts
    db.close_paper_position(pid, 999.0, time.time(), 999.0, "manual")
    check("erneutes close aendert nichts (Idempotenz)", db.get_paper_realized_pnl() == 30.0)


# ---------------------------------------------------------------------------
# open_positions_for_alert (mit gemocktem Kurs + Telegram)
# ---------------------------------------------------------------------------

def _install_fakes(pt, orch, quotes: dict):
    """Ersetzt Kursabfrage + Telegram-Versand durch Fakes. quotes: {TICKER: dict|None}."""
    sent = []

    async def fake_quote(ticker, client=None):
        return quotes.get(ticker.upper())

    async def fake_send_text(text):
        sent.append(text)
        return True

    orch.prices.get_quote = fake_quote
    pt.prices.get_quote = fake_quote
    import app.telegram_alert as tg
    tg.send_text = fake_send_text
    return sent


def test_open_positions_for_alert():
    _, config, db, prices, pt, orch = _fresh()
    from app.db import Classification

    sent = _install_fakes(pt, orch, {
        "NVDA": {"price": 100.0, "high": 105.0, "low": 95.0, "change_pct": 1.0},
    })

    cls = Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.95,
        ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}],
    )
    asyncio.run(pt.open_positions_for_alert(cls, statement_id=1, score=90))

    check("Position eroeffnet (1 offen)", db.count_open_paper_positions() == 1)
    pos = db.get_open_paper_position_for_ticker("NVDA")
    check("Einstiegskurs gemerkt (100)", pos["entry_price"] == 100.0)
    check("Einsatz 150 (Score 90 -> 30%)", pos["stake"] == 150.0)
    check("Eroeffnung wurde per Telegram gemeldet", any("eröffnet" in s for s in sent))

    # Gleiche Richtung erneut -> NICHT aufstocken
    asyncio.run(pt.open_positions_for_alert(cls, statement_id=2, score=90))
    check("gleiche Richtung stockt nicht auf (weiterhin 1 offen)", db.count_open_paper_positions() == 1)


def test_reversal_closes_and_reopens():
    _, config, db, prices, pt, orch = _fresh()
    from app.db import Classification

    sent = _install_fakes(pt, orch, {
        "NVDA": {"price": 100.0, "high": 105.0, "low": 95.0, "change_pct": 1.0},
    })
    long_cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                              ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    asyncio.run(pt.open_positions_for_alert(long_cls, statement_id=1, score=90))
    long_id = db.get_open_paper_position_for_ticker("NVDA")["id"]

    # Kurs faellt, jetzt kommt ein SHORT-Signal -> alte Long-Position glattstellen, Short eroeffnen.
    sent.clear()
    pt.prices.get_quote = orch.prices.get_quote = None  # sicherstellen, dass neuer Fake greift
    _install_fakes(pt, orch, {"NVDA": {"price": 90.0, "high": 95.0, "low": 88.0, "change_pct": -1.0}})
    short_cls = Classification(is_market_relevant=True, sentiment="negative", confidence=0.95,
                               ticker_calls=[{"ticker": "NVDA", "direction": "short", "confidence": 0.95}])
    asyncio.run(pt.open_positions_for_alert(short_cls, statement_id=2, score=90))

    check("alte Long-Position ist geschlossen", db.get_open_paper_position_for_ticker("NVDA")["direction"] == "short")
    check("genau eine offene Position (Short)", db.count_open_paper_positions() == 1)
    check("Long wurde realisiert (1 closed)", db.get_paper_closed_stats()["closed"] == 1)
    # Long @100 -> exit @90, qty 1.5 -> -15 EUR
    check("Umkehr-Verlust realisiert (~-15)", abs(db.get_paper_realized_pnl() - (-15.0)) < 1e-6)


def test_manage_closes_on_stop():
    _, config, db, prices, pt, orch = _fresh(PAPER_STATUS_INTERVAL_MINUTES="30")
    from app.db import Classification

    sent = _install_fakes(pt, orch, {
        "NVDA": {"price": 100.0, "high": 102.0, "low": 98.0, "change_pct": 0.0},
    })
    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                         ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    asyncio.run(pt.open_positions_for_alert(cls, statement_id=1, score=90))
    pos = db.get_open_paper_position_for_ticker("NVDA")
    stop = pos["stop"]

    # Kurs faellt unter den Stop -> manage_open_positions muss automatisch schliessen.
    sent = _install_fakes(pt, orch, {"NVDA": {"price": stop - 1.0, "high": 100.0, "low": stop - 1.0, "change_pct": -5.0}})
    asyncio.run(pt.manage_open_positions())

    check("Position durch Stop-Loss geschlossen", db.count_open_paper_positions() == 0)
    check("Schliessung wurde gemeldet", any("geschlossen" in s for s in sent))
    check("realisierter Verlust < 0", db.get_paper_realized_pnl() < 0)


def test_status_throttle():
    _, config, db, prices, pt, orch = _fresh(PAPER_STATUS_INTERVAL_MINUTES="30")
    from app.db import Classification

    sent = _install_fakes(pt, orch, {
        "NVDA": {"price": 100.0, "high": 102.0, "low": 98.0, "change_pct": 0.0},
    })
    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                         ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    asyncio.run(pt.open_positions_for_alert(cls, statement_id=1, score=90))

    # Kurs bleibt zwischen Stop und Ziel -> kein Auto-Close, aber ein Depot-Status.
    sent = _install_fakes(pt, orch, {"NVDA": {"price": 101.0, "high": 102.0, "low": 100.0, "change_pct": 1.0}})
    asyncio.run(pt.manage_open_positions())
    check("erster manage-Aufruf schickt einen Depot-Status", any("Paper-Depot" in s for s in sent))

    sent = _install_fakes(pt, orch, {"NVDA": {"price": 101.0, "high": 102.0, "low": 100.0, "change_pct": 1.0}})
    asyncio.run(pt.manage_open_positions())
    check("zweiter Aufruf sofort danach ist gedrosselt (kein weiterer Status)",
          not any("Paper-Depot" in s for s in sent))


def test_late_move_extra():
    _, config, db, prices, pt, orch = _fresh(ENABLE_PRICE_TRACKING="true", LATE_MOVE_WARN_PCT="3")
    from app.db import Classification

    _install_fakes(pt, orch, {
        "NVDA": {"price": 100.0, "open": 90.0, "high": 101.0, "low": 89.0, "change_pct": 8.0},
    })
    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                         ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    extras = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="x1", text="NVDA rally"),
        cls, statement_id=1, score=80, corroboration=1, hedged=False,
    ))
    check("late_move gesetzt (long, heute schon +8%)", extras.get("late_move") == 8.0)

    # Gegenrichtung: long, aber Kurs heute -8% -> KEINE late-move-Warnung (das ist Divergenz)
    _install_fakes(pt, orch, {
        "AMD": {"price": 100.0, "open": 110.0, "high": 111.0, "low": 99.0, "change_pct": -8.0},
    })
    cls2 = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                          ticker_calls=[{"ticker": "AMD", "direction": "long", "confidence": 0.95}])
    extras2 = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="x2", text="AMD"),
        cls2, statement_id=2, score=80, corroboration=1, hedged=False,
    ))
    check("kein late_move bei Gegenbewegung (long aber -8%)", extras2.get("late_move") is None)


def test_capital_preservation_mode():
    """Kapitalerhalt-Modus: nach genug Verlust-Trades IN FOLGE wird das Sizing kleiner -
    sowohl in der reinen position_fraction()-Berechnung als auch end-to-end ueber
    open_positions_for_alert() (das den Streak selbst aus der DB liest)."""
    _, config, db, prices, pt, orch = _fresh(
        PAPER_LOSS_STREAK_THRESHOLD="3", PAPER_LOSS_STREAK_SIZE_FACTOR="0.5"
    )

    check("kein Streak -> normales Sizing", pt.position_fraction(90, loss_streak=0) == 0.30)
    check("Streak unter Schwelle -> normales Sizing", pt.position_fraction(90, loss_streak=2) == 0.30)
    check("Streak erreicht Schwelle -> halbiert", pt.position_fraction(90, loss_streak=3) == 0.15)
    check("Streak ueber Schwelle -> weiterhin halbiert", pt.position_fraction(90, loss_streak=5) == 0.15)

    # get_consecutive_paper_losses: 3 Verluste in Folge, dann ein Gewinn (juenger) -> Streak = 0.
    now = time.time()

    def _closed_trade(ticker, entry_ts, exit_ts, pnl, reason):
        pid = db.insert_paper_position(1, ticker, "long", 100.0, entry_ts, 1.0, 100.0, None, None)
        db.close_paper_position(pid, 100.0 + pnl, exit_ts, pnl, reason)

    _closed_trade("AAA", now - 400, now - 300, -10.0, "stop")
    _closed_trade("BBB", now - 300, now - 200, -10.0, "stop")
    _closed_trade("CCC", now - 200, now - 100, -10.0, "stop")
    check("3 Verluste in Folge -> Streak 3", db.get_consecutive_paper_losses() == 3)

    _closed_trade("DDD", now - 100, now - 50, 10.0, "target")
    check("nach einem Gewinn -> Streak zurueckgesetzt auf 0", db.get_consecutive_paper_losses() == 0)

    # End-to-end: der aktive Kapitalerhalt-Modus reduziert den tatsaechlichen Einsatz bei
    # open_positions_for_alert() (Streak mit drei weiteren Verlusten wieder auf 3 bringen).
    _closed_trade("EEE", now - 50, now - 40, -10.0, "stop")
    _closed_trade("FFF", now - 40, now - 30, -10.0, "stop")
    _closed_trade("GGG", now - 30, now - 20, -10.0, "stop")
    check("erneut 3 Verluste in Folge (letzte 3 Trades)", db.get_consecutive_paper_losses() == 3)

    from app.db import Classification
    sent = _install_fakes(pt, orch, {
        "NVDA": {"price": 100.0, "high": 105.0, "low": 95.0, "change_pct": 1.0},
    })
    cls = Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.95,
        ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}],
    )
    asyncio.run(pt.open_positions_for_alert(cls, statement_id=99, score=90))
    pos = db.get_open_paper_position_for_ticker("NVDA")
    # Realisierter Saldo der obigen 7 Trades: -10-10-10+10-10-10-10 = -50 -> Depotwert 450.
    # Normal waer's 30% von 450 = 135; mit aktivem Kapitalerhalt-Modus 15% = 67.5.
    check("Einsatz durch Kapitalerhalt-Modus halbiert (~67.5 statt ~135)",
          abs(pos["stake"] - 67.5) < 1e-6)


def test_capital_preservation_disabled():
    """PAPER_CAPITAL_PRESERVATION=false -> kein Effekt, egal wie lang der Streak."""
    _, config, db, prices, pt, orch = _fresh(
        PAPER_CAPITAL_PRESERVATION="false", PAPER_LOSS_STREAK_THRESHOLD="3",
    )
    check("deaktiviert: Streak hat keinen Effekt", pt.position_fraction(90, loss_streak=10) == 0.30)


def main():
    test_pure_helpers()
    test_account_and_close()
    test_open_positions_for_alert()
    test_reversal_closes_and_reopens()
    test_manage_closes_on_stop()
    test_status_throttle()
    test_late_move_extra()
    test_capital_preservation_mode()
    test_capital_preservation_disabled()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
