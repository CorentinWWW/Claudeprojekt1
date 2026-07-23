"""Tests fuer app/indicators.py (technische Indikatoren, TradingView-Stil), den Stooq-
Historie-Parser (app/prices.py) und die Verdrahtung im Orchestrator (technische
Zweitmeinung: Score-Nudge, Anzeige-Extras, Agreement-Gate). Reine Mathematik + gemockte
Kurshistorie, kein echtes Netz.
"""
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


def _approx(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def _trend(n, start, step, spread=0.5):
    """Lineare Kursreihe (steigend bei step>0, fallend bei step<0)."""
    closes = [start + step * i for i in range(n)]
    highs = [c + spread for c in closes]
    lows = [c - spread for c in closes]
    volumes = [1000.0 + i for i in range(n)]
    return highs, lows, closes, volumes


# ---------------------------------------------------------------------------
# Basis-Indikatoren (exakte Werte)
# ---------------------------------------------------------------------------

def test_basic_math():
    import app.indicators as ind

    check("SMA(5) von 1..5 = 3", _approx(ind.sma([1, 2, 3, 4, 5], 5), 3.0))
    check("SMA(2) letzte zwei von 1..5 = 4.5", _approx(ind.sma([1, 2, 3, 4, 5], 2), 4.5))
    check("SMA zu wenig Daten -> None", ind.sma([1, 2], 5) is None)

    check("EMA konstante Reihe = Konstante", _approx(ind.ema([1] * 10, 3), 1.0))
    check("EMA zu wenig Daten -> None", ind.ema([1, 2], 5) is None)

    check("RSI streng steigend = 100", _approx(ind.rsi(list(range(1, 30))), 100.0))
    check("RSI streng fallend = 0", _approx(ind.rsi(list(range(30, 1, -1))), 0.0))

    check("Momentum(10) von 1..11 = 10", _approx(ind.momentum(list(range(1, 12)), 10), 10.0))
    check("ROC(1) [1,2] = 100%", _approx(ind.roc([1, 2], 1), 100.0))

    bb = ind.bollinger([5.0] * 20)
    check("Bollinger konstante Reihe: %B = 0.5", bb is not None and _approx(bb["pctb"], 0.5))
    check("Bollinger konstante Reihe: Mitte = 5", bb is not None and _approx(bb["mid"], 5.0))

    wr = ind.williams_r([10, 11, 12], [8, 9, 10], [9, 10, 11], period=3)
    check("Williams %R = -25", _approx(wr, -25.0))

    macd = ind.macd(list(range(1, 60)))
    check("MACD steigende Reihe: macd-Linie > 0", macd is not None and macd["macd"] > 0)
    check("MACD konstante Reihe ~ 0", _approx(ind.macd([7.0] * 60)["macd"], 0.0, tol=1e-6))

    highs, lows, closes, _ = _trend(30, 100, 1)
    st = ind.stochastic(highs, lows, closes)
    check("Stochastik Aufwaertstrend: %K hoch (>80)", st is not None and st["k"] > 80)

    check("ATR steigende Reihe > 0", ind.atr(*_trend(30, 100, 1)[:3]) > 0)

    adx = ind.adx(*_trend(40, 100, 1)[:3])
    check("ADX Aufwaertstrend: +DI > -DI", adx is not None and adx["plus_di"] > adx["minus_di"])
    check("ADX Aufwaertstrend: ADX in [0,100]", adx is not None and 0 <= adx["adx"] <= 100)


# ---------------------------------------------------------------------------
# Gesamtbewertung (Rating) + Agreement
# ---------------------------------------------------------------------------

def test_rating_and_agreement():
    import app.indicators as ind

    up = _trend(60, 100, 1)
    summ_up = ind.technical_summary(up[0], up[1], up[2], up[3])
    check("Aufwaertstrend liefert eine Bewertung", summ_up is not None)
    check("Aufwaertstrend ist bullish (Buy/Strong Buy)",
          summ_up["label"] in ("Buy", "Strong Buy"))
    check("Aufwaertstrend: Score > 0", summ_up["score"] > 0)
    check("Aufwaertstrend: MA-Gruppe klar Buy", summ_up["ma"]["buy"] > summ_up["ma"]["sell"])

    down = _trend(60, 200, -1)
    summ_down = ind.technical_summary(down[0], down[1], down[2], down[3])
    check("Abwaertstrend ist bearish (Sell/Strong Sell)",
          summ_down["label"] in ("Sell", "Strong Sell"))
    check("Abwaertstrend: Score < 0", summ_down["score"] < 0)

    check("zu wenig Bars -> keine Bewertung", ind.technical_summary([1, 2], [1, 2], [1, 2]) is None)

    # Agreement-Logik
    check("bullish + long -> bestaetigt", ind.agreement("Strong Buy", "long") is True)
    check("bullish + short -> widerspricht", ind.agreement("Buy", "short") is False)
    check("bearish + short -> bestaetigt", ind.agreement("Strong Sell", "short") is True)
    check("Neutral -> None", ind.agreement("Neutral", "long") is None)
    check("keine Richtung -> None", ind.agreement("Buy", None) is None)

    check("Long trotz Strong Sell -> klarer Widerspruch",
          ind.strongly_contradicts("Strong Sell", "long") is True)
    check("Short trotz Strong Buy -> klarer Widerspruch",
          ind.strongly_contradicts("Strong Buy", "short") is True)
    check("Buy (nicht Strong) -> kein klarer Widerspruch",
          ind.strongly_contradicts("Sell", "long") is False)


# ---------------------------------------------------------------------------
# Stooq-Historie-Parser
# ---------------------------------------------------------------------------

def test_history_parser():
    import app.prices as prices

    csv = (
        "Date,Open,High,Low,Close,Volume\n"
        "2020-01-01,10,11,9,10.5,1000\n"
        "2020-01-02,10.5,12,10,11.5,1100\n"
        "2020-01-03,11.5,13,N/D,12.5,1200\n"   # kaputte Low -> Zeile wird uebersprungen
        "2020-01-04,12.5,13.5,12,13.0,N/D\n"    # Volumen N/D -> 0.0, Zeile bleibt
    )
    h = prices.parse_stooq_history_csv(csv)
    check("Parser: 3 gueltige Zeilen (kaputte uebersprungen)", h is not None and len(h["close"]) == 3)
    check("Parser: Close-Reihe korrekt", h["close"] == [10.5, 11.5, 13.0])
    check("Parser: High-Reihe korrekt", h["high"] == [11.0, 12.0, 13.5])
    check("Parser: fehlendes Volumen -> 0.0", h["volume"][-1] == 0.0)
    check("Parser: Muell -> None", prices.parse_stooq_history_csv("nonsense") is None)
    check("Parser: nur Header -> None",
          prices.parse_stooq_history_csv("Date,Open,High,Low,Close,Volume") is None)

    # max_bars begrenzt auf die juengsten Zeilen
    many = "Date,Open,High,Low,Close,Volume\n" + "\n".join(
        f"2020-{(i % 12) + 1:02d}-01,{i},{i + 1},{i - 1},{i},100" for i in range(1, 50)
    )
    h2 = prices.parse_stooq_history_csv(many, max_bars=10)
    check("Parser: max_bars begrenzt auf 10", h2 is not None and len(h2["close"]) == 10)


# ---------------------------------------------------------------------------
# Orchestrator-Verdrahtung
# ---------------------------------------------------------------------------

def _fresh(**env):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["ENABLE_TECHNICALS"] = "true"
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["ENABLE_PRICE_TRACKING"] = "false"
    os.environ["PAPER_TRADING"] = "false"
    for k, v in env.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.prices as prices
    importlib.reload(prices)
    import app.indicators as ind
    importlib.reload(ind)
    import app.paper_trading as pt
    importlib.reload(pt)
    import app.orchestrator as orch
    importlib.reload(orch)
    return config, db, prices, ind, orch


def test_orchestrator_wiring():
    config, db, prices, ind, orch = _fresh()
    from app.db import Classification

    up = _trend(60, 100, 1)

    async def fake_history(ticker, client=None, max_bars=400):
        return {"high": up[0], "low": up[1], "close": up[2], "volume": up[3]}

    orch.prices.get_history = fake_history

    # _ticker_technical: bullishe Historie + long -> bestaetigt
    summ = asyncio.run(orch._ticker_technical("NVDA", "long"))
    check("_ticker_technical liefert Bewertung", summ is not None)
    check("_ticker_technical: bullish -> agrees True", summ.get("agrees") is True)
    check("_ticker_technical: long vs bullish -> kein klarer Widerspruch",
          summ.get("contradicts_strongly") is False)

    # Score-Nudge
    check("Score-Nudge hoch bei Bestaetigung (+10)",
          orch._apply_technical_conviction(50, summ) == 60)
    contra = {"agrees": False}
    check("Score-Nudge runter bei Widerspruch (-10)",
          orch._apply_technical_conviction(50, contra) == 40)
    check("Score-Nudge neutral bei agrees None",
          orch._apply_technical_conviction(50, {"agrees": None}) == 50)
    check("Score-Nudge bleibt in [0,100] (Deckel)",
          orch._apply_technical_conviction(95, {"agrees": True}) == 100)

    # _build_alert_extras nimmt die Technik fuer die Anzeige auf
    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                         ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    extras = asyncio.run(orch._build_alert_extras(
        orch.RawStatement(source="x", source_id="x1", text="NVDA"),
        cls, statement_id=1, score=70, corroboration=1, hedged=False,
    ))
    check("_build_alert_extras enthaelt 'technical'", "technical" in extras)
    check("technical enthaelt NVDA-Bewertung",
          extras.get("technical", {}).get("NVDA", {}).get("label") in ("Buy", "Strong Buy"))


def test_agreement_gate_suppresses():
    """Das Gate wird mit einer gecannten, KLAR widersprechenden Technik-Bewertung
    getestet (Long-Signal, aber 'Strong Sell') - entkoppelt vom exakten Indikator-Tuning,
    damit der Test die Gate-Logik in _send_alerts prueft, nicht die Ratingschwellen."""
    from app.db import Classification, RawStatement, insert_statement

    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.95,
                         ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.95}])
    contra = {"label": "Strong Sell", "score": -0.8, "agrees": False,
              "contradicts_strongly": True,
              "ma": {"buy": 0, "sell": 10}, "oscillators": {"buy": 0, "sell": 8}, "values": {}}

    # --- Mit Gate: klar widersprechender Alert wird unterdrueckt ---
    config, db, prices, ind, orch = _fresh(TECHNICALS_REQUIRE_AGREEMENT="true")

    async def fake_tech(ticker, direction):
        return contra
    orch._ticker_technical = fake_tech
    sent = []

    async def fake_send_alert(raw, classification, extras=None):
        sent.append(raw)
        return True
    orch.send_alert = fake_send_alert

    raw = RawStatement(source="news", source_id="s1", text="Buy NVDA now")
    sid = insert_statement(raw, cls)
    asyncio.run(orch._send_alerts([(raw, cls, sid)]))
    check("Agreement-Gate: klar widersprechender Alert wird unterdrueckt (kein Versand)",
          len(sent) == 0)
    pend = db.get_pending_alerts()
    check("Agreement-Gate: Statement bleibt unmarkiert (noch pending)",
          bool(pend) and pend[0]["source_id"] == "s1")

    # --- Ohne Gate: derselbe (widersprechende) Alert geht durch (Technik nur Anzeige) ---
    config, db, prices, ind, orch = _fresh(TECHNICALS_REQUIRE_AGREEMENT="false")

    async def fake_tech2(ticker, direction):
        return contra
    orch._ticker_technical = fake_tech2
    sent2 = []

    async def fake_send_alert2(raw, classification, extras=None):
        sent2.append(raw)
        return True
    orch.send_alert = fake_send_alert2
    # _build_alert_extras ruft _fetch_technicals -> get_history; hier stummschalten.
    async def no_history(ticker, client=None, max_bars=400):
        return None
    orch.prices.get_history = no_history

    raw2 = RawStatement(source="news", source_id="s2", text="Buy NVDA now")
    sid2 = insert_statement(raw2, cls)
    asyncio.run(orch._send_alerts([(raw2, cls, sid2)]))
    check("ohne Gate: derselbe Alert geht durch (Technik nur Anzeige)", len(sent2) == 1)


def main():
    test_basic_math()
    test_rating_and_agreement()
    test_history_parser()
    test_orchestrator_wiring()
    test_agreement_gate_suppresses()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
