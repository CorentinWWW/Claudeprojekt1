"""Tests fuer die US-Boersen-Session-Erkennung (#7)."""
import calendar
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def _utc(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M"))


def main():
    from app.market_hours import session_label, us_market_session

    # 2026-07-13 ist ein Montag, 2026-07-11 ein Samstag (Sommerzeit -> EDT = UTC-4).
    check("regulaer offen (Mo 10:00 ET)", us_market_session(_utc("2026-07-13 14:00")) == "open")
    check("vorboerslich (Mo 08:00 ET)", us_market_session(_utc("2026-07-13 12:00")) == "pre")
    check("nachboerslich (Mo 19:00 ET)", us_market_session(_utc("2026-07-13 23:00")) == "after")
    check("geschlossen (Mo 02:00 ET)", us_market_session(_utc("2026-07-13 06:00")) == "closed")
    check("Wochenende (Sa)", us_market_session(_utc("2026-07-11 18:00")) == "weekend")

    check("Label fuer open ist nicht leer", session_label("open") != "")
    check("Label fuer unbekannt ist leer", session_label("unknown") == "")

    # Aufruf ohne Argument (jetzt) darf nicht crashen und liefert einen bekannten Wert.
    now_session = us_market_session()
    check("Aufruf ohne ts liefert gueltige Session",
          now_session in {"open", "pre", "after", "closed", "weekend", "unknown"})

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
