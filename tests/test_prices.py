"""Tests fuer den Kurs-Parser (#2/#8) - reine Logik, kein Netz.

Inkl. Yahoo-Finance-Fallback (live gefunden: ein vorboerslicher Alert fuer RKLB bekam
trotz gueltigem Ticker keine Paper-Position, weil Stooqs kostenloser Feed vorboerslich
fuer kleinere/juengere Werte oft noch kein frisches 'close' hat - siehe
app/prices.py get_quote()). Netzwerk-Grenzen sind gemockt, die Parser selbst reine
Logik."""
import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")


def _fresh_prices_module():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    import app.prices as prices
    importlib.reload(prices)
    prices.clear_cache()
    return prices

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

    # --- Yahoo-Fallback: reine Parser (kein Netz) ------------------------------------
    from app.prices import parse_yahoo_history_json, parse_yahoo_quote_json

    # Vorboerslich: preMarketPrice muss VOR dem regulaeren Kurs verwendet werden - genau
    # die Luecke, die Stooq vorboerslich oft nicht deckt.
    yq_premarket = {"chart": {"result": [{"meta": {
        "regularMarketPrice": 24.50, "preMarketPrice": 26.10,
        "regularMarketOpen": 24.00, "previousClose": 24.00,
        "regularMarketDayHigh": 24.80, "regularMarketDayLow": 23.90,
    }}]}}
    yq = parse_yahoo_quote_json(yq_premarket)
    check("Yahoo: preMarketPrice hat Vorrang vor regularMarketPrice",
          yq and yq["price"] == 26.10)
    check("Yahoo: change_pct relativ zu open berechnet",
          yq and abs(yq["change_pct"] - (26.10 - 24.00) / 24.00 * 100) < 1e-9)

    # Regulaere Session, kein Pre-/Postmarket-Feld vorhanden -> regulaerer Kurs.
    yq_regular = {"chart": {"result": [{"meta": {
        "regularMarketPrice": 100.0, "regularMarketOpen": 98.0,
    }}]}}
    yq2 = parse_yahoo_quote_json(yq_regular)
    check("Yahoo: Fallback auf regularMarketPrice ohne Pre-/Postmarket",
          yq2 and yq2["price"] == 100.0)

    check("Yahoo: leere/kaputte Antwort -> None", parse_yahoo_quote_json({}) is None)
    check("Yahoo: None-Antwort -> None", parse_yahoo_quote_json(None) is None)
    check("Yahoo: kein result[] -> None",
          parse_yahoo_quote_json({"chart": {"result": []}}) is None)
    check("Yahoo: negativer/kaputter Preis wird verworfen (kein 0/negativ als Kurs)",
          parse_yahoo_quote_json({"chart": {"result": [{"meta": {
              "regularMarketPrice": -5.0}}]}}) is None)

    # Historie: drei Tagesbalken, Unix-Timestamps -> ISO-Datum.
    yh = {"chart": {"result": [{
        "timestamp": [1752400000, 1752486400, 1752572800],
        "indicators": {"quote": [{
            "open": [10.0, 11.0, 12.0],
            "high": [10.5, 11.5, 12.5],
            "low": [9.5, 10.5, 11.5],
            "close": [10.2, 11.2, 12.2],
            "volume": [1000, 1100, 1200],
        }]},
    }]}}
    hist = parse_yahoo_history_json(yh)
    check("Yahoo-Historie: 3 Balken geparst", hist and len(hist["close"]) == 3)
    check("Yahoo-Historie: Datum als ISO-String", hist and hist["date"][0].count("-") == 2)
    check("Yahoo-Historie: Reihenfolge/Werte erhalten", hist and hist["close"] == [10.2, 11.2, 12.2])

    # Luecke in einem Balken (fehlender close) -> dieser Balken wird uebersprungen,
    # nicht die ganze Historie verworfen.
    yh_gap = {"chart": {"result": [{
        "timestamp": [1752400000, 1752486400],
        "indicators": {"quote": [{
            "open": [10.0, 11.0], "high": [10.5, 11.5], "low": [9.5, 10.5],
            "close": [10.2, None], "volume": [1000, 1100],
        }]},
    }]}}
    check("Yahoo-Historie: einzelner luecken-Balken -> None (nur 1 gueltiger Balken uebrig)",
          parse_yahoo_history_json(yh_gap) is None)

    check("Yahoo-Historie: leere Antwort -> None", parse_yahoo_history_json({}) is None)
    check("Yahoo-Historie: None -> None", parse_yahoo_history_json(None) is None)

    # --- get_quote()/get_history(): Fallback-Orchestrierung (Netzwerk gemockt) -------
    async def _run_fallback_orchestration_tests():
        prices = _fresh_prices_module()

        # Stooq liefert einen Kurs -> Yahoo wird NICHT angefragt (Fallback nur bei Bedarf).
        async def stooq_ok(symbol, client=None):
            return {"price": 42.0, "open": 41.0, "high": 43.0, "low": 40.0, "change_pct": 2.4}
        with patch.object(prices, "_fetch_quote_by_symbol", new=AsyncMock(side_effect=stooq_ok)), \
             patch.object(prices, "_fetch_yahoo_chart_json", new=AsyncMock(
                 side_effect=AssertionError("Yahoo haette NICHT aufgerufen werden duerfen"))):
            q = await prices.get_quote("AAPL")
        check("Stooq liefert Kurs: Yahoo-Fallback wird uebersprungen", q and q["price"] == 42.0)

        # Stooq liefert nichts (z.B. vorboerslich N/D) -> Yahoo als Fallback greift.
        prices = _fresh_prices_module()
        async def stooq_none(symbol, client=None):
            return None
        async def yahoo_data(ticker, params, client=None):
            return {"chart": {"result": [{"meta": {"preMarketPrice": 26.10}}]}}
        with patch.object(prices, "_fetch_quote_by_symbol", new=AsyncMock(side_effect=stooq_none)), \
             patch.object(prices, "_fetch_yahoo_chart_json", new=AsyncMock(side_effect=yahoo_data)):
            q2 = await prices.get_quote("RKLB")
        check("Stooq liefert nichts: Yahoo-Fallback liefert den Kurs (RKLB-Fall)",
              q2 and q2["price"] == 26.10)

        # Beide Quellen liefern nichts -> sauber None, kein Crash.
        prices = _fresh_prices_module()
        async def yahoo_none(ticker, params, client=None):
            return None
        with patch.object(prices, "_fetch_quote_by_symbol", new=AsyncMock(side_effect=stooq_none)), \
             patch.object(prices, "_fetch_yahoo_chart_json", new=AsyncMock(side_effect=yahoo_none)):
            q3 = await prices.get_quote("XYZQQQ")
        check("beide Quellen leer: get_quote() liefert None statt Crash", q3 is None)

        # get_history(): derselbe Fallback-Mechanismus.
        prices = _fresh_prices_module()

        class FakeResp:
            status_code = 200
            text = "Date,Open,High,Low,Close,Volume\n2026-01-01,1,1,1,1,N/D"  # < 2 Zeilen Nutzdaten -> None

            def raise_for_status(self):
                pass

        class FakeClient:
            async def get(self, url, **kw):
                return FakeResp()

        async def yahoo_history(ticker, params, client=None):
            return {"chart": {"result": [{
                "timestamp": [1752400000, 1752486400],
                "indicators": {"quote": [{
                    "open": [10.0, 11.0], "high": [10.5, 11.5], "low": [9.5, 10.5],
                    "close": [10.2, 11.2], "volume": [1000, 1100],
                }]},
            }]}}
        with patch.object(prices, "_fetch_yahoo_chart_json", new=AsyncMock(side_effect=yahoo_history)):
            h = await prices.get_history("RKLB", client=FakeClient())
        check("get_history(): Stooq unbrauchbar -> Yahoo-Fallback liefert Historie",
              h and len(h["close"]) == 2)

    asyncio.run(_run_fallback_orchestration_tests())

    # --- Marktkapitalisierung: Finnhub statt Yahoo (live gefunden: Yahoos frueherer
    # v7/finance/quote-Endpunkt liefert seit einem Backtest-Lauf durchgehend 401
    # Unauthorized - kein Netzwerkproblem, sondern eine dauerhafte Aenderung bei
    # Yahoo. Finnhubs stock/profile2 ersetzt ihn komplett, siehe app/prices.py.) ----
    from app.prices import parse_finnhub_profile_market_cap

    check("Finnhub: marketCapitalization (Millionen) -> USD umgerechnet",
          parse_finnhub_profile_market_cap({"marketCapitalization": 3000000.0})
          == 3000000.0 * 1_000_000.0)
    check("Finnhub: unbekanntes Symbol (leeres Objekt) -> None, kein Fehler",
          parse_finnhub_profile_market_cap({}) is None)
    check("Finnhub: None-Antwort -> None", parse_finnhub_profile_market_cap(None) is None)
    check("Finnhub: fehlendes marketCapitalization-Feld -> None",
          parse_finnhub_profile_market_cap({"name": "Foo Inc"}) is None)
    check("Finnhub: negativer/kaputter Wert wird verworfen",
          parse_finnhub_profile_market_cap({"marketCapitalization": -5.0}) is None)
    check("Finnhub: Nicht-Zahl wird verworfen",
          parse_finnhub_profile_market_cap({"marketCapitalization": "viel"}) is None)

    async def _run_market_cap_tests():
        prices = _fresh_prices_module()

        # Ohne Key: sofort leeres Dict, kein Netzwerkversuch, keine Exception (best-effort,
        # FINNHUB_API_KEY ist optional - siehe .env.example).
        prices.config.FINNHUB_API_KEY = ""
        caps_no_key = await prices.get_market_caps(["AAPL"])
        check("ohne FINNHUB_API_KEY: leeres Dict statt Fehler", caps_no_key == {})

        # Mit Key: EIN Request je Ticker (kein Batch-Endpunkt mehr, siehe Modul-Docstring),
        # ein einzelner fehlschlagender/unbekannter Ticker darf die anderen nicht verwerfen.
        prices.config.FINNHUB_API_KEY = "dummy-key"
        calls = []

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def raise_for_status(self):
                pass
            def json(self):
                return self._payload

        class FakeClient:
            async def get(self, url, params=None, **kw):
                calls.append(params.get("symbol"))
                if params.get("symbol") == "NVDA":
                    raise RuntimeError("simulierter Netzfehler nur fuer NVDA")
                payload = {
                    "AAPL": {"marketCapitalization": 3000000.0},
                    "SOFI": {"marketCapitalization": 12000.0},
                    "UNBEKANNT": {},
                }.get(params.get("symbol"), {})
                return FakeResp(payload)

        orig_sleep = prices.asyncio.sleep
        prices.asyncio.sleep = AsyncMock(return_value=None)  # Test nicht ausbremsen
        try:
            caps = await prices.get_market_caps(
                ["AAPL", "NVDA", "SOFI", "UNBEKANNT"], client=FakeClient()
            )
        finally:
            prices.asyncio.sleep = orig_sleep

        check("EIN Request je Ticker abgesetzt", calls == ["AAPL", "NVDA", "SOFI", "UNBEKANNT"])
        check("AAPL korrekt in USD umgerechnet", caps.get("AAPL") == 3000000.0 * 1_000_000.0)
        check("SOFI korrekt in USD umgerechnet", caps.get("SOFI") == 12000.0 * 1_000_000.0)
        check("NVDA (Netzfehler) fehlt im Ergebnis, wirft aber NICHT nach aussen",
              "NVDA" not in caps)
        check("UNBEKANNT (leeres Finnhub-Objekt) fehlt im Ergebnis",
              "UNBEKANNT" not in caps)
        check("nur die zwei erfolgreichen Ticker im Ergebnis", len(caps) == 2)

        # Dedupe: derselbe Ticker mehrfach in der Anfrageliste -> nur EIN Request.
        calls.clear()
        prices.asyncio.sleep = AsyncMock(return_value=None)
        try:
            await prices.get_market_caps(["AAPL", "aapl", " AAPL "], client=FakeClient())
        finally:
            prices.asyncio.sleep = orig_sleep
        check("doppelte/verschieden geschriebene Ticker werden dedupliziert (1 statt 3 Requests)",
              calls == ["AAPL"])

    asyncio.run(_run_market_cap_tests())

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
