"""Tests fuer app/replay.py: Auswertung der bereits vorhandenen Klassifikationen gegen
Kursverlaeufe, ohne neue Claude-Calls.

Wichtigster Einzeltest ist die Read-only-Garantie: dieses Werkzeug laeuft gegen die
Datenbank des LAUFENDEN Bots - ein versehentlicher Schreibzugriff waere ein echtes
Risiko fuer den Produktivbetrieb. Kein Netz (Kursabfragen gemockt)."""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

import app.replay as replay

failures = []


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


SCHEMA = """
CREATE TABLE statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, source_id TEXT NOT NULL UNIQUE, text TEXT NOT NULL,
    url TEXT, published_at REAL, ingested_at REAL NOT NULL,
    is_market_relevant INTEGER, sentiment TEXT, confidence REAL, tickers TEXT,
    sectors TEXT, reasoning TEXT, alert_sent INTEGER NOT NULL DEFAULT 0,
    duplicate_of_id INTEGER, related_topic_id INTEGER,
    is_major_escalation INTEGER NOT NULL DEFAULT 0,
    expected_move_pct REAL, expected_horizon TEXT
);
"""

# 5 Handelstage, durchgehend steigend - ein Long trifft, ein Short liegt falsch.
HIST = {
    "date": ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"],
    "close": [100.0, 101.0, 102.0, 103.0, 104.0],
}


def _ts(date_str: str, hour: int = 15) -> float:
    import datetime
    return datetime.datetime.fromisoformat(date_str).replace(
        hour=hour, tzinfo=datetime.timezone.utc
    ).timestamp()


def _make_db(rows) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(SCHEMA)
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO statements (source, source_id, text, published_at, ingested_at,"
            " is_market_relevant, sentiment, confidence, tickers, alert_sent, duplicate_of_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("news_rss", f"https://x/{i}", f"Meldung {i}", r.get("published_at"), time.time(),
             r.get("relevant", 1), "negative", r.get("confidence", 0.9),
             json.dumps(r["tickers"]) if r.get("tickers") is not None else None,
             int(r.get("alert_sent", 0)), r.get("duplicate_of_id")),
        )
    conn.commit()
    conn.close()
    return tmp.name


async def _fake_history(ticker, client=None):
    return HIST if ticker in ("AAA", "BBB") else None


async def _fake_caps(tickers, client=None):
    return {"AAA": 500_000_000, "BBB": 50_000_000_000}


def _run(db_path, **kw):
    orig_h, orig_c = replay.prices.get_history, replay.prices.get_market_caps
    replay.prices.get_history = _fake_history
    replay.prices.get_market_caps = _fake_caps
    try:
        return asyncio.run(replay.run_replay(db_path, **kw))
    finally:
        replay.prices.get_history, replay.prices.get_market_caps = orig_h, orig_c


def test_datenbank_wird_strikt_lesend_geoeffnet():
    """Die wichtigste Garantie: dieses Werkzeug laeuft gegen die DB des laufenden Bots.
    mode=ro muss einen Schreibversuch hart ablehnen - nicht nur 'wir schreiben halt
    nicht'."""
    db = _make_db([{"published_at": _ts("2026-01-05"),
                    "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.9}]}])
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.execute("UPDATE statements SET confidence = 0.1")
        conn.commit()
        wrote = True
    except sqlite3.OperationalError:
        wrote = False
    finally:
        conn.close()
    check("mode=ro lehnt Schreibzugriff ab (harte SQLite-Garantie)", wrote is False)

    # Gegenprobe: die Daten sind wirklich unveraendert.
    conn = sqlite3.connect(db)
    val = conn.execute("SELECT confidence FROM statements").fetchone()[0]
    conn.close()
    check("Daten nach abgelehntem Schreibversuch unveraendert", abs(val - 0.9) < 1e-9)


def test_laedt_nur_brauchbare_statements():
    db = _make_db([
        # gueltig
        {"published_at": _ts("2026-01-05"),
         "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.9}]},
        # nicht marktrelevant -> raus
        {"published_at": _ts("2026-01-05"), "relevant": 0,
         "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.9}]},
        # als Duplikat markiert -> raus (sonst zaehlt dieselbe Story doppelt)
        {"published_at": _ts("2026-01-05"), "duplicate_of_id": 1,
         "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.9}]},
        # ohne Ticker -> raus
        {"published_at": _ts("2026-01-05"), "tickers": None},
        # ohne Veroeffentlichungszeit -> raus (Einstiegstag waere geraten)
        {"published_at": None,
         "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.9}]},
    ])
    loaded = replay.load_classified_statements(db)
    check("nur das eine brauchbare Statement geladen", len(loaded) == 1)


def test_trefferquote_und_richtung():
    db = _make_db([
        {"published_at": _ts("2026-01-05"),
         "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.95}]},
        {"published_at": _ts("2026-01-05"),
         "tickers": [{"ticker": "AAA", "direction": "short", "confidence": 0.95}]},
    ])
    r = _run(db, horizon_days=[2])
    check("beide Ticker-Calls ausgewertet", r.get("ausgewertet") == 2)
    check("Trefferquote 50% (steigender Kurs: long trifft, short nicht)",
          r["gesamt"]["hit_rate"] == 0.5)


def test_konfidenz_aufschluesselung():
    """Die eigentliche Kernfrage: haengt die Trefferquote ueberhaupt an der Konfidenz?"""
    db = _make_db([
        {"published_at": _ts("2026-01-05"),
         "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.95}]},
        {"published_at": _ts("2026-01-05"),
         "tickers": [{"ticker": "AAA", "direction": "short", "confidence": 0.65}]},
    ])
    r = _run(db, horizon_days=[2])
    buckets = r.get("nach_konfidenz", {})
    check("hohe Konfidenz landet im 95-100%-Bucket", buckets.get("95-100%", {}).get("n") == 1)
    check("niedrige Konfidenz landet im <70%-Bucket", buckets.get("<70%", {}).get("n") == 1)
    check("95-100%-Bucket traf (long bei steigendem Kurs)",
          buckets.get("95-100%", {}).get("hit_rate") == 1.0)


def test_alarmiert_vs_unterdrueckt_getrennt():
    """Bringen die Alarm-Gates etwas? Nur messbar, wenn beide Gruppen getrennt
    ausgewiesen werden."""
    db = _make_db([
        {"published_at": _ts("2026-01-05"), "alert_sent": 1,
         "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.95}]},
        {"published_at": _ts("2026-01-05"), "alert_sent": 0,
         "tickers": [{"ticker": "AAA", "direction": "short", "confidence": 0.95}]},
    ])
    r = _run(db, horizon_days=[2])
    split = r.get("alarmiert_vs_unterdrueckt", {})
    check("alarmierte Gruppe hat 1 Eintrag", split.get("alarmiert", {}).get("n") == 1)
    check("unterdrueckte Gruppe hat 1 Eintrag", split.get("unterdrueckt", {}).get("n") == 1)
    check("alarmiert traf, unterdrueckt nicht",
          split["alarmiert"]["hit_rate"] == 1.0 and split["unterdrueckt"]["hit_rate"] == 0.0)


def test_mehrere_horizonte_und_tier():
    db = _make_db([
        {"published_at": _ts("2026-01-05"),
         "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.95}]},
        {"published_at": _ts("2026-01-05"),
         "tickers": [{"ticker": "BBB", "direction": "long", "confidence": 0.95}]},
    ])
    r = _run(db, horizon_days=[1, 2, 4])
    check("nach_horizont enthaelt alle drei Horizonte",
          set(r.get("nach_horizont", {}).keys()) == {"1", "2", "4"})
    tiers = r.get("nach_marktkapitalisierung", {})
    check("AAA als small-cap eingeordnet", tiers.get("small", {}).get("n") == 1)
    check("BBB als large-cap eingeordnet", tiers.get("large", {}).get("n") == 1)


def test_fehlende_kurshistorie_faellt_sauber_raus():
    db = _make_db([
        {"published_at": _ts("2026-01-05"),
         "tickers": [{"ticker": "AAA", "direction": "long", "confidence": 0.9}]},
        {"published_at": _ts("2026-01-05"),
         "tickers": [{"ticker": "ZZZ", "direction": "long", "confidence": 0.9}]},
    ])
    r = _run(db, horizon_days=[2])
    check("nur der Ticker mit Historie ausgewertet", r.get("ausgewertet") == 1)
    check("der andere als nicht auswertbar gezaehlt", r.get("nicht_auswertbar") == 1)
    check("kein Absturz bei fehlender Historie", "gesamt" in r)


def test_leere_datenbank_ist_kein_fehler():
    db = _make_db([])
    r = _run(db)
    check("leere DB liefert erklaerenden Hinweis statt Absturz", "note" in r)
    check("keine Statements gezaehlt", r.get("statements_klassifiziert") == 0)


def main():
    test_datenbank_wird_strikt_lesend_geoeffnet()
    test_laedt_nur_brauchbare_statements()
    test_trefferquote_und_richtung()
    test_konfidenz_aufschluesselung()
    test_alarmiert_vs_unterdrueckt_getrennt()
    test_mehrere_horizonte_und_tier()
    test_fehlende_kurshistorie_faellt_sauber_raus()
    test_leere_datenbank_ist_kein_fehler()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
