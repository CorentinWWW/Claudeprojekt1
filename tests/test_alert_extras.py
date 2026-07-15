"""Tests fuer die Alert-Anreicherung: Boersen-Session (#7), Volatilitaets-Flag (#6),
Chart-Buttons (#9), Themen-Zeitleiste (#5) und heutige Kursbewegung je Ticker (#8)."""
import importlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def main():
    import app.telegram_alert as ta
    from app.db import Classification, RawStatement
    from app.market_hours import session_label, us_market_session

    cls = Classification(
        is_market_relevant=True, sentiment="negative", confidence=0.9,
        reasoning="Trump kuendigt neue Zoelle auf Halbleiter an.",
        ticker_calls=[
            {"ticker": "TSM", "direction": "short", "confidence": 0.95},
            {"ticker": "INTC", "direction": "long", "confidence": 0.6},
        ],
    )
    raw = RawStatement(source="t", source_id="x", text="New tariffs on semiconductors",
                       url="https://ex.com/a", published_at=time.time() - 120)

    extras = {
        "ticker_change": {"TSM": -3.2},
        "thread": [
            {"text": "Erste Zoll-Drohung gegen Chips", "ingested_at": time.time() - 7200},
            {"text": "Jetzt konkret", "ingested_at": time.time()},
        ],
    }
    msg = ta._format_message(raw, cls, extras=extras)

    # #7 Session
    expected_session = session_label(us_market_session())
    check("Session im Kopf (falls Zeitzonendaten vorhanden)",
          not expected_session or expected_session in msg)

    # #6 Volatilitaet (Text enthaelt 'tariffs')
    check("Volatilitaets-Flag bei tariff-Text", "Hohe Volatilität" in msg)

    # #8 heutige Bewegung am handelbaren Ticker
    check("heutige Bewegung am Ticker (TSM -3.2%)", "TSM (short) – 95% ⭐ · heute -3.2%" in msg)
    check("Kontext-Ticker ohne Bewegung/Stern", "INTC (long) – 60%" in msg and "60% ⭐" not in msg)

    # #5 Thread-Zeitleiste
    check("Themen-Zeitleiste vorhanden", "🧵 Teil einer Entwicklung (2 Meldungen)" in msg)
    check("Thread nennt Beginn-Snippet", "Erste Zoll-Drohung gegen Chips" in msg)

    # #9 Chart-Buttons: nur der handelbare Ticker (TSM), nicht INTC
    markup = ta._build_chart_markup(cls.ticker_calls)
    buttons = markup["inline_keyboard"][0] if markup else []
    texts = [b["text"] for b in buttons]
    check("Chart-Button fuer handelbaren Ticker (TSM)", "📈 TSM" in texts)
    check("kein Chart-Button fuer schwachen Ticker (INTC)", "📈 INTC" not in texts)
    check("Chart-Button-URL ist http(s)",
          bool(buttons) and buttons[0]["url"].startswith("https://"))

    # Ohne Volatilitaets-Text kein Flag; ohne Thread keine Zeitleiste.
    calm = RawStatement(source="t", source_id="y", text="Trump lobt die Menge bei einer Kundgebung")
    msg_calm = ta._format_message(calm, cls, extras={})
    check("kein Volatilitaets-Flag ohne Signalwort", "Hohe Volatilität" not in msg_calm)
    check("keine Zeitleiste ohne Thread", "🧵" not in msg_calm)

    # Toggle: Buttons deaktivierbar.
    os.environ["ENABLE_CHART_BUTTONS"] = "false"
    import app.config as config
    importlib.reload(config)
    importlib.reload(ta)
    check("Chart-Buttons deaktiviert -> None", ta._build_chart_markup(cls.ticker_calls) is None)
    os.environ["ENABLE_CHART_BUTTONS"] = "true"

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
