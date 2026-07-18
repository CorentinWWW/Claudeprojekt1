"""Tests fuer app/scoring.py - reine, deterministische Bewertungs-Hilfen (Ueberzeugungs-
Score #1, Hedge-Erkennung #2, Positionsgroesse #4, erwartete Bewegung #3, Ruhezeiten-
Fenster #7) und deren Darstellung im Alert (telegram_alert)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def test_conviction_score():
    from app.scoring import conviction_score

    base = conviction_score(0.9, 0.95)
    check("Score liegt im Bereich 0-100", 0 <= base <= 100)
    check("hohe Konfidenzen -> hoher Score", base >= 85)

    # Hedge senkt den Score.
    check("Hedge senkt den Score", conviction_score(0.9, 0.95, hedged=True) < base)
    # Hohe Volatilitaet senkt den Score.
    check("Volatilitaet senkt den Score", conviction_score(0.9, 0.95, high_volatility=True) < base)
    # Mehr Quellen heben den Score.
    check("mehr Quellen heben den Score",
          conviction_score(0.9, 0.95, corroboration_sources=3) > base)
    # Frische hebt, Alter senkt.
    fresh = conviction_score(0.9, 0.95, freshness_minutes=5)
    old = conviction_score(0.9, 0.95, freshness_minutes=5000)
    check("frische Meldung > alte Meldung", fresh > old)
    # Staerkere Ticker-Konfidenz dominiert.
    check("staerkere Ticker-Konfidenz -> hoeherer Score",
          conviction_score(0.5, 0.95) > conviction_score(0.5, 0.6))
    # Robust gegen None/Muell.
    check("None-Konfidenzen crashen nicht", isinstance(conviction_score(None, None), int))
    # Korroborations-Bonus ist gedeckelt (4 vs. 10 Quellen gleich).
    check("Korroborations-Bonus gedeckelt",
          conviction_score(0.9, 0.95, corroboration_sources=4)
          == conviction_score(0.9, 0.95, corroboration_sources=10))


def test_position_tier():
    from app.scoring import position_tier

    check("sehr hoher Score -> hohe Ueberzeugung", "hohe Überzeugung" in position_tier(95))
    check("mittlerer Score -> Standard", "Standard" in position_tier(80))
    check("niedriger Score -> Sondierung", "Sondierung" in position_tier(50))
    # Monoton: hoeherer Score nie schlechtere Kategorie (grobe Reihenfolge-Pruefung).
    order = {"🟠 Sondierung": 0, "🟡 Standard": 1, "🟢 hohe Überzeugung": 2}
    check("Tier ist monoton im Score",
          order[position_tier(95)] >= order[position_tier(80)] >= order[position_tier(50)])


def test_hedge_and_volatility():
    from app.scoring import has_hedge_language, high_volatility_text

    check("'could' ist Hedge", has_hedge_language("The Fed could cut rates next week"))
    check("'reportedly' ist Hedge", has_hedge_language("Company reportedly in talks to acquire rival"))
    check("deutsches 'könnte' ist Hedge", has_hedge_language("Die EZB könnte die Zinsen senken"))
    check("vollzogene Tatsache ist KEIN Hedge", not has_hedge_language("Fed cuts rates by 25 bps"))
    check("leerer Text -> kein Hedge", not has_hedge_language(""))
    check("None -> kein Hedge", not has_hedge_language(None))
    # 'May' als Monatsname darf NICHT faelschlich als Hedge gelten.
    check("Monat 'May' ist kein Hedge", not has_hedge_language("Earnings released in May were strong"))

    check("'tariff' ist Hoch-Vol", high_volatility_text("New tariff on imported cars"))
    check("harmlose Meldung ist nicht Hoch-Vol",
          not high_volatility_text("Company opens new store in Ohio"))
    check("None -> nicht Hoch-Vol", not high_volatility_text(None))


def test_expected_move_format():
    from app.scoring import format_expected_move

    check("Prozent + Horizont", format_expected_move(4.2, "Tage") == "~4% · Tage")
    check("nur Prozent ohne Horizont", format_expected_move(3, None) == "~3%")
    check("Betrag (Vorzeichen ignoriert)", format_expected_move(-5, "Stunden") == "~5% · Stunden")
    check("None -> leer", format_expected_move(None, "Tage") == "")
    check("0 -> leer", format_expected_move(0, "Tage") == "")
    check("nicht-numerisch -> leer", format_expected_move("viel", "Tage") == "")


def test_quiet_hours_window():
    from app.scoring import parse_hour_window, hour_in_window

    check("Standard-Fenster 23-7", parse_hour_window("23-7") == (23, 7))
    check("Tagesfenster 9-17", parse_hour_window("9-17") == (9, 17))
    check("leer -> None", parse_hour_window("") is None)
    check("None -> None", parse_hour_window(None) is None)
    check("Unsinn -> None", parse_hour_window("abc") is None)
    check("gleiche Grenzen -> None", parse_hour_window("5-5") is None)
    check("ungueltige Stunde -> None", parse_hour_window("25-7") is None)

    # Umschlag ueber Mitternacht (23-7): 2 Uhr drin, 12 Uhr draussen.
    win = (23, 7)
    check("02:00 liegt im Nacht-Fenster", hour_in_window(2, win) is True)
    check("23:00 liegt im Nacht-Fenster", hour_in_window(23, win) is True)
    check("12:00 liegt NICHT im Nacht-Fenster", hour_in_window(12, win) is False)
    check("07:00 (Ende, exklusiv) liegt NICHT im Fenster", hour_in_window(7, win) is False)
    # Tagesfenster ohne Umschlag (9-17).
    check("10:00 im Tagesfenster", hour_in_window(10, (9, 17)) is True)
    check("20:00 nicht im Tagesfenster", hour_in_window(20, (9, 17)) is False)
    check("None-Fenster -> immer False", hour_in_window(3, None) is False)


def test_alert_rendering_new_fields():
    """Der Einzel-Alert zeigt Ueberzeugung, erwartete Bewegung, Korroboration, Hedge-
    Hinweis sowie Stop/Ziel + Trefferquote je Ticker, wenn die Extras vorliegen."""
    from app.db import Classification, RawStatement
    from app.telegram_alert import _format_message

    cls = Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.92,
        ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95, "reasoning": "x"}],
        reasoning="Nvidia meldet Rekordzahlen", expected_move_pct=4.0, expected_horizon="Tage",
    )
    raw = RawStatement(source="news", source_id="s1", text="Nvidia beats estimates", url=None)
    extras = {
        "conviction": 93, "position_tier": "🟢 hohe Überzeugung",
        "corroboration": 3, "hedged": False,
        "ticker_risk": {"NVDA": {"stop": 120.5, "target": 140.0, "rr": 1.5}},
        "ticker_hitrate": {"NVDA": {"n": 10, "hits": 7, "hit_rate": 0.7}},
    }
    msg = _format_message(raw, cls, extras=extras)
    check("Ueberzeugung im Alert", "Überzeugung 93/100" in msg)
    check("Positions-Tier im Alert", "hohe Überzeugung" in msg)
    check("erwartete Bewegung im Alert", "erwartete Bewegung ~4% · Tage" in msg)
    check("Korroboration im Alert", "bestätigt durch 3 Quellen" in msg)
    check("Stop/Ziel im Alert", "SL 120.5" in msg and "TP 140" in msg)
    check("Trefferquote im Alert", "bisher 7/10 richtig" in msg)

    # Hedge-Hinweis erscheint nur, wenn gesetzt.
    extras_hedged = dict(extras, hedged=True)
    msg2 = _format_message(raw, cls, extras=extras_hedged)
    check("Hedge-Hinweis bei hedged=True", "unbestätigt/Gerücht" in msg2)
    check("kein Hedge-Hinweis ohne Flag", "unbestätigt/Gerücht" not in msg)


def main():
    test_conviction_score()
    test_position_tier()
    test_hedge_and_volatility()
    test_expected_move_format()
    test_quiet_hours_window()
    test_alert_rendering_new_fields()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
