"""Best-effort Kursabfrage ueber eine kostenlose, key-freie Quelle (Stooq) - fuer das
Ergebnis-Tracking/Backtesting (#2), die Konfidenz-Kalibrierung (#3) und die heutige
Bewegung im Alert (#8).

Bewusst best-effort: schlaegt die Abfrage fehl (Netz nicht erreichbar, Rate-Limit,
unbekanntes Symbol), wird None zurueckgegeben und das jeweilige Feature entfaellt
still - der Poll-Zyklus darf daran NIE scheitern. Deshalb ist das gesamte Preis-Tracking
per ENABLE_PRICE_TRACKING standardmaessig aus.
"""
import logging
import math
import time
from typing import Optional

import httpx

from app import config

logger = logging.getLogger(__name__)

# Stooq-Live-Quote als CSV, ohne API-Key:
#   https://stooq.com/q/l/?s=aapl.us&f=sd2t2ohlcv&h&e=csv
# Antwort (mit Header): Symbol,Date,Time,Open,High,Low,Close,Volume
_STOOQ_URL = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"

# --- Kurz-Cache + Circuit-Breaker (#13) -------------------------------------------
# Cache: dieselbe Ticker-Quote wird innerhalb von PRICE_CACHE_TTL_SECONDS nicht erneut
# vom Kursdienst geholt (spart HTTP-Calls, wenn ein Ticker in einem Zyklus mehrfach
# vorkommt - z.B. bei Baseline-Erfassung und Anzeige). Circuit-Breaker: nach mehreren
# aufeinanderfolgenden Fehlern kurz gar nicht mehr anfragen, statt einen down/rate-limited
# Kursdienst bei jedem Ticker erneut zu hammern.
_QUOTE_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_breaker = {"consecutive_failures": 0, "open_until": 0.0}
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECONDS = 120.0


def clear_cache() -> None:
    """Setzt Cache + Circuit-Breaker zurueck (fuer Tests / manuellen Reset)."""
    _QUOTE_CACHE.clear()
    _breaker["consecutive_failures"] = 0
    _breaker["open_until"] = 0.0


def suggest_risk_levels(
    price: Optional[float],
    direction: Optional[str],
    day_high: Optional[float] = None,
    day_low: Optional[float] = None,
) -> Optional[dict]:
    """Grobe, UNVERBINDLICHE Stop-Loss-/Take-Profit-Marken (#5) aus der heutigen
    Tagesspanne als einfachem Volatilitaets-Mass (kein ATR/Historie noetig). Stop = eine
    Tagesspanne entfernt, Ziel = das 1.5-fache in Richtung des Trades (Chance-Risiko ~1.5).
    Faellt die Spanne aus (fehlend/0), wird ersatzweise 1.5% des Kurses genommen. None,
    wenn kein gueltiger Kurs/Richtung vorliegt. Ausdruecklich keine Anlageberatung."""
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        return None
    if direction not in ("long", "short"):
        return None
    span = None
    if (
        isinstance(day_high, (int, float)) and isinstance(day_low, (int, float))
        and math.isfinite(day_high) and math.isfinite(day_low) and day_high > day_low
    ):
        span = day_high - day_low
    if not span or span <= 0:
        span = price * 0.015  # Fallback: 1.5% des Kurses
    rr = 1.5
    if direction == "long":
        stop = price - span
        target = price + rr * span
    else:
        stop = price + span
        target = price - rr * span
    stop = max(0.0, stop)
    return {"stop": round(stop, 2), "target": round(target, 2), "rr": rr}


def to_stooq_symbol(ticker: str) -> str:
    """US-Ticker -> Stooq-Symbol: Kleinbuchstaben, Klassensuffix mit Bindestrich statt
    Punkt (BRK.B -> brk-b), Suffix '.us'. (BRK.B wird so zu 'brk-b.us'.)"""
    return ticker.strip().lower().replace(".", "-") + ".us"


def parse_stooq_csv(text: str) -> Optional[dict]:
    """Parst die Stooq-CSV-Antwort zu {'price', 'open', 'change_pct'} (change_pct =
    Tagesbewegung Open->Close in Prozentpunkten, oder None). None bei fehlenden/
    ungueltigen Daten ('N/D')."""
    if not isinstance(text, str):
        return None
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    values = [v.strip() for v in lines[1].split(",")]
    if len(values) != len(header):
        return None
    row = dict(zip(header, values))

    def _num(key) -> Optional[float]:
        v = row.get(key, "")
        if not v or v.upper() == "N/D":
            return None
        try:
            parsed = float(v)
        except ValueError:
            return None
        # float() akzeptiert auch "inf"/"-inf"/"nan"/"Infinity" - eine korrupte/
        # unerwartete Kursdienst-Antwort mit so einem Wert soll nicht als gueltiger
        # Kurs durchgehen (sonst koennte im Alert z.B. "heute +inf%" erscheinen).
        return parsed if math.isfinite(parsed) else None

    price = _num("close")
    open_ = _num("open")
    if price is None:
        return None
    change_pct = None
    if open_ is not None and open_ > 0:
        change_pct = (price - open_) / open_ * 100.0
    return {
        "price": price,
        "open": open_,
        "high": _num("high"),
        "low": _num("low"),
        "change_pct": change_pct,
    }


async def get_quote(ticker: str, client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """Holt eine Live-Quote fuer einen US-Ticker (best-effort). Gibt bei jedem Problem
    None zurueck (nie eine Exception nach aussen). Nutzt einen Kurz-Cache
    (PRICE_CACHE_TTL_SECONDS) und einen Circuit-Breaker (#13), damit ein mehrfach
    vorkommender Ticker nicht mehrfach abgefragt und ein down/rate-limited Kursdienst
    nicht bei jedem Ticker erneut angefragt wird."""
    if not ticker:
        return None
    symbol = to_stooq_symbol(ticker)
    now = time.time()
    ttl = config.PRICE_CACHE_TTL_SECONDS

    if ttl > 0:
        cached = _QUOTE_CACHE.get(symbol)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]

    if now < _breaker["open_until"]:
        # Circuit-Breaker offen: Kursdienst gilt gerade als gestoert, gar nicht anfragen.
        logger.debug("Kurs-Circuit-Breaker offen, ueberspringe Abfrage fuer %s.", ticker)
        return None

    url = _STOOQ_URL.format(symbol=symbol)
    try:
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
                resp = await c.get(url)
        resp.raise_for_status()
        quote = parse_stooq_csv(resp.text)
    except Exception:
        logger.debug("Kursabfrage fuer %s fehlgeschlagen (best-effort).", ticker, exc_info=True)
        _breaker["consecutive_failures"] += 1
        if _breaker["consecutive_failures"] >= _BREAKER_THRESHOLD:
            _breaker["open_until"] = now + _BREAKER_COOLDOWN_SECONDS
            logger.info(
                "Kurs-Circuit-Breaker fuer %.0fs geoeffnet (%d Fehler in Folge).",
                _BREAKER_COOLDOWN_SECONDS, _breaker["consecutive_failures"],
            )
        return None

    # Erfolgreicher HTTP-Call (auch wenn die Antwort keine gueltigen Kursdaten enthielt):
    # Fehlerzaehler zuruecksetzen und Ergebnis cachen (kurz, damit ein "N/D" nicht sofort
    # erneut abgefragt wird, aber schnell wieder frisch geholt werden kann).
    _breaker["consecutive_failures"] = 0
    if ttl > 0:
        _QUOTE_CACHE[symbol] = (now, quote)
    return quote


async def get_price(ticker: str, client: Optional[httpx.AsyncClient] = None) -> Optional[float]:
    quote = await get_quote(ticker, client=client)
    return quote["price"] if quote else None
