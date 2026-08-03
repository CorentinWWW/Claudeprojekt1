"""Finnhub company-news als weitere HISTORISCHE Nachrichtenquelle fuer app/backtest.py.

Warum eine zweite Quelle noetig wurde: Alpha Vantage begrenzt den Gratis-Tarif auf 25
Anfragen/TAG - fuer einen einzelnen sauberen Lauf ausreichend, aber schnell aufgebraucht,
wenn am selben Tag vorher schon Diagnose-/Testlaeufe passiert sind (das Kontingent ist
tages-, nicht laufbezogen - live erlebt). Finnhubs Gratis-Tarif erlaubt stattdessen
~60 Anfragen/MINUTE - grosszuegig genug, um an einem Tag mehrfach neu anzusetzen.

WICHTIGER, EHRLICHER UNTERSCHIED zu GDELT/Alpha Vantage: Finnhubs company-news ist PRO
TICKER, nicht marktweit - der Gratis-Tarif hat keinen Endpunkt fuer "alle
Marktnachrichten". Das heisst: statt Claude selbst aus einem allgemeinen Artikel den
betroffenen Ticker ableiten zu lassen, wird hier VORAB eine Ticker-Liste vorgegeben und
je Ticker abgefragt. Das ist kein 1:1-Ersatz, sondern ein anderer Blickwinkel: "wie
haetten sich Nachrichten zu DIESEN Tickern ausgewirkt" statt "welche Ticker tauchen in
den Nachrichten ueberhaupt auf". Traegt ausserdem ein echtes Auswahl-Risiko (die
Ticker-Liste ist von Menschen kuratiert) - wird im Report als 'tickers' offen
ausgewiesen, nicht verschleiert.
"""
import datetime
import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config import FINNHUB_API_KEY
from app.db import RawStatement
from app.util import retry_async

logger = logging.getLogger(__name__)

FINNHUB_ENDPOINT = "https://finnhub.io/api/v1/company-news"


class FinnhubError(RuntimeError):
    """Kein Key konfiguriert, oder Finnhub hat eine unerwartete Antwortform geliefert."""


def parse_feed(data) -> list[RawStatement]:
    """Finnhub liefert eine JSON-LISTE direkt (kein umschliessendes Objekt wie bei
    Alpha Vantages {'feed': [...]}). 'datetime' ist bereits ein Unix-Zeitstempel (UTC,
    Sekunden) - kein String-Parsing wie bei GDELT/Alpha Vantage noetig."""
    if not isinstance(data, list):
        return []
    results: list[RawStatement] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        headline = (item.get("headline") or "").strip()
        if not url or not headline:
            continue
        dt = item.get("datetime")
        if not isinstance(dt, (int, float)) or dt <= 0:
            continue
        results.append(
            RawStatement(
                source="news_finnhub",
                source_id=url,
                text=f"{headline} (Quelle: {item.get('source', 'unbekannt')})",
                url=url,
                published_at=float(dt),
            )
        )
    return results


async def fetch_ticker_range(
    ticker: str,
    start: datetime.date,
    end: datetime.date,
    client: Optional[httpx.AsyncClient] = None,
) -> list[RawStatement]:
    """Nachrichten zu EINEM Ticker im Zeitraum. `start`/`end` sind Kalendertage (Finnhub
    nutzt hier Datum, nicht Datum+Uhrzeit wie Alpha Vantage).

    Wirft FinnhubError, wenn kein Key konfiguriert ist oder die Antwort keine Liste ist
    (z.B. ein Fehlerobjekt) - beides darf NICHT als "keine Artikel" durchgehen."""
    if not FINNHUB_API_KEY:
        raise FinnhubError(
            "FINNHUB_API_KEY ist nicht gesetzt - Key kostenlos unter "
            "https://finnhub.io/register holen und in die .env eintragen."
        )

    params = {
        "symbol": ticker.upper(),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "token": FINNHUB_API_KEY,
    }
    url = f"{FINNHUB_ENDPOINT}?{urlencode(params)}"

    async def _fetch():
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.get(url)
        resp.raise_for_status()
        return resp.json()

    data = await retry_async(
        _fetch, retries=3, backoff_seconds=3.0,
        retry_on=(httpx.TransportError, httpx.HTTPStatusError),
    )
    if not isinstance(data, list):
        raise FinnhubError(
            f"Unerwartete Antwortform fuer {ticker} (erwartet: Liste): {str(data)[:200]}"
        )
    return parse_feed(data)
