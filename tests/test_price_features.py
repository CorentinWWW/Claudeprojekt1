"""Tests fuer die neuen Preis-Features: High/Low-Parsing, Stop-/Take-Profit-Vorschlag
(#5), Kurz-Cache und Circuit-Breaker (#13)."""
import asyncio
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


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeClient:
    """Zaehlt HTTP-Aufrufe und liefert eine feste CSV-Antwort (oder wirft)."""
    def __init__(self, text=None, raise_exc=False):
        self.text = text
        self.raise_exc = raise_exc
        self.calls = 0

    async def get(self, url):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("Netzfehler (Test)")
        return _FakeResponse(self.text)


_CSV = "Symbol,Date,Time,Open,High,Low,Close,Volume\nAAPL.US,2026-07-17,22:00:00,100,110,95,105,1000\n"


def test_parse_high_low():
    from app.prices import parse_stooq_csv

    q = parse_stooq_csv(_CSV)
    check("Close geparst", q["price"] == 105.0)
    check("Open geparst", q["open"] == 100.0)
    check("High geparst", q["high"] == 110.0)
    check("Low geparst", q["low"] == 95.0)
    check("change_pct = (105-100)/100 = 5%", abs(q["change_pct"] - 5.0) < 1e-9)


def test_risk_levels():
    from app.prices import suggest_risk_levels

    # Long: Stop unter Kurs, Ziel darueber, Chance-Risiko 1.5 (Range = 110-90 = 20).
    lvl = suggest_risk_levels(100.0, "long", day_high=110.0, day_low=90.0)
    check("long: Stop = Kurs - Range", lvl["stop"] == 80.0)
    check("long: Ziel = Kurs + 1.5*Range", lvl["target"] == 130.0)
    check("Chance-Risiko 1.5", lvl["rr"] == 1.5)

    # Short spiegelt.
    lvl_s = suggest_risk_levels(100.0, "short", day_high=110.0, day_low=90.0)
    check("short: Stop = Kurs + Range", lvl_s["stop"] == 120.0)
    check("short: Ziel = Kurs - 1.5*Range", lvl_s["target"] == 70.0)

    # Ohne brauchbare Spanne: Fallback 1.5% des Kurses.
    lvl_fb = suggest_risk_levels(200.0, "long", day_high=None, day_low=None)
    check("Fallback-Spanne 1.5% -> Stop 197", lvl_fb["stop"] == 197.0)

    # Ungueltige Eingaben -> None.
    check("kein Kurs -> None", suggest_risk_levels(None, "long") is None)
    check("Kurs 0 -> None", suggest_risk_levels(0, "long") is None)
    check("keine Richtung -> None", suggest_risk_levels(100, None) is None)
    check("Stop nie negativ",
          suggest_risk_levels(1.0, "long", day_high=100.0, day_low=1.0)["stop"] >= 0.0)

    # Extrem grosse Tagesspanne relativ zum Kurs (duenn gehandelter/sehr volatiler
    # Titel): darf weder einen unerreichbaren Long-Stop (<= 0, ein Kurs kann nie <= 0
    # werden) noch ein unerreichbares/negatives Short-Ziel erzeugen - sonst wuerde der
    # automatische Stop-/Ziel-Ausstieg im Paper-Depot fuer genau diese volatilen Titel
    # lautlos nie ausloesen.
    lvl_wide_long = suggest_risk_levels(1.0, "long", day_high=100.0, day_low=1.0)
    check("extreme Spanne (long): Stop bleibt erreichbar (> 0)", lvl_wide_long["stop"] > 0.0)
    check("extreme Spanne (long): Ziel bleibt sinnvoll (> Kurs)", lvl_wide_long["target"] > 1.0)

    lvl_wide_short = suggest_risk_levels(1.0, "short", day_high=100.0, day_low=1.0)
    check("extreme Spanne (short): Ziel bleibt erreichbar (> 0)", lvl_wide_short["target"] > 0.0)
    check("extreme Spanne (short): Stop bleibt sinnvoll (> Kurs)", lvl_wide_short["stop"] > 1.0)


def test_quote_cache():
    import app.config as config
    from app import prices

    config.PRICE_CACHE_TTL_SECONDS = 60
    prices.clear_cache()

    client = _FakeClient(text=_CSV)
    q1 = asyncio.run(prices.get_quote("AAPL", client=client))
    q2 = asyncio.run(prices.get_quote("AAPL", client=client))
    check("Cache: identisches Ergebnis", q1 == q2 and q1["price"] == 105.0)
    check("Cache: nur EIN HTTP-Call fuer zwei Abfragen", client.calls == 1)

    # TTL = 0 -> kein Cache, jeder Aufruf holt neu.
    config.PRICE_CACHE_TTL_SECONDS = 0
    prices.clear_cache()
    client2 = _FakeClient(text=_CSV)
    asyncio.run(prices.get_quote("AAPL", client=client2))
    asyncio.run(prices.get_quote("AAPL", client=client2))
    check("TTL=0: zwei HTTP-Calls", client2.calls == 2)

    config.PRICE_CACHE_TTL_SECONDS = 60  # zuruecksetzen fuer Folgetests


def test_circuit_breaker():
    import app.config as config
    from app import prices

    config.PRICE_CACHE_TTL_SECONDS = 0  # Cache aus, damit nur der Breaker zaehlt
    prices.clear_cache()

    client = _FakeClient(raise_exc=True)
    # Erste 5 Fehler gehen jeweils an den Kursdienst; danach oeffnet der Breaker.
    for _ in range(prices._BREAKER_THRESHOLD):
        res = asyncio.run(prices.get_quote("AAA", client=client))
        check_silent = res is None
    check("5 Fehlversuche erreichten den Kursdienst", client.calls == prices._BREAKER_THRESHOLD)
    check("alle Fehlversuche liefern None", res is None)

    # Weiterer Aufruf: Breaker offen -> KEIN HTTP-Call mehr.
    res_open = asyncio.run(prices.get_quote("BBB", client=client))
    check("Breaker offen: kein weiterer HTTP-Call", client.calls == prices._BREAKER_THRESHOLD)
    check("Breaker offen: Ergebnis None", res_open is None)

    # Nach clear_cache ist der Breaker zurueckgesetzt.
    prices.clear_cache()
    ok_client = _FakeClient(text=_CSV)
    res_ok = asyncio.run(prices.get_quote("CCC", client=ok_client))
    check("nach Reset wieder erreichbar", res_ok is not None and ok_client.calls == 1)

    config.PRICE_CACHE_TTL_SECONDS = 60


def main():
    test_parse_high_low()
    test_risk_levels()
    test_quote_cache()
    test_circuit_breaker()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
