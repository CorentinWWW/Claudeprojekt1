"""Tests fuer die robustere Ticker-Erkennung: Normalisierung, Validierung und
Deduplizierung der von Claude gelieferten ticker_calls.

Damit ein Modell nicht versehentlich Muell ('NASDAQ:NVDA', 'Nvidia', '$AAPL',
'TBD', ganze Firmennamen) als vermeintliches Boersenkuerzel an den Nutzer
schickt, werden alle Ticker vor der Anzeige durch _normalize_ticker /
_clean_ticker_calls gejagt.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

from app.classifier import _clean_ticker_calls, _normalize_ticker

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def main():
    # --- _normalize_ticker: gueltige Kuerzel bleiben erhalten / werden gesaeubert ---
    check("Sauberes Kuerzel bleibt", _normalize_ticker("NVDA") == "NVDA")
    check("Kleinschreibung wird zu Grossbuchstaben", _normalize_ticker("nvda") == "NVDA")
    check("Cashtag-$ wird entfernt", _normalize_ticker("$AAPL") == "AAPL")
    check("Boersen-Prefix wird entfernt", _normalize_ticker("NASDAQ:NVDA") == "NVDA")
    check("Boersen-Prefix mit Leerzeichen", _normalize_ticker("NYSE: XOM") == "XOM")
    check("Klassen-Suffix mit Punkt bleibt", _normalize_ticker("BRK.B") == "BRK.B")
    check("Klassen-Suffix mit Bindestrich bleibt", _normalize_ticker("BRK-B") == "BRK-B")
    check("Umgebende Leerzeichen werden getrimmt", _normalize_ticker("  T  ") == "T")
    check("Einzelbuchstaben-Ticker (z.B. Ford=F)", _normalize_ticker("F") == "F")

    # --- _normalize_ticker: Muell/Platzhalter -> None (Eintrag wird verworfen) ---
    check("Ganzer Firmenname wird verworfen", _normalize_ticker("Nvidia") is None)
    check("Satz wird verworfen", _normalize_ticker("Apple Inc.") is None)
    check("Zu langes 'Kuerzel' wird verworfen", _normalize_ticker("TOOLONG") is None)
    check("Platzhalter TBD wird verworfen", _normalize_ticker("TBD") is None)
    check("Platzhalter TBA wird verworfen", _normalize_ticker("TBA") is None)
    check("Platzhalter N/A-artig (NA) wird verworfen", _normalize_ticker("NA") is None)
    check("Platzhalter NONE wird verworfen", _normalize_ticker("none") is None)
    check("Platzhalter UNKNOWN wird verworfen", _normalize_ticker("UNKNOWN") is None)
    check("Leerer String wird verworfen", _normalize_ticker("") is None)
    check("Nur-Leerzeichen wird verworfen", _normalize_ticker("   ") is None)
    check("Nicht-String (None) wird verworfen", _normalize_ticker(None) is None)
    check("Nicht-String (Zahl) wird verworfen", _normalize_ticker(123) is None)

    # --- _clean_ticker_calls: Struktur, Defaults, Clamping ---
    cleaned = _clean_ticker_calls(
        [{"ticker": "nvda", "direction": "long", "confidence": 0.8, "reasoning": "KI-Boom"}]
    )
    check("Ein sauberer Eintrag bleibt erhalten", len(cleaned) == 1)
    check("Ticker wird normalisiert (nvda->NVDA)", cleaned and cleaned[0]["ticker"] == "NVDA")
    check("Direction wird uebernommen", cleaned and cleaned[0]["direction"] == "long")
    check("Confidence wird uebernommen", cleaned and cleaned[0]["confidence"] == 0.8)
    check("Reasoning wird uebernommen", cleaned and cleaned[0]["reasoning"] == "KI-Boom")

    # Fehlende direction -> Default 'long'; fehlende confidence/reasoning -> Defaults.
    cleaned = _clean_ticker_calls([{"ticker": "AAPL"}])
    check("Fehlende direction -> 'long'", cleaned and cleaned[0]["direction"] == "long")
    check("Fehlende confidence -> 0.0", cleaned and cleaned[0]["confidence"] == 0.0)
    check("Fehlendes reasoning -> ''", cleaned and cleaned[0]["reasoning"] == "")

    # Confidence ausserhalb [0,1] bzw. nicht-numerisch wird geclamped/aufgefangen.
    cleaned = _clean_ticker_calls(
        [
            {"ticker": "AAPL", "confidence": 1.5},
            {"ticker": "MSFT", "confidence": -0.3},
            {"ticker": "TSLA", "confidence": "nan-ish"},
            {"ticker": "AMZN", "confidence": None},
        ]
    )
    by = {c["ticker"]: c["confidence"] for c in cleaned}
    check("Confidence > 1 wird auf 1.0 geklemmt", by.get("AAPL") == 1.0)
    check("Confidence < 0 wird auf 0.0 geklemmt", by.get("MSFT") == 0.0)
    check("Nicht-numerische Confidence -> 0.0", by.get("TSLA") == 0.0)
    check("None-Confidence -> 0.0", by.get("AMZN") == 0.0)

    # --- _clean_ticker_calls: Muell wird gefiltert ---
    cleaned = _clean_ticker_calls(
        [
            {"ticker": "NVDA", "confidence": 0.7},
            {"ticker": "Nvidia Corporation", "confidence": 0.9},  # kein Kuerzel
            {"ticker": "TBD", "confidence": 0.9},                 # Platzhalter
            "garbage-string",                                      # kein dict
            {"no_ticker_field": True},                            # fehlendes Feld
            None,                                                 # kein dict
        ]
    )
    tickers = [c["ticker"] for c in cleaned]
    check("Nur der eine gueltige Ticker ueberlebt das Filtern", tickers == ["NVDA"])

    # --- _clean_ticker_calls: Dedup nach hoechster Confidence ---
    cleaned = _clean_ticker_calls(
        [
            {"ticker": "nvda", "direction": "long", "confidence": 0.5, "reasoning": "a"},
            {"ticker": "NASDAQ:NVDA", "direction": "short", "confidence": 0.9, "reasoning": "b"},
            {"ticker": "$NVDA", "direction": "long", "confidence": 0.2, "reasoning": "c"},
            {"ticker": "XOM", "direction": "short", "confidence": 0.6, "reasoning": "d"},
        ]
    )
    by = {c["ticker"]: c for c in cleaned}
    check("Nach Dedup nur 2 eindeutige Ticker", len(cleaned) == 2)
    check("NVDA behaelt den Eintrag mit hoechster Confidence (0.9)", by["NVDA"]["confidence"] == 0.9)
    check("NVDA-Gewinner behaelt seine direction (short)", by["NVDA"]["direction"] == "short")
    check("NVDA-Gewinner behaelt sein reasoning (b)", by["NVDA"]["reasoning"] == "b")
    check("XOM bleibt unveraendert erhalten", by["XOM"]["confidence"] == 0.6)

    # --- Randfaelle: leere/ungueltige Eingaben crashen nicht ---
    check("Leere Liste -> leere Liste", _clean_ticker_calls([]) == [])
    check("Nicht-iterierbares faellt nicht auf die Nase", isinstance(_clean_ticker_calls([None, 5, "x"]), list))

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
