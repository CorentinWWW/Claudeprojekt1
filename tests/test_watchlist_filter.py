"""Tests fuer den persoenlichen Watchlist/Blocklist-Filter (#10) und die gemeinsame
actionable_tickers-Definition."""
import importlib
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


def _reload(watchlist="", sectors="", blocklist=""):
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["ALERT_CONFIDENCE_THRESHOLD"] = "0.5"
    os.environ["WATCHLIST_TICKERS"] = watchlist
    os.environ["WATCHLIST_SECTORS"] = sectors
    os.environ["BLOCKLIST_TICKERS"] = blocklist
    import app.config as config
    importlib.reload(config)
    import app.orchestrator as orch
    importlib.reload(orch)
    return orch


def _cls(orch_db, tickers, sectors=None):
    from app.db import Classification
    return Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                          ticker_calls=tickers, sectors=sectors or [])


def main():
    # --- Kein Filter: strenger Ticker reicht ---
    orch = _reload()
    from app.db import Classification
    strong = Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                            ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    check("ohne Watchlist: starker Ticker ist alarmwuerdig", orch.is_alert_worthy(strong) is True)

    # --- Blocklist entfernt Ticker aus der Bewertung ---
    orch = _reload(blocklist="NVDA")
    blocked = Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                             ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    check("Blocklist: geblockter Ticker -> keine handelbaren Ticker -> nicht alarmwuerdig",
          orch.is_alert_worthy(blocked) is False)
    check("Blocklist: actionable_tickers ist leer", orch.actionable_tickers(blocked) == [])

    # --- Watchlist-Ticker: nur passende lösen aus ---
    orch = _reload(watchlist="NVDA,AMD")
    on_list = Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                             ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    off_list = Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                              ticker_calls=[{"ticker": "XOM", "direction": "short", "confidence": 0.95}])
    check("Watchlist: Ticker auf der Liste -> alarmwuerdig", orch.is_alert_worthy(on_list) is True)
    check("Watchlist: Ticker NICHT auf der Liste -> nicht alarmwuerdig",
          orch.is_alert_worthy(off_list) is False)

    # --- Watchlist-Sektor: passender Sektor lässt auch fremde Ticker durch ---
    orch = _reload(sectors="halbleiter")
    sector_hit = Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                                ticker_calls=[{"ticker": "XOM", "direction": "short", "confidence": 0.95}],
                                sectors=["Halbleiter", "Technologie"])
    sector_miss = Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                                 ticker_calls=[{"ticker": "XOM", "direction": "short", "confidence": 0.95}],
                                 sectors=["Energie"])
    check("Watchlist-Sektor: passender Sektor -> alarmwuerdig", orch.is_alert_worthy(sector_hit) is True)
    check("Watchlist-Sektor: unpassender Sektor -> nicht alarmwuerdig",
          orch.is_alert_worthy(sector_miss) is False)

    # --- actionable_tickers filtert schwache Ticker + Richtung ---
    orch = _reload()
    mixed = Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                           ticker_calls=[
                               {"ticker": "NVDA", "direction": "long", "confidence": 0.95},
                               {"ticker": "INTC", "direction": "long", "confidence": 0.6},
                               {"ticker": "AMD", "direction": None, "confidence": 0.99},
                           ])
    act = [t["ticker"] for t in orch.actionable_tickers(mixed)]
    check("actionable_tickers: nur der starke, gerichtete Ticker", act == ["NVDA"])

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
