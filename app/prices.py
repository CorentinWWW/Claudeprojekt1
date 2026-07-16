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
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Stooq-Live-Quote als CSV, ohne API-Key:
#   https://stooq.com/q/l/?s=aapl.us&f=sd2t2ohlcv&h&e=csv
# Antwort (mit Header): Symbol,Date,Time,Open,High,Low,Close,Volume
_STOOQ_URL = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"


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
    return {"price": price, "open": open_, "change_pct": change_pct}


async def get_quote(ticker: str, client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """Holt eine Live-Quote fuer einen US-Ticker (best-effort). Gibt bei jedem Problem
    None zurueck (nie eine Exception nach aussen)."""
    if not ticker:
        return None
    url = _STOOQ_URL.format(symbol=to_stooq_symbol(ticker))
    try:
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
                resp = await c.get(url)
        resp.raise_for_status()
        return parse_stooq_csv(resp.text)
    except Exception:
        logger.debug("Kursabfrage fuer %s fehlgeschlagen (best-effort).", ticker, exc_info=True)
        return None


async def get_price(ticker: str, client: Optional[httpx.AsyncClient] = None) -> Optional[float]:
    quote = await get_quote(ticker, client=client)
    return quote["price"] if quote else None
