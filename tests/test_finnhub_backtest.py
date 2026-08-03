"""Tests fuer die historische Finnhub-Quelle und deren Einbindung in app/backtest.py.

Finnhub wurde als ZWEITE historische Quelle noetig, weil Alpha Vantages Gratis-Tarif
nur ~25 Anfragen/TAG erlaubt - bei mehreren Testlaeufen am selben Tag schnell
aufgebraucht (live erlebt). Finnhubs Gratis-Tarif erlaubt stattdessen ~60/MINUTE, dafuer
ist company-news NUR pro Ticker abrufbar, nicht marktweit (siehe news_finnhub.py-Docstring).
Zwei Dinge stehen hier im Mittelpunkt:

1. Der Parser/Fehlerpfad: Finnhub liefert bei Erfolg eine JSON-LISTE direkt (kein
   umschliessendes Objekt wie Alpha Vantages {'feed': [...]}) - eine unerwartete
   Antwortform (z.B. ein Fehlerobjekt) darf NICHT still als "keine Artikel" durchgehen.

2. Die Einbindung in run_backtest(): --source finnhub muss tatsaechlich
   fetch_all_raw_finnhub() mit der richtigen Tickerliste aufrufen und die genutzte
   Liste offen im Report ausweisen (Auswahl-Risiko, keine Verschleierung).
"""
import asyncio
import datetime
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

import app.backtest as bt
import app.sources.news_finnhub as fh
from app.db import RawStatement

failures = []


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _tmp_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return tmp.name


# --- Finnhub-Parser ---------------------------------------------------------------
def test_feed_parsing():
    data = [
        {"headline": "Fed hikes rates", "url": "https://x/1",
         "datetime": 1752586200, "source": "Reuters"},
        # ohne datetime -> kein verlaesslicher Einstiegstag
        {"headline": "Ohne Zeit", "url": "https://x/2", "source": "AP"},
        # ohne Headline -> nichts zu klassifizieren
        {"headline": "", "url": "https://x/3", "datetime": 1752586300},
        # datetime <= 0 -> ungueltig
        {"headline": "Kaputtes Datum", "url": "https://x/4", "datetime": 0},
    ]
    out = fh.parse_feed(data)
    check("nur der vollstaendige Artikel wird uebernommen", len(out) == 1)
    check("Text enthaelt Headline und Quelle (gleiches Format wie die anderen Quellen)",
          out[0].text == "Fed hikes rates (Quelle: Reuters)")
    check("source korrekt gesetzt", out[0].source == "news_finnhub")
    check("published_at korrekt uebernommen (bereits Unix-Sekunden, kein Parsing noetig)",
          out[0].published_at == 1752586200.0)


def test_nicht_liste_liefert_leer():
    """parse_feed() ist eine reine Hilfsfunktion - ein Fehlerobjekt statt Liste soll
    hier NICHT abstuerzen, das Werfen uebernimmt fetch_ticker_range()."""
    check("dict statt Liste -> leeres Ergebnis, kein Absturz", fh.parse_feed({"error": "x"}) == [])
    check("None -> leeres Ergebnis", fh.parse_feed(None) == [])


def test_fehlender_key_wirft_sofort():
    orig = fh.FINNHUB_API_KEY
    fh.FINNHUB_API_KEY = ""
    try:
        raised = False
        try:
            asyncio.run(fh.fetch_ticker_range(
                "AAPL", datetime.date(2026, 1, 1), datetime.date(2026, 1, 2),
            ))
        except fh.FinnhubError as exc:
            raised = "FINNHUB_API_KEY" in str(exc)
        check("ohne Key: klare Fehlermeldung statt stiller Leerlauf", raised)
    finally:
        fh.FINNHUB_API_KEY = orig


def test_unerwartete_antwortform_wirft():
    """Ein Fehlerobjekt (z.B. bei ungueltigem Key/Symbol) darf NICHT als 'keine
    Artikel' durchgehen - das waere exakt die stille Verwechslung, die beim
    GDELT-Rate-Limit einen ganzen Tag gekostet hat."""
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, *a, **kw):
            req = __import__("httpx").Request("GET", url)
            return __import__("httpx").Response(200, request=req, json={"error": "invalid symbol"})

    orig = fh.FINNHUB_API_KEY
    fh.FINNHUB_API_KEY = "dummy-key"
    try:
        raised = False
        try:
            asyncio.run(fh.fetch_ticker_range(
                "NOPE", datetime.date(2026, 1, 1), datetime.date(2026, 1, 2),
                client=FakeClient(),
            ))
        except fh.FinnhubError:
            raised = True
        check("Fehlerobjekt statt Liste wird zur Exception (nicht still zu 0 Artikeln)", raised)
    finally:
        fh.FINNHUB_API_KEY = orig


# --- Einbindung in run_backtest() --------------------------------------------------
def test_backtest_nutzt_finnhub_quelle_und_weist_ticker_aus():
    """--source finnhub muss fetch_all_raw_finnhub() mit der richtigen Tickerliste
    aufrufen (Standard- ODER per --tickers uebergebene Liste) und diese offen im
    Report ausweisen - das ist das erklaerte Auswahl-Risiko dieser Quelle."""
    orig_fetch = bt.fetch_all_raw_finnhub
    captured = {}

    async def fake_fetch_all_raw_finnhub(start, end, tickers):
        captured["tickers"] = tickers
        base = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
        stmts = [
            RawStatement(
                source="news_finnhub", source_id=f"https://x/{i}",
                text=f"Marktmeldung {i} ueber Zinsen und Zoelle am Aktienmarkt.",
                published_at=(base + datetime.timedelta(days=i)).timestamp(),
            )
            for i in range(3)
        ]
        return stmts, 0, len(tickers)

    bt.fetch_all_raw_finnhub = fake_fetch_all_raw_finnhub
    try:
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 5, 1), datetime.date(2026, 5, 3),
            source="finnhub", tickers=["NVDA", "AMD"], db_path=_tmp_db(),
        ))
    finally:
        bt.fetch_all_raw_finnhub = orig_fetch

    check("eigene Ticker-Liste wird durchgereicht, nicht der Standard",
          captured.get("tickers") == ["NVDA", "AMD"])
    check("Report weist source='finnhub' aus", report.get("source") == "finnhub")
    check("Report weist die tatsaechlich genutzten Ticker offen aus (kein Verschleiern)",
          report.get("tickers") == ["NVDA", "AMD"])
    check("Rohtreffer wurden uebernommen", report.get("raw_fetched") == 3)


def test_backtest_ohne_tickers_nutzt_default_liste():
    orig_fetch = bt.fetch_all_raw_finnhub
    captured = {}

    async def fake_fetch_all_raw_finnhub(start, end, tickers):
        captured["tickers"] = tickers
        return [], 0, len(tickers)

    bt.fetch_all_raw_finnhub = fake_fetch_all_raw_finnhub
    try:
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 5, 1), datetime.date(2026, 5, 3),
            source="finnhub", db_path=_tmp_db(),
        ))
    finally:
        bt.fetch_all_raw_finnhub = orig_fetch

    check("ohne --tickers wird die Standardliste DEFAULT_FINNHUB_TICKERS genutzt",
          captured.get("tickers") == bt.DEFAULT_FINNHUB_TICKERS.split(","))
    check("Standardliste erscheint ebenfalls offen im Report",
          report.get("tickers") == bt.DEFAULT_FINNHUB_TICKERS.split(","))


def test_andere_quellen_bekommen_kein_tickers_feld():
    """'tickers' im Report soll AUSSCHLIESSLICH bei --source finnhub auftauchen - sonst
    waere es fuer gdelt/alphavantage ein irrefuehrendes, nicht genutztes Feld."""
    orig_fetch = bt.fetch_all_raw

    async def fake_fetch_all_raw(start, end):
        return [], 0, 1

    bt.fetch_all_raw = fake_fetch_all_raw
    try:
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 5, 1), datetime.date(2026, 5, 3),
            source="gdelt", db_path=_tmp_db(),
        ))
    finally:
        bt.fetch_all_raw = orig_fetch

    check("--source gdelt hat kein 'tickers'-Feld im Report", "tickers" not in report)


def test_fehlschlag_bricht_ab_statt_jeden_ticker_zu_wiederholen():
    """Wie bei Alpha Vantage: ein grundsaetzliches Problem (kein Key, kaputte
    Antwortform) trifft jeden weiteren Ticker genauso - Weitermachen wuerde nur
    dieselbe Fehlermeldung x-mal wiederholen."""
    orig = fh.FINNHUB_API_KEY
    fh.FINNHUB_API_KEY = ""
    try:
        raw, failed, total = asyncio.run(bt.fetch_all_raw_finnhub(
            datetime.date(2026, 5, 1), datetime.date(2026, 5, 3),
            ["AAPL", "MSFT", "GOOGL"],
        ))
    finally:
        fh.FINNHUB_API_KEY = orig

    check("bricht nach dem ersten grundsaetzlichen Fehler ab (1 statt 3 Versuche)", total == 1)
    check("dieser eine Versuch zaehlt als fehlgeschlagen", failed == 1)
    check("keine Statements bei komplettem Fehlschlag", raw == [])


def main():
    test_feed_parsing()
    test_nicht_liste_liefert_leer()
    test_fehlender_key_wirft_sofort()
    test_unerwartete_antwortform_wirft()
    test_backtest_nutzt_finnhub_quelle_und_weist_ticker_aus()
    test_backtest_ohne_tickers_nutzt_default_liste()
    test_andere_quellen_bekommen_kein_tickers_feld()
    test_fehlschlag_bricht_ab_statt_jeden_ticker_zu_wiederholen()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
