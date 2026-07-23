"""Technische Indikatoren (TradingView-Stil) in reinem Python - ohne numpy/pandas, damit
das Projekt dependency-frei und die Mathematik gut testbar bleibt.

Zweck: den (bislang rein text-/nachrichtenbasierten) Predictor um eine TECHNISCHE
Zweitmeinung erweitern. Aus den historischen Tageskursen eines handelbaren Tickers wird
- analog zum "Technicals"-Panel auf TradingView - ein Bündel gängiger Indikatoren
berechnet und zu einer Gesamtbewertung (Strong Buy / Buy / Neutral / Sell / Strong Sell)
verdichtet. Diese Bewertung wird im Alert angezeigt und daraufhin geprüft, ob sie die von
Claude eingeschätzte Long/Short-Richtung BESTÄTIGT oder ihr WIDERSPRICHT.

Implementiert (Großteil des TradingView-Standardsatzes):
- Gleitende Durchschnitte: SMA & EMA (10/20/30/50/100/200), VWMA(20), Hull-MA(9),
  Ichimoku-Basislinie(9/26/52)
- Oszillatoren: RSI(14), Stochastik(14,3,3), Stochastik-RSI(14,14,3,3), CCI(20),
  ADX(14)+DI, Awesome Oscillator, Momentum(10), MACD(12,26,9), Williams %R(14),
  Bull/Bear Power(13), Ultimate Oscillator(7,14,28)
- Zusätzlich für Kontext/Anzeige: ATR(14), Bollinger-Bänder(20,2), ROC, OBV

Die Aggregations-/Signal-Regeln folgen der TradingView-Methodik (je Indikator Buy/Sell/
Neutral, dann Mehrheit je Gruppe, dann Mittel aus MA- und Oszillator-Gruppe auf [-1,1]).
Wo TradingViews exakte Formel proprietär/mehrdeutig ist, ist die verwendete Regel im
Code dokumentiert - Ziel ist eine robuste, nachvollziehbare Bestätigung, kein
bit-genauer Nachbau der TradingView-Zahl.
"""
import math
from typing import Optional, Sequence

Number = float


# --- Basis-Bausteine ---------------------------------------------------------------

def _sma_series(vals: Sequence[Number], period: int) -> list[float]:
    """Gleitender einfacher Durchschnitt als Serie (Element i = SMA über vals[i-period+1..i]).
    Länge = len(vals) - period + 1 (leer, wenn zu wenig Daten)."""
    n = len(vals)
    if period <= 0 or n < period:
        return []
    out = []
    window = sum(vals[:period])
    out.append(window / period)
    for i in range(period, n):
        window += vals[i] - vals[i - period]
        out.append(window / period)
    return out


def sma(vals: Sequence[Number], period: int) -> Optional[float]:
    s = _sma_series(vals, period)
    return s[-1] if s else None


def _ema_series(vals: Sequence[Number], period: int) -> list[float]:
    """Exponentieller gleitender Durchschnitt, mit SMA der ersten `period` Werte als Seed
    (TradingView-Konvention). Länge = len(vals) - period + 1."""
    n = len(vals)
    if period <= 0 or n < period:
        return []
    k = 2.0 / (period + 1)
    seed = sum(vals[:period]) / period
    out = [seed]
    for v in vals[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(vals: Sequence[Number], period: int) -> Optional[float]:
    s = _ema_series(vals, period)
    return s[-1] if s else None


def _rma_series(vals: Sequence[Number], period: int) -> list[float]:
    """Wilder-Glättung (RMA/SMMA), Seed = SMA der ersten `period`. Basis für RSI/ATR/ADX."""
    n = len(vals)
    if period <= 0 or n < period:
        return []
    seed = sum(vals[:period]) / period
    out = [seed]
    for v in vals[period:]:
        out.append((out[-1] * (period - 1) + v) / period)
    return out


def _wma_series(vals: Sequence[Number], period: int) -> list[float]:
    """Linear gewichteter gleitender Durchschnitt (jüngster Wert am stärksten gewichtet)."""
    n = len(vals)
    if period <= 0 or n < period:
        return []
    weights = list(range(1, period + 1))
    wsum = sum(weights)
    out = []
    for i in range(period - 1, n):
        window = vals[i - period + 1: i + 1]
        out.append(sum(v * w for v, w in zip(window, weights)) / wsum)
    return out


# --- Oszillatoren ------------------------------------------------------------------

def rsi(closes: Sequence[Number], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = _rma_series(gains, period)
    al = _rma_series(losses, period)
    if not ag or not al:
        return None
    if al[-1] == 0:
        return 100.0
    rs = ag[-1] / al[-1]
    return 100.0 - 100.0 / (1.0 + rs)


def macd(closes: Sequence[Number], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[dict]:
    if len(closes) < slow + signal:
        return None
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    if not ef or not es:
        return None
    ef_tail = ef[-len(es):]  # auf die kürzere (langsame) Serie ausrichten
    macd_line = [a - b for a, b in zip(ef_tail, es)]
    sig = _ema_series(macd_line, signal)
    if not sig:
        return None
    return {"macd": macd_line[-1], "signal": sig[-1], "hist": macd_line[-1] - sig[-1]}


def _true_ranges(highs, lows, closes) -> list[float]:
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return tr


def atr(highs, lows, closes, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    s = _rma_series(_true_ranges(highs, lows, closes), period)
    return s[-1] if s else None


def adx(highs, lows, closes, period: int = 14) -> Optional[dict]:
    """Average Directional Index + gerichtete Indikatoren (+DI/-DI). Misst Trendstärke
    (ADX) und -richtung (+DI vs -DI)."""
    n = len(closes)
    if n < 2 * period:
        return None
    plus_dm, minus_dm, tr = [0.0], [0.0], [highs[0] - lows[0]]
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr_s = _rma_series(tr, period)
    pdm_s = _rma_series(plus_dm, period)
    mdm_s = _rma_series(minus_dm, period)
    if not atr_s or not pdm_s or not mdm_s:
        return None
    L = min(len(atr_s), len(pdm_s), len(mdm_s))
    plus_di, minus_di, dx = [], [], []
    for i in range(L):
        a = atr_s[len(atr_s) - L + i]
        p = 100.0 * pdm_s[len(pdm_s) - L + i] / a if a else 0.0
        m = 100.0 * mdm_s[len(mdm_s) - L + i] / a if a else 0.0
        plus_di.append(p)
        minus_di.append(m)
        dx.append(100.0 * abs(p - m) / (p + m) if (p + m) else 0.0)
    adx_s = _rma_series(dx, period)
    if not adx_s:
        return None
    return {"adx": adx_s[-1], "plus_di": plus_di[-1], "minus_di": minus_di[-1]}


def stochastic(highs, lows, closes, k: int = 14, d: int = 3, smooth: int = 3) -> Optional[dict]:
    n = len(closes)
    if n < k + smooth + d:
        return None
    raw = []
    for i in range(k - 1, n):
        hh = max(highs[i - k + 1: i + 1])
        ll = min(lows[i - k + 1: i + 1])
        raw.append(100.0 * (closes[i] - ll) / (hh - ll) if hh != ll else 0.0)
    kk = _sma_series(raw, smooth)      # geglättetes %K (TradingView: SMA)
    dd = _sma_series(kk, d)            # %D = SMA(%K)
    if not kk or not dd:
        return None
    return {"k": kk[-1], "d": dd[-1]}


def stoch_rsi(closes, rsi_period: int = 14, stoch_period: int = 14,
              k: int = 3, d: int = 3) -> Optional[dict]:
    """Stochastik-RSI: Stochastik-Formel angewandt auf die RSI-Serie."""
    if len(closes) < rsi_period + stoch_period + k + d:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    ag = _rma_series(gains, rsi_period)
    al = _rma_series(losses, rsi_period)
    L = min(len(ag), len(al))
    rsi_series = []
    for i in range(L):
        g = ag[len(ag) - L + i]
        l = al[len(al) - L + i]
        rsi_series.append(100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l))
    if len(rsi_series) < stoch_period:
        return None
    stoch_raw = []
    for i in range(stoch_period - 1, len(rsi_series)):
        window = rsi_series[i - stoch_period + 1: i + 1]
        hi, lo = max(window), min(window)
        stoch_raw.append(100.0 * (rsi_series[i] - lo) / (hi - lo) if hi != lo else 0.0)
    kk = _sma_series(stoch_raw, k)
    dd = _sma_series(kk, d)
    if not kk or not dd:
        return None
    return {"k": kk[-1], "d": dd[-1]}


def cci(highs, lows, closes, period: int = 20) -> Optional[float]:
    n = len(closes)
    if n < period:
        return None
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    window = tp[-period:]
    ma = sum(window) / period
    mean_dev = sum(abs(x - ma) for x in window) / period
    if mean_dev == 0:
        return 0.0
    return (tp[-1] - ma) / (0.015 * mean_dev)


def williams_r(highs, lows, closes, period: int = 14) -> Optional[float]:
    if len(closes) < period:
        return None
    hh = max(highs[-period:])
    ll = min(lows[-period:])
    if hh == ll:
        return -50.0
    return -100.0 * (hh - closes[-1]) / (hh - ll)


def momentum(closes, period: int = 10) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    return closes[-1] - closes[-1 - period]


def roc(closes, period: int = 9) -> Optional[float]:
    if len(closes) < period + 1 or closes[-1 - period] == 0:
        return None
    return (closes[-1] - closes[-1 - period]) / closes[-1 - period] * 100.0


def awesome_oscillator(highs, lows) -> Optional[float]:
    n = len(highs)
    if n < 34:
        return None
    median = [(highs[i] + lows[i]) / 2.0 for i in range(n)]
    return sma(median, 5) - sma(median, 34)


def ultimate_oscillator(highs, lows, closes, s1: int = 7, s2: int = 14, s3: int = 28) -> Optional[float]:
    n = len(closes)
    if n < s3 + 1:
        return None
    bp, tr = [], []
    for i in range(1, n):
        low_or_prev = min(lows[i], closes[i - 1])
        high_or_prev = max(highs[i], closes[i - 1])
        bp.append(closes[i] - low_or_prev)
        tr.append(high_or_prev - low_or_prev)

    def _avg(period):
        b = sum(bp[-period:])
        t = sum(tr[-period:])
        return b / t if t else 0.0

    return 100.0 * (4 * _avg(s1) + 2 * _avg(s2) + _avg(s3)) / 7.0


def bull_bear_power(highs, lows, closes, period: int = 13) -> Optional[float]:
    e = ema(closes, period)
    if e is None:
        return None
    bull = highs[-1] - e
    bear = lows[-1] - e
    return bull + bear


def bollinger(closes, period: int = 20, mult: float = 2.0) -> Optional[dict]:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = math.sqrt(var)
    upper = mid + mult * sd
    lower = mid - mult * sd
    pctb = (closes[-1] - lower) / (upper - lower) if upper != lower else 0.5
    return {"upper": upper, "mid": mid, "lower": lower, "pctb": pctb,
            "bandwidth": (upper - lower) / mid if mid else None}


def obv(closes, volumes) -> Optional[float]:
    if len(closes) < 2 or volumes is None or len(volumes) != len(closes):
        return None
    val = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            val += volumes[i]
        elif closes[i] < closes[i - 1]:
            val -= volumes[i]
    return val


def vwma(closes, volumes, period: int = 20) -> Optional[float]:
    if volumes is None or len(volumes) != len(closes) or len(closes) < period:
        return None
    c = closes[-period:]
    v = volumes[-period:]
    denom = sum(v)
    if denom == 0:
        return None
    return sum(ci * vi for ci, vi in zip(c, v)) / denom


def hull_ma(closes, period: int = 9) -> Optional[float]:
    if len(closes) < period:
        return None
    half = _wma_series(closes, max(1, period // 2))
    full = _wma_series(closes, period)
    L = min(len(half), len(full))
    if L == 0:
        return None
    raw = [2 * half[len(half) - L + i] - full[len(full) - L + i] for i in range(L)]
    sq = max(1, int(math.sqrt(period)))
    hs = _wma_series(raw, sq)
    return hs[-1] if hs else None


def ichimoku_base(highs, lows, conv: int = 9, base: int = 26, span_b: int = 52) -> Optional[dict]:
    """Ichimoku-Kennlinien (Wandlungslinie, Basislinie, Vorlaufspannen A/B) - für die
    Bewertung der Basislinie im TradingView-Sinne."""
    n = len(highs)
    if n < span_b:
        return None

    def _mid(period):
        return (max(highs[-period:]) + min(lows[-period:])) / 2.0

    conversion = _mid(conv)
    baseline = _mid(base)
    lead_a = (conversion + baseline) / 2.0
    lead_b = _mid(span_b)
    return {"conversion": conversion, "base": baseline, "lead_a": lead_a, "lead_b": lead_b}


# --- Signal-Regeln (Buy / Sell / Neutral je Indikator) -----------------------------

BUY, SELL, NEUTRAL = 1, -1, 0


def _ma_vote(ma_value: Optional[float], close: float) -> Optional[int]:
    """TradingView-Regel für einen gleitenden Durchschnitt: liegt der Kurs darüber -> Buy,
    darunter -> Sell. None, wenn der MA nicht berechenbar war (zählt dann nicht mit)."""
    if ma_value is None:
        return None
    if ma_value < close:
        return BUY
    if ma_value > close:
        return SELL
    return NEUTRAL


def _rating_label(score: float) -> str:
    """Score in [-1,1] -> TradingView-Klasse. Schwellen ±0.1 / ±0.5 wie bei TradingView."""
    if score >= 0.5:
        return "Strong Buy"
    if score >= 0.1:
        return "Buy"
    if score <= -0.5:
        return "Strong Sell"
    if score <= -0.1:
        return "Sell"
    return "Neutral"


def _tally(votes: list[Optional[int]]) -> dict:
    counted = [v for v in votes if v is not None]
    buy = sum(1 for v in counted if v == BUY)
    sell = sum(1 for v in counted if v == SELL)
    neutral = sum(1 for v in counted if v == NEUTRAL)
    total = len(counted)
    score = (buy - sell) / total if total else 0.0
    return {"buy": buy, "sell": sell, "neutral": neutral, "total": total, "score": score}


def technical_summary(highs, lows, closes, volumes=None) -> Optional[dict]:
    """Berechnet das komplette Indikator-Panel und verdichtet es - im TradingView-Stil -
    zu einer Gesamtbewertung. Rückgabe u.a.:
        {
          "label": "Buy", "score": 0.34,
          "ma": {buy, sell, neutral, total, score, label},
          "oscillators": {buy, sell, neutral, total, score, label},
          "values": {"rsi":.., "macd_hist":.., "adx":.., "stoch_k":.., ...},
        }
    None, wenn zu wenige Kursdaten für eine sinnvolle Auswertung vorliegen.
    """
    n = len(closes)
    if n < 35:  # unter ~35 Bars sind zu viele Indikatoren nicht bestimmbar
        return None
    close = closes[-1]

    # --- Gleitende Durchschnitte (nur die tatsächlich berechenbaren zählen mit) ---
    ma_votes: list[Optional[int]] = []
    for p in (10, 20, 30, 50, 100, 200):
        ma_votes.append(_ma_vote(sma(closes, p), close))
        ma_votes.append(_ma_vote(ema(closes, p), close))
    ma_votes.append(_ma_vote(vwma(closes, volumes, 20), close))
    ma_votes.append(_ma_vote(hull_ma(closes, 9), close))

    # Ichimoku-Basislinie: TradingView-artige Regel über Wolke + Wandlungs-/Basislinie.
    ich = ichimoku_base(highs, lows)
    if ich is not None:
        if (ich["lead_a"] > ich["lead_b"] and close > ich["lead_a"]
                and close > ich["base"] and ich["conversion"] > ich["base"]):
            ma_votes.append(BUY)
        elif (ich["lead_a"] < ich["lead_b"] and close < ich["lead_a"]
              and close < ich["base"] and ich["conversion"] < ich["base"]):
            ma_votes.append(SELL)
        else:
            ma_votes.append(NEUTRAL)

    ma = _tally(ma_votes)
    ma["label"] = _rating_label(ma["score"])

    # --- Oszillatoren (mit Vor-Wert für "steigend/fallend", wo TradingView das nutzt) ---
    osc_votes: list[Optional[int]] = []
    values: dict = {}

    rsi_now = rsi(closes)
    rsi_prev = rsi(closes[:-1])
    values["rsi"] = rsi_now
    if rsi_now is not None and rsi_prev is not None:
        if rsi_now < 30 and rsi_now > rsi_prev:
            osc_votes.append(BUY)
        elif rsi_now > 70 and rsi_now < rsi_prev:
            osc_votes.append(SELL)
        else:
            osc_votes.append(NEUTRAL)

    st = stochastic(highs, lows, closes)
    if st is not None:
        values["stoch_k"], values["stoch_d"] = st["k"], st["d"]
        if st["k"] < 20 and st["d"] < 20 and st["k"] > st["d"]:
            osc_votes.append(BUY)
        elif st["k"] > 80 and st["d"] > 80 and st["k"] < st["d"]:
            osc_votes.append(SELL)
        else:
            osc_votes.append(NEUTRAL)

    cci_now = cci(highs, lows, closes)
    cci_prev = cci(highs[:-1], lows[:-1], closes[:-1])
    values["cci"] = cci_now
    if cci_now is not None and cci_prev is not None:
        if cci_now < -100 and cci_now > cci_prev:
            osc_votes.append(BUY)
        elif cci_now > 100 and cci_now < cci_prev:
            osc_votes.append(SELL)
        else:
            osc_votes.append(NEUTRAL)

    adx_res = adx(highs, lows, closes)
    if adx_res is not None:
        values["adx"] = adx_res["adx"]
        values["plus_di"] = adx_res["plus_di"]
        values["minus_di"] = adx_res["minus_di"]
        if adx_res["adx"] > 20:
            if adx_res["plus_di"] > adx_res["minus_di"]:
                osc_votes.append(BUY)
            elif adx_res["minus_di"] > adx_res["plus_di"]:
                osc_votes.append(SELL)
            else:
                osc_votes.append(NEUTRAL)
        else:
            osc_votes.append(NEUTRAL)

    ao_now = awesome_oscillator(highs, lows)
    ao_prev = awesome_oscillator(highs[:-1], lows[:-1])
    values["ao"] = ao_now
    if ao_now is not None and ao_prev is not None:
        if ao_now > 0 and ao_now > ao_prev:
            osc_votes.append(BUY)
        elif ao_now < 0 and ao_now < ao_prev:
            osc_votes.append(SELL)
        else:
            osc_votes.append(NEUTRAL)

    mom_now = momentum(closes)
    mom_prev = momentum(closes[:-1])
    values["momentum"] = mom_now
    if mom_now is not None and mom_prev is not None:
        if mom_now > 0 and mom_now > mom_prev:
            osc_votes.append(BUY)
        elif mom_now < 0 and mom_now < mom_prev:
            osc_votes.append(SELL)
        else:
            osc_votes.append(NEUTRAL)

    mac = macd(closes)
    if mac is not None:
        values["macd"], values["macd_signal"], values["macd_hist"] = (
            mac["macd"], mac["signal"], mac["hist"])
        osc_votes.append(BUY if mac["macd"] > mac["signal"]
                         else SELL if mac["macd"] < mac["signal"] else NEUTRAL)

    srsi = stoch_rsi(closes)
    if srsi is not None:
        values["stoch_rsi_k"], values["stoch_rsi_d"] = srsi["k"], srsi["d"]
        if srsi["k"] < 20 and srsi["k"] > srsi["d"]:
            osc_votes.append(BUY)
        elif srsi["k"] > 80 and srsi["k"] < srsi["d"]:
            osc_votes.append(SELL)
        else:
            osc_votes.append(NEUTRAL)

    wr_now = williams_r(highs, lows, closes)
    wr_prev = williams_r(highs[:-1], lows[:-1], closes[:-1])
    values["williams_r"] = wr_now
    if wr_now is not None and wr_prev is not None:
        if wr_now < -80 and wr_now > wr_prev:
            osc_votes.append(BUY)
        elif wr_now > -20 and wr_now < wr_prev:
            osc_votes.append(SELL)
        else:
            osc_votes.append(NEUTRAL)

    bbp_now = bull_bear_power(highs, lows, closes)
    bbp_prev = bull_bear_power(highs[:-1], lows[:-1], closes[:-1])
    values["bull_bear_power"] = bbp_now
    if bbp_now is not None and bbp_prev is not None:
        if bbp_now > 0 and bbp_now > bbp_prev:
            osc_votes.append(BUY)
        elif bbp_now < 0 and bbp_now < bbp_prev:
            osc_votes.append(SELL)
        else:
            osc_votes.append(NEUTRAL)

    uo = ultimate_oscillator(highs, lows, closes)
    if uo is not None:
        values["ultimate"] = uo
        osc_votes.append(BUY if uo > 70 else SELL if uo < 30 else NEUTRAL)

    osc = _tally(osc_votes)
    osc["label"] = _rating_label(osc["score"])

    # Kontextwerte (nur Anzeige, kein Vote)
    bb = bollinger(closes)
    if bb is not None:
        values["bb_pctb"] = bb["pctb"]
    atr_v = atr(highs, lows, closes)
    if atr_v is not None:
        values["atr"] = atr_v
    roc_v = roc(closes)
    if roc_v is not None:
        values["roc"] = roc_v

    # TradingView-Gesamtbewertung: Mittel aus MA- und Oszillator-Gruppe.
    if ma["total"] and osc["total"]:
        summary_score = (ma["score"] + osc["score"]) / 2.0
    elif ma["total"]:
        summary_score = ma["score"]
    elif osc["total"]:
        summary_score = osc["score"]
    else:
        return None

    return {
        "label": _rating_label(summary_score),
        "score": summary_score,
        "ma": ma,
        "oscillators": osc,
        "values": values,
    }


def agreement(label: Optional[str], direction: Optional[str]) -> Optional[bool]:
    """Passt die technische Gesamtbewertung zur eingeschätzten Long/Short-Richtung?
    True = bestätigt, False = widerspricht, None = neutral/unbestimmbar."""
    if not label or direction not in ("long", "short"):
        return None
    bullish = label in ("Buy", "Strong Buy")
    bearish = label in ("Sell", "Strong Sell")
    if not bullish and not bearish:
        return None
    if direction == "long":
        return True if bullish else False
    return True if bearish else False


def strongly_contradicts(label: Optional[str], direction: Optional[str]) -> bool:
    """True nur bei KLAREM Widerspruch (Long trotz 'Strong Sell' bzw. Short trotz
    'Strong Buy') - für ein optionales, konservatives Alarm-Gate."""
    if direction == "long" and label == "Strong Sell":
        return True
    if direction == "short" and label == "Strong Buy":
        return True
    return False
