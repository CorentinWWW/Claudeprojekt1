"""Tests fuer den Nutzerwunsch "zu JEDEM Alert eine passende Depot-Position": bei
endlichem Kapital laesst sich das nicht immer erfuellen, aber es darf keine STILLE
Luecke geben. Sichert ab, dass open_positions_for_alert in jedem Fall eine Telegram-
Meldung erzeugt - entweder die Eroeffnung oder eine Begruendung, warum nichts
eroeffnet wurde. Vorher verschwanden diese Faelle kommentarlos im Server-Log, und ein
Alert ohne jede Depot-Reaktion sah fuer den Nutzer wie ein Fehler aus. Kein Netz
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
    os.environ["PAPER_TRADING"] = "true"
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.5"
    os.environ["PAPER_STARTING_CAPITAL"] = "500"
    for k, v in env.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)
    import app.paper_trading as pt
    importlib.reload(pt)
    return config, db, pt


def _cls(db, ticker="AAA", direction="long"):
    return db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.95,
        ticker_calls=[{"ticker": ticker, "direction": direction, "confidence": 0.95}],
    )


def _capture(pt):
    """Haengt sich an den Telegram-Versand, um die erzeugten Nachrichten zu pruefen."""
    sent = []

    async def fake_send(text, **kwargs):
        sent.append(text)
        return True

    import app.telegram_alert as tg
    tg.send_text = fake_send
    return sent


def _quote(pt, value):
    async def q(ticker):
        return value
    pt.prices.get_quote = q


def test_kein_kurs_meldet_begruendung():
    config, db, pt = _fresh()
    sent = _capture(pt)
    _quote(pt, None)
    asyncio.run(pt.open_positions_for_alert(_cls(db), 1, 80))
    check("kein Kurs -> es kommt ueberhaupt eine Meldung", len(sent) == 1)
    check("kein Kurs -> Meldung nennt den Grund",
          sent and "kein Kurs abrufbar" in sent[0])


def test_eroeffnung_erzeugt_keine_doppelmeldung():
    config, db, pt = _fresh()
    sent = _capture(pt)
    _quote(pt, {"price": 100.0, "high": 102.0, "low": 98.0})
    asyncio.run(pt.open_positions_for_alert(_cls(db), 1, 80))
    check("Eroeffnung -> genau EINE Meldung (keine zusaetzliche Begruendung)",
          len(sent) == 1)
    check("Eroeffnung -> Meldung ist die Eroeffnung", sent and "eröffnet" in sent[0])


def test_gleiche_richtung_meldet_statt_zu_schweigen():
    config, db, pt = _fresh()
    _capture(pt)
    _quote(pt, {"price": 100.0, "high": 102.0, "low": 98.0})
    asyncio.run(pt.open_positions_for_alert(_cls(db), 1, 80))

    sent = _capture(pt)
    asyncio.run(pt.open_positions_for_alert(_cls(db), 2, 80))
    check("laufende Position, gleiche Richtung -> Meldung statt Stille", len(sent) == 1)
    check("laufende Position -> Grund wird genannt",
          sent and "läuft bereits" in sent[0])
    check("laufende Position -> es wird NICHT aufgestockt",
          sent and "eröffnet" not in sent[0])


def test_kein_freies_kapital_meldet_begruendung():
    # Winziges Startkapital: schon die erste Position kann den Mindesteinsatz reissen.
    config, db, pt = _fresh(PAPER_STARTING_CAPITAL="12", PAPER_MIN_STAKE="10")
    _capture(pt)
    _quote(pt, {"price": 100.0, "high": 102.0, "low": 98.0})
    # Erste Position bindet fast alles (12€ * max. 15% liegt unter dem Mindesteinsatz,
    # daher greift hier direkt der Kapital-Pfad).
    sent = _capture(pt)
    asyncio.run(pt.open_positions_for_alert(_cls(db, ticker="BBB"), 1, 80))
    check("zu wenig Kapital -> Meldung statt Stille", len(sent) == 1)
    check("zu wenig Kapital -> Grund nennt das freie Kapital",
          sent and "zu wenig freies Kapital" in sent[0])


def test_mehrere_ticker_teilerfolg_meldet_nur_eroeffnung():
    """Wird wenigstens EIN Ticker eroeffnet, ist die Depot-Reaktion sichtbar - eine
    zusaetzliche 'aber Ticker X nicht'-Nachricht waere blosses Rauschen."""
    config, db, pt = _fresh()
    sent = _capture(pt)

    async def q(ticker):
        return {"price": 100.0, "high": 102.0, "low": 98.0} if ticker == "AAA" else None
    pt.prices.get_quote = q

    cls = db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.95,
        ticker_calls=[
            {"ticker": "AAA", "direction": "long", "confidence": 0.95},
            {"ticker": "ZZZ", "direction": "long", "confidence": 0.95},
        ],
    )
    asyncio.run(pt.open_positions_for_alert(cls, 1, 80))
    check("Teilerfolg -> nur die Eroeffnung, keine Begruendungs-Nachricht",
          len(sent) == 1 and "eröffnet" in sent[0])


def main():
    test_kein_kurs_meldet_begruendung()
    test_eroeffnung_erzeugt_keine_doppelmeldung()
    test_gleiche_richtung_meldet_statt_zu_schweigen()
    test_kein_freies_kapital_meldet_begruendung()
    test_mehrere_ticker_teilerfolg_meldet_nur_eroeffnung()

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
