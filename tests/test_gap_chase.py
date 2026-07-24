"""Tests fuer die Gap-Chase-Bewertung (Gegenstueck zur Uebernacht-Gap-Antizipation):
wurde ein Ticker bereits UEBER NACHT/VORBOERSLICH stark in Signalrichtung gegappt
(Markt war zu, jetzt zur Boersenoeffnung extrem hoch/niedrig), lohnt sich ein Einstieg
noch, und falls ja, wann verkaufen? Deckt ab: die reine Bewertungs-Formel
(scoring.evaluate_gap_chase), prices.previous_close, market_hours.minutes_since_open/
current_market_date, die Orchestrator-Verdrahtung (_evaluate_gap_chase,
_build_alert_extras) und die Telegram-Zeile. Kein Netz (Kursabfragen gemockt).
"""
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


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


# ---------------------------------------------------------------------------
# scoring.evaluate_gap_chase (reine Logik)
# ---------------------------------------------------------------------------

def test_evaluate_gap_chase_pure():
    from app.scoring import evaluate_gap_chase

    check("keine Richtung -> None", evaluate_gap_chase(None, 10.0) is None)
    check("gap_pct None -> None", evaluate_gap_chase("long", None) is None)
    check("gap_pct nicht endlich -> None", evaluate_gap_chase("long", float("nan")) is None)

    # Gap GEGEN die These (long, aber nach unten gegappt) -> kein Chase-Fall.
    check("long, Gap nach unten -> None (kein Chase-Fall)",
          evaluate_gap_chase("long", -8.0) is None)
    check("short, Gap nach oben -> None (kein Chase-Fall)",
          evaluate_gap_chase("short", 8.0) is None)

    # Mit Erwartungswert: Anteil-basierte Bewertung.
    r = evaluate_gap_chase("long", 12.0, expected_move_pct=15.0, too_late_ratio=0.8)
    check("12/15 = 80% -> genau an der Schwelle -> zu spaet", r and r["too_late"] is True)
    check("used_fraction korrekt (0.8)", r and abs(r["used_fraction"] - 0.8) < 1e-9)
    check("remaining_pct korrekt (3.0)", r and abs(r["remaining_pct"] - 3.0) < 1e-9)

    r2 = evaluate_gap_chase("long", 6.0, expected_move_pct=15.0, too_late_ratio=0.8)
    check("6/15 = 40% -> noch nicht zu spaet", r2 and r2["too_late"] is False)
    check("remaining_pct korrekt (9.0)", r2 and abs(r2["remaining_pct"] - 9.0) < 1e-9)

    # Short: gap_pct ist vorzeichenbehaftet (negativ = Kurs gefallen), with_thesis positiv.
    r3 = evaluate_gap_chase("short", -6.0, expected_move_pct=15.0, too_late_ratio=0.8)
    check("short, Kurs -6% -> mit der These, nicht zu spaet",
          r3 and r3["too_late"] is False and abs(r3["gap_pct"] - 6.0) < 1e-9)

    # Ohne Erwartungswert: absolute Ersatzschwelle.
    r4 = evaluate_gap_chase("long", 7.0, too_late_abs_pct=6.0)
    check("ohne Erwartungswert, Gap 7% >= 6% -> zu spaet", r4 and r4["too_late"] is True)
    check("used_fraction ohne Erwartungswert None", r4 and r4["used_fraction"] is None)
    r5 = evaluate_gap_chase("long", 4.0, too_late_abs_pct=6.0)
    check("ohne Erwartungswert, Gap 4% < 6% -> noch nicht zu spaet", r5 and r5["too_late"] is False)


# ---------------------------------------------------------------------------
# prices.previous_close (reine Logik)
# ---------------------------------------------------------------------------

def test_previous_close():
    from app.prices import previous_close

    check("kein history -> None", previous_close(None, "2026-07-21") is None)
    check("kein today -> None", previous_close({"date": ["2026-07-20"], "close": [100.0]}, "") is None)

    # Historie enthaelt den heutigen (laufenden) Tag noch NICHT als eigene Zeile.
    hist_no_today = {"date": ["2026-07-17", "2026-07-20"], "close": [98.0, 100.0]}
    check("ohne heutige Zeile -> letzte Zeile ist 'gestern'",
          previous_close(hist_no_today, "2026-07-21") == 100.0)

    # Historie enthaelt den heutigen Tag bereits als (unvollstaendige) Zeile.
    hist_with_today = {"date": ["2026-07-20", "2026-07-21"], "close": [100.0, 113.0]}
    check("mit heutiger Zeile -> die wird uebersprungen, 'gestern' bleibt korrekt",
          previous_close(hist_with_today, "2026-07-21") == 100.0)

    check("nur heutige Zeile vorhanden -> None",
          previous_close({"date": ["2026-07-21"], "close": [113.0]}, "2026-07-21") is None)
    check("date/close-Laengen inkonsistent -> None",
          previous_close({"date": ["2026-07-20", "2026-07-21"], "close": [100.0]}, "2026-07-21") is None)

    # Zeilenreihenfolge wird von Stooq NICHT garantiert sortiert (Relisting/Datenfehler
    # koennen eine Zeile falsch einordnen) - previous_close() muss trotzdem das Datum
    # mit dem tatsaechlichen Maximum < today waehlen, nicht einfach die letzte im Array
    # passende Zeile.
    hist_out_of_order = {
        "date": ["2026-07-20", "2026-07-21", "2026-07-16"],
        "close": [100.0, 113.0, 90.0],
    }
    check("unsortierte Historie -> trotzdem der ECHTE juengste Vortag (2026-07-20)",
          previous_close(hist_out_of_order, "2026-07-21") == 100.0)

    hist_duplicate = {
        "date": ["2026-07-17", "2026-07-20", "2026-07-18"],
        "close": [98.0, 100.0, 99.0],
    }
    check("Duplikat/Ausreisser am Ende der Liste -> trotzdem 2026-07-20 (100.0)",
          previous_close(hist_duplicate, "2026-07-21") == 100.0)


# ---------------------------------------------------------------------------
# market_hours.minutes_since_open / current_market_date
# ---------------------------------------------------------------------------

def test_market_hours_helpers():
    from app import market_hours
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        print("[OK] (übersprungen: keine Zeitzonendaten)")
        return

    def ts(y, m, d, hh, mm):
        return datetime.datetime(y, m, d, hh, mm, tzinfo=et).timestamp()

    # 2026-07-20 ist ein Montag.
    check("Mo 09:30 ET (Eroeffnung) -> 0 Min seit Open",
          market_hours.minutes_since_open(ts(2026, 7, 20, 9, 30)) == 0)
    check("Mo 09:55 ET -> 25 Min seit Open",
          market_hours.minutes_since_open(ts(2026, 7, 20, 9, 55)) == 25)
    check("Mo 09:00 ET (vorbörslich) -> None",
          market_hours.minutes_since_open(ts(2026, 7, 20, 9, 0)) is None)
    check("Mo 16:30 ET (nachbörslich) -> None",
          market_hours.minutes_since_open(ts(2026, 7, 20, 16, 30)) is None)
    check("Sa -> None", market_hours.minutes_since_open(ts(2026, 7, 18, 12, 0)) is None)

    check("current_market_date liefert ISO-Datum",
          market_hours.current_market_date(ts(2026, 7, 20, 9, 30)) == "2026-07-20")


# ---------------------------------------------------------------------------
# Orchestrator-Verdrahtung + Telegram-Zeile
# ---------------------------------------------------------------------------

def _fresh(**env):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["ENABLE_GAP_CHASE_EVALUATION"] = "true"
    os.environ["ENABLE_PRICE_TRACKING"] = "true"
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["ENABLE_GAP_PREDICTION"] = "false"
    os.environ["ENABLE_TECHNICALS"] = "false"
    os.environ["PAPER_TRADING"] = "false"
    os.environ["GAP_CHASE_WINDOW_MINUTES"] = "30"
    os.environ["GAP_CHASE_MIN_GAP_PCT"] = "3"
    os.environ["GAP_CHASE_TOO_LATE_RATIO"] = "0.8"
    for k, v in env.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)
    return config, db, orch


def _mock_prices(orch, *, open_price, price, prev_close, today="2026-07-21"):
    async def fake_quote(ticker, client=None):
        return {"price": price, "open": open_price, "high": open_price * 1.02,
                "low": open_price * 0.98, "change_pct": 0.0}

    async def fake_history(ticker, client=None, max_bars=400):
        return {"date": ["2026-07-17", "2026-07-20"], "close": [prev_close, prev_close]}

    orch.prices.get_quote = fake_quote
    orch.prices.get_history = fake_history
    orch.minutes_since_open = lambda ts=None: 10
    orch.current_market_date = lambda ts=None: today


def test_evaluate_gap_chase_orchestrator():
    config, db, orch = _fresh()

    # NVDA: gestern 100, heute mit +12% eroeffnet (also klar gegappt), Kurs seither kaum
    # bewegt. Erwartete Bewegung laut Claude 15% -> 80% schon verbraucht -> zu spaet.
    _mock_prices(orch, open_price=112.0, price=112.5, prev_close=100.0)
    result = asyncio.run(orch._evaluate_gap_chase("NVDA", "long", 15.0))
    check("Gap erkannt und bewertet", result is not None)
    check("Gap-Groesse korrekt (~12%)", result and abs(result["gap_pct"] - 12.0) < 0.1)
    check("bereits ueberwiegend gelaufen -> zu spaet", result and result["too_late"] is True)
    check("kein Ausstiegs-Kursziel bei 'zu spaet'", result and "target_price" not in result)

    # Gleicher Ticker, aber nur 5% gegappt bei 15% Erwartungswert -> noch Luft.
    _mock_prices(orch, open_price=105.0, price=105.2, prev_close=100.0)
    result2 = asyncio.run(orch._evaluate_gap_chase("NVDA", "long", 15.0))
    check("noch nicht ausgereizt -> lohnt sich noch", result2 and result2["too_late"] is False)
    check("Ausstiegs-Kursziel wird genannt", result2 and isinstance(result2.get("target_price"), (int, float)))
    check("Kursziel liegt ueber dem aktuellen Kurs (long)",
          result2 and result2["target_price"] > 105.2)
    # Kursziel MUSS auf prev_close (100.0) mit der VOLLEN erwarteten Bewegung (15%)
    # aufsetzen (100 * 1.15 = 115.0), nicht die verbleibende Bewegung (10%) auf den
    # bereits gegappten Live-Kurs (105.2) draufmultiplizieren (105.2 * 1.10 = 115.72,
    # falscher, verzerrter Wert - Gap- und Restbewegung wuerden sich sonst
    # faelschlich multiplikativ statt additiv auf prev_close aufaddieren).
    check("Kursziel exakt auf prev_close-Basis berechnet (100 * 1.15 = 115.0)",
          result2 and abs(result2["target_price"] - 115.0) < 1e-9)

    # Ausserhalb des Zeitfensters nach Eroeffnung -> keine Bewertung mehr.
    _mock_prices(orch, open_price=112.0, price=112.5, prev_close=100.0)
    orch.minutes_since_open = lambda ts=None: 45  # > GAP_CHASE_WINDOW_MINUTES=30
    check("ausserhalb des Zeitfensters -> None",
          asyncio.run(orch._evaluate_gap_chase("NVDA", "long", 15.0)) is None)

    # Zu kleiner Gap (< GAP_CHASE_MIN_GAP_PCT) -> keine Bewertung (kein "extrem hoch").
    _mock_prices(orch, open_price=101.0, price=101.1, prev_close=100.0)
    check("Gap zu klein -> None",
          asyncio.run(orch._evaluate_gap_chase("NVDA", "long", 15.0)) is None)


def test_build_alert_extras_wiring():
    from app.db import Classification

    config, db, orch = _fresh()
    _mock_prices(orch, open_price=112.0, price=112.5, prev_close=100.0)
    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                          ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}],
                          expected_move_pct=15.0, expected_horizon="Tage")
    extras = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="gc1", text="NVDA news"),
        cls, statement_id=1, score=85, corroboration=1, hedged=False,
    ))
    chase = extras.get("gap_chase")
    check("_build_alert_extras enthält gap_chase", isinstance(chase, dict))
    check("gap_chase trägt den Ticker", chase and chase.get("ticker") == "NVDA")

    # Ohne ENABLE_PRICE_TRACKING (Default) keine Bewertung, obwohl das Gate selbst an ist.
    config, db, orch = _fresh(ENABLE_PRICE_TRACKING="false")
    _mock_prices(orch, open_price=112.0, price=112.5, prev_close=100.0)
    extras2 = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="gc2", text="NVDA news"),
        cls, statement_id=2, score=85, corroboration=1, hedged=False,
    ))
    check("ohne Preis-Tracking -> kein gap_chase", "gap_chase" not in extras2)


def test_telegram_line():
    import app.telegram_alert as tg
    importlib.reload(tg)
    from app.db import Classification, RawStatement

    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                          ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    raw = RawStatement(source="x", source_id="gc1", text="NVDA news")

    msg_late = tg._format_message(raw, cls, extras={
        "gap_chase": {"ticker": "NVDA", "gap_pct": 12.0, "too_late": True,
                      "used_fraction": 0.8, "remaining_pct": 0.0},
    })
    check("Telegram: 'zu spaet'-Hinweis vorhanden", "riskant" in msg_late and "Gap-Fade" in msg_late)
    check("Telegram: nennt den Ticker", "NVDA" in msg_late)

    msg_ok = tg._format_message(raw, cls, extras={
        "gap_chase": {"ticker": "NVDA", "gap_pct": 5.0, "too_late": False,
                      "used_fraction": 0.33, "remaining_pct": 10.0, "target_price": 118.5},
    })
    check("Telegram: 'lohnt sich noch'-Hinweis vorhanden", "lohnen" in msg_ok)
    check("Telegram: Ausstiegs-Kursziel im Text", "118.5" in msg_ok)


def main():
    test_evaluate_gap_chase_pure()
    test_previous_close()
    test_market_hours_helpers()
    test_evaluate_gap_chase_orchestrator()
    test_build_alert_extras_wiring()
    test_telegram_line()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
