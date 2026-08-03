"""Alpha Vantage NEWS_SENTIMENT als HISTORISCHE Nachrichtenquelle fuer app/backtest.py.

Warum diese Quelle: GDELTs DOC-API liefert nach einer IP-Sperre auf unbestimmte Zeit
nur noch 429 (siehe app/sources/news_gdelt.py), und historische Nachrichten sind sonst
kaum kostenlos zu bekommen. Alpha Vantage bietet einen kostenlosen Key (ohne
Kreditkarte), Zeitraum-Parameter (time_from/time_to) und bis zu 1000 Artikel pro
Abfrage - genug, um drei Monate marktweite Nachrichten mit wenigen Abfragen abzudecken.

Bewusst NUR fuer den historischen Abruf gedacht, nicht als Live-Quelle: der kostenlose
Tarif erlaubt sehr wenige Abfragen pro Tag (Groessenordnung 25), was fuer einen
Minuten-Poll nicht reicht, fuer einen einmaligen Backtest-Lauf aber vollkommen genuegt.

WICHTIGE EIGENHEIT (haeufige Fehlerquelle): Alpha Vantage antwortet bei Rate-Limits,
ungueltigem Key oder zu grosser Anfrage NICHT mit einem HTTP-Fehlerstatus, sondern mit
HTTP 200 und einer Erklaerung im JSON-Body ("Note"/"Information"/"Error Message").
Ohne die ausdrueckliche Pruefung unten wuerde so eine Antwort still als "keine Artikel
gefunden" durchgehen - genau die Verwechslung, die beim GDELT-Rate-Limit einen ganzen
Tag gekostet hat. Deshalb wird sie hier zu einer echten Exception.
"""
import datetime
import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config import ALPHAVANTAGE_API_KEY
from app.db import RawStatement
from app.util import retry_async

logger = logging.getLogger(__name__)

ALPHAVANTAGE_ENDPOINT = "https://www.alphavantage.co/query"

# Marktweite Themen - bewusst breit gewaehlt, damit das Material dem aehnelt, was die
# Live-Quellen (RSS) liefern: allgemeine Wirtschafts-/Marktnachrichten, aus denen Claude
# selbst den betroffenen Ticker ableiten muss. Eine ticker-spezifische Abfrage waere ein
# anderes Experiment (der Ticker waere dann schon vorgegeben).
DEFAULT_TOPICS = (
    "financial_markets,earnings,mergers_and_acquisitions,economy_macro,economy_monetary"
)

# Maximum des Endpunkts pro Abfrage.
MAX_LIMIT_PER_QUERY = 1000


class AlphaVantageError(RuntimeError):
    """Alpha Vantage hat eine Erklaerung statt Daten geliefert (Rate-Limit, ungueltiger
    Key, ...) - siehe Modul-Docstring."""


def parse_time_published(value) -> Optional[float]:
    """Alpha Vantages 'time_published' hat das Format 'YYYYMMDDTHHMMSS' - OHNE das 'Z'
    am Ende, das GDELT verwendet (deshalb nicht util.parse_compact_utc_epoch, das genau
    daran scheitern wuerde). Zeiten sind UTC. None, wenn nicht parsebar."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.datetime.strptime(value.strip(), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


def _raise_if_api_message(data) -> None:
    """Macht aus einer 200er-Antwort mit Erklaerungstext eine echte Exception."""
    if not isinstance(data, dict):
        return
    for key in ("Note", "Information", "Error Message"):
        msg = data.get(key)
        if msg:
            raise AlphaVantageError(f"{key}: {msg}")


def parse_feed(data, seen: Optional[set] = None) -> list[RawStatement]:
    """Wandelt eine NEWS_SENTIMENT-Antwort in RawStatements um. `seen` (optional)
    verhindert Duplikate ueber mehrere Abfragen hinweg."""
    seen = seen if seen is not None else set()
    feed = (data or {}).get("feed") if isinstance(data, dict) else None
    if not isinstance(feed, list):
        return []
    results: list[RawStatement] = []
    for item in feed:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        title = (item.get("title") or "").strip()
        if not url or not title or url in seen:
            continue
        published_at = parse_time_published(item.get("time_published"))
        if published_at is None:
            # Ohne verlaesslichen Zeitstempel ist der Artikel fuer eine historische
            # Auswertung wertlos (der Einstiegstag waere geraten) - lieber weglassen.
            continue
        seen.add(url)
        results.append(
            RawStatement(
                source="news_alphavantage",
                source_id=url,
                # Gleiches Format wie die anderen Quellen (Titel + Herkunft), damit die
                # Klassifikation exakt dasselbe Material sieht wie im Live-Betrieb.
                text=f"{title} (Quelle: {item.get('source', 'unbekannt')})",
                url=url,
                published_at=published_at,
            )
        )
    return results


async def fetch_range(
    start: datetime.datetime,
    end: datetime.datetime,
    client: Optional[httpx.AsyncClient] = None,
    topics: str = DEFAULT_TOPICS,
    limit: int = MAX_LIMIT_PER_QUERY,
) -> list[RawStatement]:
    """Historische Nachrichten fuer einen Zeitraum. `start`/`end` muessen UTC-aware sein.

    Wirft AlphaVantageError, wenn kein Key konfiguriert ist oder die API eine Erklaerung
    statt Daten liefert - beides darf NICHT als "keine Artikel" durchgehen."""
    if not ALPHAVANTAGE_API_KEY:
        raise AlphaVantageError(
            "ALPHAVANTAGE_API_KEY ist nicht gesetzt - Key kostenlos unter "
            "https://www.alphavantage.co/support/#api-key holen und in die .env eintragen."
        )

    def _fmt(dt: datetime.datetime) -> str:
        return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M")

    params = {
        "function": "NEWS_SENTIMENT",
        "topics": topics,
        "time_from": _fmt(start),
        "time_to": _fmt(end),
        "limit": str(min(limit, MAX_LIMIT_PER_QUERY)),
        # EARLIEST statt LATEST: bei mehr Treffern als `limit` wird sonst nur das Ende
        # des Zeitraums abgedeckt und der Anfang fehlt komplett - das waere eine stille,
        # systematische Verzerrung der Stichprobe.
        "sort": "EARLIEST",
        "apikey": ALPHAVANTAGE_API_KEY,
    }
    url = f"{ALPHAVANTAGE_ENDPOINT}?{urlencode(params)}"

    async def _fetch():
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.get(url)
        resp.raise_for_status()
        return resp.json()

    data = await retry_async(
        _fetch, retries=3, backoff_seconds=4.0,
        retry_on=(httpx.TransportError, httpx.HTTPStatusError),
    )
    _raise_if_api_message(data)
    return parse_feed(data)
