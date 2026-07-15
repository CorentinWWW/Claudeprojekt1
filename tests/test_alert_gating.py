"""Tests fuer den strengen Alarm-Filter: es wird NUR noch alarmiert, wenn eine
konkrete Aktie mit klarer Long/Short-Richtung und sehr hoher Pro-Ticker-Konfidenz
betroffen ist (is_alert_worthy). Ausserdem: der Resend-Pfad darf die Schwelle nicht
umgehen.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def _fresh_modules(min_ticker_conf="0.85"):
    import importlib
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = min_ticker_conf
    os.environ["ALERT_CONFIDENCE_THRESHOLD"] = "0.5"
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)
    return tmp.name, db, orch


def _cls(db, *, relevant=True, confidence=0.9, tickers=None):
    return db.Classification(
        is_market_relevant=relevant,
        sentiment="negative",
        confidence=confidence,
        ticker_calls=tickers if tickers is not None else [],
    )


def test_is_alert_worthy():
    tmp_name, db, orch = _fresh_modules()
    iaw = orch.is_alert_worthy

    check("None -> nicht alarmwuerdig", iaw(None) is False)
    check("nicht marktrelevant -> nicht alarmwuerdig",
          iaw(_cls(db, relevant=False, tickers=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])) is False)
    check("marktrelevant aber KEIN Ticker -> nicht alarmwuerdig",
          iaw(_cls(db, tickers=[])) is False)
    check("Ticker unter Schwelle (0.7 < 0.85) -> nicht alarmwuerdig",
          iaw(_cls(db, tickers=[{"ticker": "NVDA", "direction": "long", "confidence": 0.7}])) is False)
    check("Ticker genau auf Schwelle (0.85) -> alarmwuerdig",
          iaw(_cls(db, tickers=[{"ticker": "NVDA", "direction": "long", "confidence": 0.85}])) is True)
    check("Ticker ueber Schwelle, long -> alarmwuerdig",
          iaw(_cls(db, tickers=[{"ticker": "NVDA", "direction": "long", "confidence": 0.92}])) is True)
    check("Ticker ueber Schwelle, short -> alarmwuerdig",
          iaw(_cls(db, tickers=[{"ticker": "TSLA", "direction": "short", "confidence": 0.9}])) is True)

    # Hohe Ticker-Konfidenz, aber ohne klare Richtung -> keine handelbare Aussage.
    check("hohe Konfidenz aber direction=None -> nicht alarmwuerdig",
          iaw(_cls(db, tickers=[{"ticker": "NVDA", "direction": None, "confidence": 0.99}])) is False)
    check("hohe Konfidenz aber unbekannte direction -> nicht alarmwuerdig",
          iaw(_cls(db, tickers=[{"ticker": "NVDA", "direction": "hold", "confidence": 0.99}])) is False)

    # Alte DB-Zeile ohne confidence-Feld (None) -> zaehlt nicht als sicher.
    check("Ticker mit confidence=None (alte Zeile) -> nicht alarmwuerdig",
          iaw(_cls(db, tickers=[{"ticker": "NVDA", "direction": "long", "confidence": None}])) is False)

    # Gesamt-Konfidenz unter ALERT_CONFIDENCE_THRESHOLD blockt trotz starkem Ticker.
    check("Gesamt-Konfidenz < ALERT_CONFIDENCE_THRESHOLD -> nicht alarmwuerdig",
          iaw(_cls(db, confidence=0.3, tickers=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])) is False)

    # Von mehreren Tickern zaehlt der staerkste: ein schwacher + ein starker -> alarmwuerdig.
    check("staerkster von mehreren Tickern entscheidet",
          iaw(_cls(db, tickers=[
              {"ticker": "XOM", "direction": "short", "confidence": 0.4},
              {"ticker": "NVDA", "direction": "long", "confidence": 0.9},
          ])) is True)

    # Muell-Eintraege (kein dict) crashen nicht.
    check("Muell im ticker_calls crasht nicht",
          iaw(_cls(db, tickers=["garbage", None, {"ticker": "NVDA", "direction": "long", "confidence": 0.9}])) is True)

    os.unlink(tmp_name)


def test_min_ticker_confidence_zero_restores_old_behavior():
    """ALERT_MIN_TICKER_CONFIDENCE=0 -> Filter aus: jede marktrelevante Meldung
    oberhalb von ALERT_CONFIDENCE_THRESHOLD ist wieder alarmwuerdig, auch ohne Ticker."""
    tmp_name, db, orch = _fresh_modules(min_ticker_conf="0")
    iaw = orch.is_alert_worthy
    check("Filter aus: marktrelevant ohne Ticker -> alarmwuerdig",
          iaw(_cls(db, tickers=[])) is True)
    check("Filter aus: aber Gesamt-Konfidenz-Schwelle gilt weiter",
          iaw(_cls(db, confidence=0.3, tickers=[])) is False)
    os.unlink(tmp_name)


def test_resend_path_respects_threshold():
    """Der Resend-Pfad (get_pending_alerts) darf nur wirklich alarmwuerdige Meldungen
    nachschicken - sonst waere die strenge Schwelle einen Zyklus spaeter wirkungslos."""
    tmp_name, db, orch = _fresh_modules()

    worthy = db.RawStatement(source="test", source_id="worthy-1", text="Trump verhaengt Zoelle auf Chips")
    db.insert_statement(worthy, _cls(db, tickers=[{"ticker": "NVDA", "direction": "short", "confidence": 0.92}]))

    unworthy = db.RawStatement(source="test", source_id="unworthy-1", text="Allgemeine Wirtschaftsrhetorik")
    db.insert_statement(unworthy, _cls(db, tickers=[{"ticker": "SPY", "direction": "long", "confidence": 0.4}]))

    no_ticker = db.RawStatement(source="test", source_id="noticker-1", text="Marktrelevant aber ohne konkrete Aktie")
    db.insert_statement(no_ticker, _cls(db, tickers=[]))

    sent = []

    async def fake_send_alert(raw, classification):
        sent.append(raw.source_id)
        return True

    orig = orch.send_alert
    orch.send_alert = fake_send_alert
    try:
        asyncio.run(orch._resend_pending_alerts())
    finally:
        orch.send_alert = orig

    check("Resend: nur die handelbare Meldung wird nachgeschickt", sent == ["worthy-1"])
    check("Resend: schwacher Ticker wird NICHT nachgeschickt", "unworthy-1" not in sent)
    check("Resend: Meldung ohne Ticker wird NICHT nachgeschickt", "noticker-1" not in sent)

    # Die nachgeschickte Meldung ist jetzt als alarmiert markiert, die anderen nicht.
    pending_after = {row["source_id"] for row in db.get_pending_alerts()}
    check("Resend: nachgeschickte Meldung ist als alarmiert markiert", "worthy-1" not in pending_after)
    check("Resend: nicht-alarmwuerdige bleiben unmarkiert (kein falsches alert_sent)",
          {"unworthy-1", "noticker-1"} <= pending_after)

    os.unlink(tmp_name)


def main():
    test_is_alert_worthy()
    test_min_ticker_confidence_zero_restores_old_behavior()
    test_resend_path_respects_threshold()

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
