"""Tests fuer die Uebernacht-/Vorboersen-Gap-Antizipation: die reine Zeit-/Richtungs-
Logik (scoring.predict_gap), minutes_until_close (market_hours) und die Verdrahtung im
Orchestrator + die Telegram-Zeile. Kein Netz.
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
# scoring.predict_gap (reine Logik)
# ---------------------------------------------------------------------------

def test_predict_gap():
    from app.scoring import predict_gap

    g = predict_gap("long", "after", None)
    check("long nachbörslich -> Gap hoch, early", g and g["gap_direction"] == "up" and g["early"])
    g = predict_gap("short", "closed", None)
    check("short über Nacht -> Gap runter, early", g and g["gap_direction"] == "down" and g["early"])
    g = predict_gap("long", "weekend", None)
    check("long am Wochenende -> Gap hoch, early", g and g["gap_direction"] == "up" and g["early"])
    check("long am Wochenende: Phase 'übers Wochenende'", g and "Wochenende" in g["phase"])

    g = predict_gap("long", "open", 30, near_close_minutes=45)
    check("long kurz vor Schluss (30<=45) -> Gap-Kandidat", g and g["gap_direction"] == "up")
    check("kurz vor Schluss: Phase erwähnt Schluss", g and "schluss" in g["phase"].lower())
    check("long mitten in der Session (120min) -> kein Gap",
          predict_gap("long", "open", 120, near_close_minutes=45) is None)

    g = predict_gap("long", "pre", None)
    check("long vorbörslich -> Gap hoch, aber NICHT early (läuft evtl. schon)",
          g and g["gap_direction"] == "up" and g["early"] is False)

    check("keine klare Richtung -> None", predict_gap(None, "after", None) is None)
    check("session None -> None", predict_gap("long", None, None) is None)
    check("session unknown -> None", predict_gap("long", "unknown", None) is None)

    # Mindest-Erwartungswert filtert Mini-Katalysatoren
    check("expected 1.0% < min 2.0% -> None",
          predict_gap("long", "after", None, expected_move_pct=1.0, min_expected_move_pct=2.0) is None)
    check("expected 3.0% >= min 2.0% -> Gap",
          predict_gap("long", "after", None, expected_move_pct=3.0, min_expected_move_pct=2.0) is not None)


# ---------------------------------------------------------------------------
# market_hours.minutes_until_close
# ---------------------------------------------------------------------------

def test_minutes_until_close():
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
    check("Mo 15:30 ET -> 30 Min bis Schluss", market_hours.minutes_until_close(ts(2026, 7, 20, 15, 30)) == 30)
    check("Mo 09:00 ET (vorbörslich) -> 420 Min bis Schluss",
          market_hours.minutes_until_close(ts(2026, 7, 20, 9, 0)) == 420)
    check("Mo 16:30 ET (nachbörslich) -> None",
          market_hours.minutes_until_close(ts(2026, 7, 20, 16, 30)) is None)
    # 2026-07-18 ist ein Samstag.
    check("Sa -> None", market_hours.minutes_until_close(ts(2026, 7, 18, 12, 0)) is None)


# ---------------------------------------------------------------------------
# Orchestrator-Verdrahtung + Telegram-Zeile
# ---------------------------------------------------------------------------

def _fresh(**env):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["ENABLE_GAP_PREDICTION"] = "true"
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["ENABLE_PRICE_TRACKING"] = "false"
    os.environ["ENABLE_TECHNICALS"] = "false"
    os.environ["PAPER_TRADING"] = "false"
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


def test_orchestrator_and_telegram():
    config, db, orch = _fresh()
    from app.db import Classification

    # Session/Restzeit forcieren: nachbörslich -> Gap hoch erwartet.
    orch.us_market_session = lambda ts=None: "after"
    orch.minutes_until_close = lambda ts=None: None

    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                         ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}],
                         expected_move_pct=5.0, expected_horizon="Tage")
    extras = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="g1", text="NVDA big news"),
        cls, statement_id=1, score=85, corroboration=1, hedged=False,
    ))
    gap = extras.get("overnight_gap")
    check("_build_alert_extras enthält overnight_gap", isinstance(gap, dict))
    check("Gap-Richtung up (long, nachbörslich)", gap and gap.get("gap_direction") == "up")
    check("Gap early=True (noch vor dem Gap)", gap and gap.get("early") is True)
    check("Gap trägt den Ticker", gap and gap.get("ticker") == "NVDA")

    # Mitten in der Session (viel Zeit) -> kein Gap-Hinweis.
    config, db, orch = _fresh()
    orch.us_market_session = lambda ts=None: "open"
    orch.minutes_until_close = lambda ts=None: 180
    extras2 = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="g2", text="NVDA"),
        cls, statement_id=2, score=85, corroboration=1, hedged=False,
    ))
    check("mitten in der Session -> kein overnight_gap", "overnight_gap" not in extras2)

    # Telegram-Zeile enthält den Rallye-Hinweis.
    import app.telegram_alert as tg
    importlib.reload(tg)
    msg = tg._format_message(
        orch.RawStatement(source="x", source_id="g1", text="NVDA big news"),
        cls,
        extras={"overnight_gap": {"gap_direction": "up", "phase": "nachbörslich",
                                  "early": True, "ticker": "NVDA"}},
    )
    check("Telegram: Übernacht-Rallye-Hinweis vorhanden", "Übernacht-Rallye" in msg)
    check("Telegram: nennt 'bevor' den Vorbörsen-Anstieg", "bevor" in msg.lower())


def main():
    test_predict_gap()
    test_minutes_until_close()
    test_orchestrator_and_telegram()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
