"""Tests fuer den Kurs-Parser (#2/#8) - reine Logik, kein Netz."""
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


def main():
    from app.prices import parse_stooq_csv, to_stooq_symbol

    check("US-Symbol", to_stooq_symbol("AAPL") == "aapl.us")
    check("Klassensuffix (BRK.B -> brk-b.us)", to_stooq_symbol("BRK.B") == "brk-b.us")
    check("Whitespace/Kleinschreibung", to_stooq_symbol("  nvda ") == "nvda.us")

    csv = ("Symbol,Date,Time,Open,High,Low,Close,Volume\n"
           "AAPL.US,2026-07-13,22:00:00,200,210,199,206,1000000")
    q = parse_stooq_csv(csv)
    check("Preis (Close) geparst", q and q["price"] == 206.0)
    check("Open geparst", q and q["open"] == 200.0)
    check("change_pct = (206-200)/200 = 3%", q and abs(q["change_pct"] - 3.0) < 1e-9)

    # Ohne Open kann keine Tagesbewegung berechnet werden -> change_pct None, Preis bleibt.
    csv_no_open = ("Symbol,Date,Time,Open,High,Low,Close,Volume\n"
                   "X.US,2026-07-13,22:00:00,N/D,N/D,N/D,150,10")
    q2 = parse_stooq_csv(csv_no_open)
    check("Preis ohne Open trotzdem da", q2 and q2["price"] == 150.0)
    check("change_pct ohne Open ist None", q2 and q2["change_pct"] is None)

    # Komplett N/D (unbekanntes Symbol / kein Handel) -> None.
    csv_nd = ("Symbol,Date,Time,Open,High,Low,Close,Volume\n"
              "X.US,N/D,N/D,N/D,N/D,N/D,N/D,N/D")
    check("komplett N/D -> None", parse_stooq_csv(csv_nd) is None)

    check("leerer Text -> None", parse_stooq_csv("") is None)
    check("nur Header -> None", parse_stooq_csv("Symbol,Date,Time,Open,High,Low,Close,Volume") is None)
    check("Nicht-String -> None", parse_stooq_csv(None) is None)
    check("Spaltenzahl passt nicht -> None",
          parse_stooq_csv("Symbol,Close\nAAPL.US,1,2,3") is None)

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
