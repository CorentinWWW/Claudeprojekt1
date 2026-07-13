"""GDELT DOC 2.0 API: kostenlos, kein API-Key, aktualisiert im ~15-Minuten-Takt.

Limitation: GDELT liefert Artikel-Titel, nicht das woertliche Zitat. Das reicht als
Signal ("worueber berichten Medien im Zusammenhang mit Trump+Markt gerade"), ersetzt
aber keine woertliche Aussage. Fuer woertliche Zitate siehe truth_social.py.
"""
import logging
import time
from urllib.parse import urlencode

import httpx

from app.db import RawStatement
from app.sources.base import Source
from app.util import retry_async

logger = logging.getLogger(__name__)

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

MARKET_KEYWORDS = (
    "tariff OR tariffs OR stock OR stocks OR market OR markets OR \"Federal Reserve\" "
    "OR \"interest rate\" OR economy OR trade OR sanctions OR shares OR earnings "
    "OR \"Wall Street\""
)


class GdeltNewsSource(Source):
    name = "news_gdelt"

    def __init__(self):
        self._seen: set[str] = set()

    async def poll(self) -> list[RawStatement]:
        params = {
            "query": f'Trump ({MARKET_KEYWORDS})',
            "mode": "ArtList",
            "format": "json",
            "maxrecords": "75",
            "sort": "DateDesc",
            "timespan": "3h",
        }
        url = f"{GDELT_ENDPOINT}?{urlencode(params)}"

        async def _fetch():
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()

        try:
            # Nur Verbindungsfehler (DNS/Timeout/Reset) werden wiederholt - ein
            # HTTP-Statusfehler wie 429/403 wuerde sich innerhalb weniger Sekunden
            # ohnehin nicht aendern, das erledigt der naechste Poll-Zyklus.
            data = await retry_async(_fetch, retries=2, backoff_seconds=2.0, retry_on=(httpx.TransportError,))

            # Bewusst innerhalb des try-Blocks: GDELT liefert bei manchen
            # Rand-/Fehlerfaellen valides JSON, das aber nicht die erwartete
            # Dict-Struktur hat (z.B. eine Fehlermeldung als Liste/String) -
            # das soll die Quelle nicht mit einem AttributeError abschiessen.
            articles = data.get("articles", []) or [] if isinstance(data, dict) else []
            results: list[RawStatement] = []
            for art in articles:
                if not isinstance(art, dict):
                    continue
                source_id = art.get("url", "")
                if not source_id or source_id in self._seen:
                    continue
                self._seen.add(source_id)

                title = (art.get("title") or "").strip()
                if not title:
                    continue

                results.append(
                    RawStatement(
                        source=self.name,
                        source_id=source_id,
                        text=f"{title} (Quelle: {art.get('domain', 'unbekannt')})",
                        url=art.get("url"),
                        published_at=time.time(),
                    )
                )
            return results
        except Exception:
            logger.exception("GDELT-Abfrage fehlgeschlagen")
            return []
