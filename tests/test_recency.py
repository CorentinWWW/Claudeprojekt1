"""Tests fuer die echte Veroeffentlichungszeit (statt Ingest-Zeit) und deren Anzeige
im Alert: Zeitstempel-Parser (Truth Social ISO-8601, GDELT-Kompaktformat, RSS
struct_time), die Altersangabe im Nachrichtenkopf und die Stern-Markierung der
handelbaren (hochsicheren) Ticker.
"""
import calendar
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Muss VOR dem Import von app.config/telegram_alert gesetzt werden (Config liest die
# Schwelle beim Import), damit die Marker-Tests deterministisch bei 0.90 pruefen.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def test_parse_iso8601_epoch():
    from app.util import parse_iso8601_epoch

    # 2026-07-15T13:30:00Z == 1784295000 (UTC)
    expected = calendar.timegm(time.strptime("2026-07-15T13:30:00", "%Y-%m-%dT%H:%M:%S"))
    check("ISO mit Z", parse_iso8601_epoch("2026-07-15T13:30:00Z") == expected)
    check("ISO mit Millisekunden + Z", parse_iso8601_epoch("2026-07-15T13:30:00.000Z") == expected)
    check("ISO mit expliziter UTC-Offset", parse_iso8601_epoch("2026-07-15T13:30:00+00:00") == expected)
    # Ohne Zeitzone -> als UTC interpretiert (nicht als lokale Zeit).
    check("ISO ohne Zeitzone -> als UTC", parse_iso8601_epoch("2026-07-15T13:30:00") == expected)
    check("Ungueltiger String -> None", parse_iso8601_epoch("kein-datum") is None)
    check("Leerer String -> None", parse_iso8601_epoch("") is None)
    check("None -> None", parse_iso8601_epoch(None) is None)
    check("Nicht-String -> None", parse_iso8601_epoch(12345) is None)


def test_parse_compact_utc_epoch():
    from app.util import parse_compact_utc_epoch

    expected = calendar.timegm(time.strptime("2026-07-15T13:30:00", "%Y-%m-%dT%H:%M:%S"))
    check("GDELT-Kompaktformat", parse_compact_utc_epoch("20260715T133000Z") == expected)
    check("GDELT ungueltig -> None", parse_compact_utc_epoch("2026-07-15") is None)
    check("GDELT leer -> None", parse_compact_utc_epoch("") is None)
    check("GDELT None -> None", parse_compact_utc_epoch(None) is None)


def test_rss_entry_epoch():
    import app.sources.news_rss as rss

    known = calendar.timegm(time.strptime("2026-07-15T13:30:00", "%Y-%m-%dT%H:%M:%S"))
    entry = {"published_parsed": time.gmtime(known)}
    check("RSS published_parsed -> Epoch", rss._entry_published_epoch(entry) == known)

    entry_updated = {"updated_parsed": time.gmtime(known)}
    check("RSS updated_parsed als Fallback -> Epoch", rss._entry_published_epoch(entry_updated) == known)

    # Ohne Datum -> Fallback auf ~jetzt (nicht 0/None).
    now_ish = rss._entry_published_epoch({})
    check("RSS ohne Datum -> Fallback auf ~jetzt", abs(now_ish - time.time()) < 5)


def test_format_age():
    import app.telegram_alert as ta

    now = time.time()
    check("kein Zeitstempel (None) -> leer", ta._format_age(None) == "")
    check("0 -> leer", ta._format_age(0) == "")
    check("nicht-numerisch -> leer", ta._format_age("gestern") == "")
    check("gerade eben (30s)", ta._format_age(now - 30) == "gerade eben")
    check("vor 5 Min", ta._format_age(now - 5 * 60) == "vor 5 Min")
    check("vor 2 Std", ta._format_age(now - 2 * 3600) == "vor 2 Std")
    check("vor 3 Tg", ta._format_age(now - 3 * 86400) == "vor 3 Tg")
    check("Zukunft (Uhr-Skew) -> gerade eben", ta._format_age(now + 120) == "gerade eben")


def test_alert_shows_age_and_marker():
    import app.telegram_alert as ta
    from app.db import Classification, RawStatement

    cls = Classification(
        is_market_relevant=True, sentiment="negative", confidence=0.9,
        reasoning="Zoelle auf Halbleiter.",
        ticker_calls=[
            {"ticker": "TSM", "direction": "short", "confidence": 0.95, "reasoning": "x"},
            {"ticker": "INTC", "direction": "long", "confidence": 0.6, "reasoning": "y"},
        ],
    )
    raw = RawStatement(source="t", source_id="a1", text="x",
                       url="https://example.com/a", published_at=time.time() - 300)
    text = ta._format_message(raw, cls)

    check("Alter wird im Kopf angezeigt", "🕒 vor 5 Min" in text)
    check("handelbarer Ticker (>=0.90) bekommt Stern", "TSM (short) – 95% ⭐" in text)
    check("Kontext-Ticker (<0.90) bekommt KEINEN Stern", "INTC (long) – 60%" in text and "60% ⭐" not in text)
    check("genau ein Stern (nur der handelbare Ticker)", text.count("⭐") == 1)

    # Ohne Zeitstempel: kein Uhr-Segment, aber ansonsten normale Nachricht.
    raw_no_time = RawStatement(source="t", source_id="a2", text="x", url="https://example.com/a")
    text2 = ta._format_message(raw_no_time, cls)
    check("ohne Zeitstempel kein Uhr-Segment", "🕒" not in text2)
    check("ohne Zeitstempel trotzdem Ticker-Stern vorhanden", text2.count("⭐") == 1)


def main():
    test_parse_iso8601_epoch()
    test_parse_compact_utc_epoch()
    test_rss_entry_epoch()
    test_format_age()
    test_alert_shows_age_and_marker()

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
