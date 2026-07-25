"""Tests fuer die verdichtete Kauf-/Verkaufsempfehlung (Nutzerwunsch): fasst
Ueberzeugungs-Score, technische Zweitmeinung, Gap-Chase-Timing und Geruecht-/
Divergenz-Warnung des staerksten handelbaren Tickers zu EINER klaren Handlungsansage
zusammen (app.scoring.trade_recommendation). Deckt die Orchestrator-Verdrahtung
(_build_alert_extras) und die Telegram-Darstellung (inkl. Gap-Chase-Fortschrittsbalken)
ab. Die reine Formel selbst ist in tests/test_scoring.py getestet. Kein Netz
(Kursabfragen gemockt)."""
import asyncio
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


def _fresh(**env):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["ENABLE_TECHNICALS"] = "false"
    os.environ["ENABLE_GAP_CHASE_EVALUATION"] = "false"
    os.environ["ENABLE_PRICE_TRACKING"] = "false"
    os.environ["PAPER_TRADING"] = "false"
    os.environ.pop("ENABLE_TRADE_RECOMMENDATION", None)
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


def _cls(db, score_confidence=0.95, direction="long"):
    return db.Classification(
        is_market_relevant=True, sentiment="positive" if direction == "long" else "negative",
        confidence=score_confidence,
        ticker_calls=[{"ticker": "AAA", "direction": direction, "confidence": score_confidence}],
    )


def test_recommendation_wiring_buy():
    config, db, orch = _fresh()
    cls = _cls(db)
    extras = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="r1", text="AAA news"),
        cls, statement_id=1, score=90, corroboration=1, hedged=False,
    ))
    rec = extras.get("recommendation")
    check("Empfehlung in extras vorhanden", isinstance(rec, dict))
    check("hoher Score, keine Flags -> buy", rec and rec["action"] == "buy")
    check("Label nennt 'kaufen' (long)", rec and "kaufen" in rec["label"])


def test_recommendation_wiring_technical_override():
    config, db, orch = _fresh(ENABLE_TECHNICALS="true")
    cls = _cls(db)

    async def fake_technical(ticker, direction):
        return {"label": "Strong Sell", "agrees": False, "contradicts_strongly": True}

    orch._ticker_technical = fake_technical
    extras = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="r2", text="AAA news"),
        cls, statement_id=2, score=90, corroboration=1, hedged=False,
    ))
    rec = extras.get("recommendation")
    check("Technik widerspricht stark -> wait trotz hohem Score",
          rec and rec["action"] == "wait")
    check("Grund nennt Technik", rec and "Technik" in ", ".join(rec["reasons"]))


def test_recommendation_disabled():
    config, db, orch = _fresh(ENABLE_TRADE_RECOMMENDATION="false")
    cls = _cls(db)
    extras = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="r3", text="AAA news"),
        cls, statement_id=3, score=90, corroboration=1, hedged=False,
    ))
    check("deaktiviert -> kein recommendation-Key", "recommendation" not in extras)


def test_recommendation_no_actionable_ticker():
    config, db, orch = _fresh()
    cls = db.Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                             ticker_calls=[])
    extras = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="r4", text="Allgemeine Marktmeldung"),
        cls, statement_id=4, score=90, corroboration=1, hedged=False,
    ))
    check("keine handelbaren Ticker -> keine Empfehlung", "recommendation" not in extras)


def test_telegram_rendering():
    import app.telegram_alert as tg
    importlib.reload(tg)
    from app.db import Classification, RawStatement

    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                          ticker_calls=[{"ticker": "AAA", "direction": "long", "confidence": 0.95}])
    raw = RawStatement(source="x", source_id="r5", text="AAA news")

    msg = tg._format_message(raw, cls, extras={
        "recommendation": {"action": "buy", "label": "🟢 Jetzt kaufen", "reasons": ["hohe Überzeugung"]},
        "gap_chase": {"ticker": "AAA", "gap_pct": 5.0, "too_late": False, "used_fraction": 0.33,
                      "remaining_pct": 10.0, "cutoff_fraction": 0.41},
    })
    check("Empfehlungs-Zeile im Alert", "📢 Empfehlung: 🟢 Jetzt kaufen" in msg)
    check("Begruendung im Alert", "hohe Überzeugung" in msg)
    check("Fortschrittsbalken im Alert (Unicode-Bloecke)", "█" in msg and "░" in msg)
    check("Balken zeigt korrekten Prozentwert (41%)", "41%" in msg)

    # Ohne recommendation-Extra: keine Empfehlungs-Zeile, kein Crash.
    msg2 = tg._format_message(raw, cls, extras={})
    check("ohne Empfehlung: keine Empfehlungs-Zeile", "📢 Empfehlung" not in msg2)


def main():
    test_recommendation_wiring_buy()
    test_recommendation_wiring_technical_override()
    test_recommendation_disabled()
    test_recommendation_no_actionable_ticker()
    test_telegram_rendering()

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
