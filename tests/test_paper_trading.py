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

    # Anteile bewusst moderat, damit das Kapital ueber mehr Alerts reicht (siehe
    # position_fraction-Docstring) - frueher 30/22/16/12%.
    check("Sizing: hoher Score -> 15%", pt.position_fraction(90) == 0.15)
    check("Sizing: mittel -> 12%", pt.position_fraction(75) == 0.12)
    check("Sizing: Score None -> 9%", pt.position_fraction(None) == 0.09)
    check("Sizing: niedrig -> 6%", pt.position_fraction(10) == 0.06)

    # compute_position: 500 Depot, Score 90 -> 15% = 75 Einsatz, qty = 75/100 = 0.75
    plan = pt.compute_position(500.0, 500.0, 90, 100.0, "long", day_high=105, day_low=95)
    check("compute_position: Einsatz 75", plan["stake"] == 75.0)
    check("compute_position: qty = 0.75", abs(plan["qty"] - 0.75) < 1e-9)
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
    check("Einsatz 75 (Score 90 -> 15%)", pos["stake"] == 75.0)
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
    # Long @100 -> exit @90, qty 0.75 (Einsatz 75 bei 15%) -> -7.50 EUR
    check("Umkehr-Verlust realisiert (~-7.50)", abs(db.get_paper_realized_pnl() - (-7.5)) < 1e-6)


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

    check("kein Streak -> normales Sizing", pt.position_fraction(90, loss_streak=0) == 0.15)
    check("Streak unter Schwelle -> normales Sizing", pt.position_fraction(90, loss_streak=2) == 0.15)
    check("Streak erreicht Schwelle -> halbiert", abs(pt.position_fraction(90, loss_streak=3) - 0.075) < 1e-9)
    check("Streak ueber Schwelle -> weiterhin halbiert", abs(pt.position_fraction(90, loss_streak=5) - 0.075) < 1e-9)

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
    # Normal waer's 15% von 450 = 67.5; mit aktivem Kapitalerhalt-Modus 7.5% = 33.75.
    check("Einsatz durch Kapitalerhalt-Modus halbiert (~33.75 statt ~67.5)",
          abs(pos["stake"] - 33.75) < 1e-6)


def test_open_message_shows_session():
    """Kaufpreis-Transparenz (Nutzerwunsch): die 'Position eroeffnet'-Meldung zeigt, in
    welcher Boersen-Session der gemerkte (tatsaechliche) Kurs abgefragt wurde."""
    _, config, db, prices, pt, orch = _fresh()
    from app.db import Classification

    sent = _install_fakes(pt, orch, {
        "NVDA": {"price": 100.0, "high": 105.0, "low": 95.0, "change_pct": 1.0},
    })
    pt.us_market_session = lambda: "pre"
    cls = Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.95,
        ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}],
    )
    asyncio.run(pt.open_positions_for_alert(cls, statement_id=1, score=90))
    check("Session-Label ('vorbörslich') in der Eroeffnungs-Meldung",
          any("vorbörslich" in s for s in sent))
    check("tatsächlicher Kurs (100) weiterhin exakt gemerkt",
          db.get_open_paper_position_for_ticker("NVDA")["entry_price"] == 100.0)


def test_paper_max_positions_default_is_high():
    """PAPER_MAX_POSITIONS ist auf Nutzerwunsch hochgesetzt (Kapital, nicht Anzahl, soll
    der praktische Deckel sein) - reine Config-Regression."""
    _, config, db, prices, pt, orch = _fresh()
    check("Code-Standard PAPER_MAX_POSITIONS >= 50", config.PAPER_MAX_POSITIONS >= 50)


def test_trailing_stop_pure():
    """Trailing-Stop ('Gewinner laufen lassen'): reine Formel, ohne DB/Netz."""
    _, config, db, prices, pt, orch = _fresh()
    f = pt.trailing_stop_price

    # Long, Einstieg 100, Stop 90 -> R = 10. Trail aktiviert ab +1R (Hoch >= 110).
    check("noch nicht aktiv unterhalb activate_r (Hoch 105 = +0.5R)",
          f("long", 100.0, 90.0, 105.0) is None)
    check("genau an der Aktivierungsschwelle (Hoch 110 = +1R) -> aktiv",
          f("long", 100.0, 90.0, 110.0) is not None)
    # Bei Hoch 110 und distance 1R: Trail = 110 - 10 = 100 (= Break-even).
    check("Trail bei +1R liegt auf Break-even (100)",
          abs(f("long", 100.0, 90.0, 110.0) - 100.0) < 1e-9)
    # Hoch 130 -> Trail = 120, sichert deutlichen Gewinn.
    check("Trail zieht mit dem Hochpunkt mit (Hoch 130 -> Stop 120)",
          abs(f("long", 100.0, 90.0, 130.0) - 120.0) < 1e-9)
    # Der Trail darf NIE unter den urspruenglichen Stop zurueckfallen.
    check("Trail faellt nie unter den Ausgangs-Stop",
          f("long", 100.0, 90.0, 110.0) >= 90.0)

    # Short spiegelverkehrt: Einstieg 100, Stop 110 -> R = 10, Gewinn wenn Kurs faellt.
    check("Short: noch nicht aktiv (Tief 95 = +0.5R)", f("short", 100.0, 110.0, 95.0) is None)
    check("Short: aktiv ab Tief 90 (+1R)", f("short", 100.0, 110.0, 90.0) is not None)
    check("Short: Trail bei Tief 70 liegt bei 80",
          abs(f("short", 100.0, 110.0, 70.0) - 80.0) < 1e-9)

    # Robustheit
    check("ungueltige Richtung -> None", f("sideways", 100.0, 90.0, 130.0) is None)
    check("fehlender Stop -> None", f("long", 100.0, None, 130.0) is None)
    check("fehlender Hochpunkt -> None", f("long", 100.0, 90.0, None) is None)
    check("Risiko 0 (Stop == Einstieg) -> None", f("long", 100.0, 100.0, 130.0) is None)

    # update_high_water
    check("Long: Hochpunkt steigt", pt.update_high_water("long", 100.0, 110.0) == 110.0)
    check("Long: Hochpunkt faellt NICHT", pt.update_high_water("long", 110.0, 100.0) == 110.0)
    check("Short: Tiefpunkt faellt", pt.update_high_water("short", 100.0, 90.0) == 90.0)
    check("Short: Tiefpunkt steigt NICHT", pt.update_high_water("short", 90.0, 100.0) == 90.0)
    check("fehlender Startwert -> aktueller Kurs", pt.update_high_water("long", None, 105.0) == 105.0)


def test_costs_pure():
    """Handelskosten-Modell: beide Seiten, in Basispunkten auf den Positionswert."""
    _, config, db, prices, pt, orch = _fresh()
    # 10 bps je Seite auf 1000 Einsatz = 2 * 1000 * 0.001 = 2.00
    check("10 bps auf 1000 Einsatz kosten 2.00 (beide Seiten)",
          abs(pt.apply_costs(50.0, 1000.0, 10.0) - 48.0) < 1e-9)
    check("0 bps -> unveraendert", pt.apply_costs(50.0, 1000.0, 0.0) == 50.0)
    check("Kosten verschlechtern auch einen Verlust",
          abs(pt.apply_costs(-50.0, 1000.0, 10.0) - (-52.0)) < 1e-9)
    check("Einsatz 0 -> unveraendert", pt.apply_costs(50.0, 0.0, 10.0) == 50.0)
    check("Code-Standard PAPER_COST_BPS ist 0 (Tests bleiben exakt)",
          config.PAPER_COST_BPS == 0.0)


def test_trailing_stop_end_to_end():
    """Trailing-Stop ueber manage_open_positions: Kurs laeuft weit ins Plus, faellt dann
    zurueck -> die Position wird zum NACHGEZOGENEN Stop geschlossen (nicht zum
    urspruenglichen), also mit Gewinn statt Verlust."""
    _, config, db, prices, pt, orch = _fresh(PAPER_TRAILING_STOP="true")
    from app.db import Classification

    _install_fakes(pt, orch, {
        "NVDA": {"price": 100.0, "high": 102.0, "low": 98.0, "change_pct": 0.0},
    })
    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                         ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    asyncio.run(pt.open_positions_for_alert(cls, statement_id=1, score=90))
    pos = db.get_open_paper_position_for_ticker("NVDA")
    entry, initial_stop = pos["entry_price"], pos["stop"]
    risk = entry - initial_stop
    check("Ausgangs-Stop liegt unter dem Einstieg", risk > 0)
    check("high_water startet beim Einstiegskurs", pos["high_water"] == entry)

    # Kurs laeuft auf +3R -> Trail sollte auf +2R nachziehen, Position bleibt offen.
    top_price = entry + 3 * risk
    _install_fakes(pt, orch, {"NVDA": {"price": top_price, "high": top_price, "low": entry,
                                       "change_pct": 10.0}})
    asyncio.run(pt.manage_open_positions())
    pos2 = db.get_open_paper_position_for_ticker("NVDA")
    check("Position bei +3R noch offen (kein fixes Ziel mehr)", pos2 is not None)
    check("high_water auf den Hochpunkt gezogen", abs(pos2["high_water"] - top_price) < 1e-6)
    check("Stop wurde nachgezogen (jetzt ueber dem Einstieg)", pos2["stop"] > entry)
    check("Stop liegt bei ~+2R", abs(pos2["stop"] - (entry + 2 * risk)) < 1e-6)

    # Kurs faellt auf den nachgezogenen Stop zurueck -> Schliessung MIT Gewinn.
    fallback = pos2["stop"] - 0.01
    _install_fakes(pt, orch, {"NVDA": {"price": fallback, "high": top_price, "low": fallback,
                                       "change_pct": -5.0}})
    asyncio.run(pt.manage_open_positions())
    check("Position durch Trailing-Stop geschlossen", db.count_open_paper_positions() == 0)
    check("Trailing-Stop realisierte einen GEWINN (statt Verlust am Ausgangs-Stop)",
          db.get_paper_realized_pnl() > 0)


def test_time_exit():
    """Zeit-Exit: eine Position, die weder Stop noch Ziel erreicht, wird nach
    PAPER_MAX_HOLDING_HOURS glattgestellt und gibt ihr Kapital wieder frei."""
    _, config, db, prices, pt, orch = _fresh(
        PAPER_MAX_HOLDING_HOURS="24", PAPER_TRAILING_STOP="false"
    )
    from app.db import Classification

    _install_fakes(pt, orch, {
        "NVDA": {"price": 100.0, "high": 102.0, "low": 98.0, "change_pct": 0.0},
    })
    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                         ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    asyncio.run(pt.open_positions_for_alert(cls, statement_id=1, score=90))
    pid = db.get_open_paper_position_for_ticker("NVDA")["id"]
    free_before = pt.account_snapshot()["free_cash"]

    # Kurs bleibt zwischen Stop und Ziel -> ohne Zeit-Exit bliebe die Position ewig offen.
    _install_fakes(pt, orch, {"NVDA": {"price": 100.5, "high": 102.0, "low": 99.0,
                                       "change_pct": 0.5}})
    asyncio.run(pt.manage_open_positions())
    check("frisch eroeffnete Position bleibt offen", db.count_open_paper_positions() == 1)

    # Einstieg kuenstlich 25h zurueckdatieren -> Haltedauer ueberschritten.
    with db.get_conn() as conn:
        conn.execute("UPDATE paper_positions SET entry_ts = ? WHERE id = ?",
                     (time.time() - 25 * 3600, pid))
    _install_fakes(pt, orch, {"NVDA": {"price": 100.5, "high": 102.0, "low": 99.0,
                                       "change_pct": 0.5}})
    asyncio.run(pt.manage_open_positions())
    check("Position nach Ueberschreiten der Haltedauer geschlossen",
          db.count_open_paper_positions() == 0)
    check("Kapital wieder frei (mehr als vorher gebunden)",
          pt.account_snapshot()["free_cash"] > free_before)

    # Deaktiviert (0) -> kein Zeit-Exit.
    _, config2, db2, prices2, pt2, orch2 = _fresh(
        PAPER_MAX_HOLDING_HOURS="0", PAPER_TRAILING_STOP="false"
    )
    check("PAPER_MAX_HOLDING_HOURS=0 -> Zeit-Exit aus", config2.PAPER_MAX_HOLDING_HOURS == 0)

    check("holding_hours rechnet korrekt (~2h)",
          abs(pt.holding_hours(time.time() - 7200) - 2.0) < 0.01)
    check("holding_hours ohne Zeitstempel -> None", pt.holding_hours(None) is None)


def test_capital_preservation_disabled():
    """PAPER_CAPITAL_PRESERVATION=false -> kein Effekt, egal wie lang der Streak."""
    _, config, db, prices, pt, orch = _fresh(
        PAPER_CAPITAL_PRESERVATION="false", PAPER_LOSS_STREAK_THRESHOLD="3",
    )
    check("deaktiviert: Streak hat keinen Effekt", pt.position_fraction(90, loss_streak=10) == 0.15)


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
    test_open_message_shows_session()
    test_paper_max_positions_default_is_high()
    test_trailing_stop_pure()
    test_costs_pure()
    test_trailing_stop_end_to_end()
    test_time_exit()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
