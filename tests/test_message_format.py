"""Edge-Case-Tests fuer das Telegram-Nachrichtenformat und das robuste Parsen der
Claude-Antwort: Laengen-Sicherheitsnetz (auch bei durchs HTML-Escaping stark
aufgeblaehtem Text), HTML-Injection-Schutz, Pro-Ticker-Konfidenz inkl. Clamping
und Sortierung."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def test_length_safety_net():
    import app.telegram_alert as ta
    from app.db import Classification, RawStatement

    raw = RawStatement(source="test", source_id="x", text="x")

    # Pathologisch langer Begruendungstext
    cls = Classification(
        is_market_relevant=True, sentiment="negative", confidence=0.8,
        reasoning="A" * 6000,
        ticker_calls=[{"ticker": "XOM", "direction": "short", "confidence": 0.5, "reasoning": "x"}],
    )
    text = ta._format_message(raw, cls)
    check("Sicherheitsnetz: langer Text bleibt <= TELEGRAM_MAX_LENGTH", len(text) <= ta.TELEGRAM_MAX_LENGTH)
    check("Sicherheitsnetz: Ticker-Zeile bleibt trotz Kuerzung erhalten", "XOM" in text)

    # Adversarial: Text besteht komplett aus Zeichen, die html.escape() bis zu
    # verfuenffacht ("<" -> "&lt;") - das Vorab-Budget allein reicht dann nicht,
    # der harte Nach-Escaping-Notfallschnitt muss greifen.
    cls2 = Classification(
        is_market_relevant=True, sentiment="negative", confidence=0.8,
        reasoning="<" * 6000, ticker_calls=[],
    )
    text2 = ta._format_message(raw, cls2)
    check("Sicherheitsnetz: Escaping-aufgeblaehter Text bleibt <= TELEGRAM_MAX_LENGTH",
          len(text2) <= ta.TELEGRAM_MAX_LENGTH)


def test_html_injection_escaped():
    import app.telegram_alert as ta
    from app.db import Classification, RawStatement

    raw = RawStatement(source="test", source_id="x", text="x")
    cls = Classification(
        is_market_relevant=True, sentiment="negative", confidence=0.8,
        reasoning="<script>alert(1)</script>",
        ticker_calls=[{"ticker": "<b>EVIL</b>", "direction": "long", "confidence": 0.9, "reasoning": "x"}],
    )
    text = ta._format_message(raw, cls)
    check("HTML-Injection: <script> in Begruendung wird escaped", "<script>" not in text)
    check("HTML-Injection: <b> im Ticker wird escaped", "<b>EVIL</b>" not in text)


def test_ticker_confidence_parsing():
    import app.classifier as clf

    class FakeBlock:
        type = "tool_use"
        name = "classify_statement"
        input = {
            "is_market_relevant": True, "sentiment": "negative", "confidence": 0.8,
            "ticker_calls": [
                {"ticker": "A", "direction": "long", "confidence": 1.5, "reasoning": "x"},
                {"ticker": "B", "direction": "short", "confidence": None, "reasoning": "y"},
                {"ticker": "C", "direction": "long", "confidence": "oops", "reasoning": "z"},
            ],
            "sectors": [], "reasoning": "test",
        }

    class FakeResponse:
        content = [FakeBlock()]

    result = clf._parse_response(FakeResponse())
    check("Ticker-Konfidenz: Wert > 1 wird auf 1.0 geclampt", result.ticker_calls[0]["confidence"] == 1.0)
    check("Ticker-Konfidenz: explizites null wird 0.0", result.ticker_calls[1]["confidence"] == 0.0)
    check("Ticker-Konfidenz: unparsebarer String wird 0.0, Ticker bleibt erhalten",
          result.ticker_calls[2]["confidence"] == 0.0 and len(result.ticker_calls) == 3)


def test_ticker_sorting():
    import app.telegram_alert as ta
    from app.db import Classification, RawStatement

    raw = RawStatement(source="test", source_id="x", text="x")
    cls = Classification(
        is_market_relevant=True, sentiment="negative", confidence=0.85,
        reasoning="Zoelle auf Halbleiter.",
        ticker_calls=[
            {"ticker": "NVDA", "direction": "short", "confidence": 0.55, "reasoning": "x"},
            {"ticker": "TSM", "direction": "short", "confidence": 0.9, "reasoning": "y"},
            {"ticker": "INTC", "direction": "long", "confidence": 0.7, "reasoning": "z"},
        ],
    )
    text = ta._format_message(raw, cls)
    check("Sortierung: sicherster Ticker (TSM 90%) steht ganz oben",
          text.index("TSM") < text.index("INTC") < text.index("NVDA"))
    check("Sortierung: ausgeschriebenes long/short + Prozent pro Ticker",
          "TSM (short) – 90%" in text and "INTC (long) – 70%" in text)
    check("Sortierung: volle Ueberschrift enthalten", "Zoelle auf Halbleiter." in text)


def main():
    test_length_safety_net()
    test_html_injection_escaped()
    test_ticker_confidence_parsing()
    test_ticker_sorting()

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
