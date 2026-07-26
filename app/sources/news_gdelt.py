"""GDELT DOC 2.0 API: kostenlos, kein API-Key, aktualisiert im ~15-Minuten-Takt.

Limitation: GDELT liefert Artikel-Titel, nicht das woertliche Zitat. Das reicht als
Signal ("worueber berichten Medien gerade im Marktkontext"), ersetzt aber keine
woertliche Aussage. Fuer woertliche Trump-Zitate siehe truth_social.py.
"""
import logging
import time
from urllib.parse import urlencode

import httpx

from app.db import RawStatement
from app.sources.base import Source
from app.util import BoundedSeenSet, parse_compact_utc_epoch, retry_async

logger = logging.getLogger(__name__)

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

# Bewusst KEIN Personen-/Themenfilter (z.B. "Trump") mehr - deckt allgemein alle
# marktrelevanten Nachrichten ab, unabhaengig davon wer/was sie ausloest (Politiker,
# Zentralbanken, Unternehmen, Wirtschaftsdaten, Geopolitik). Die eigentliche
# Praezision entsteht nicht hier, sondern nachgelagert durch die strenge
# Claude-Klassifikation + den Tages-Kostendeckel (siehe app/config.py:
# MAX_CLASSIFICATIONS_PER_DAY) - der begrenzt die Kosten unabhaengig davon, wie viel
# Rohmaterial hier hereinkommt.
MARKET_KEYWORDS = (
    "tariff OR tariffs OR stock OR stocks OR market OR markets OR \"Federal Reserve\" "
    "OR \"interest rate\" OR economy OR trade OR sanctions OR shares OR earnings OR "
    "\"Wall Street\" OR acquisition OR merger OR takeover OR bankruptcy OR layoffs OR "
    "downgrade OR upgrade OR \"guidance\" OR recall OR \"central bank\" OR inflation OR "
    "recession OR IPO"
)


class GdeltNewsSource(Source):
    name = "news_gdelt"

    def __init__(self):
        self._seen: BoundedSeenSet = BoundedSeenSet(maxlen=5000)

    async def poll(self) -> list[RawStatement]:
        params = {
            # sourcelang:english schraenkt auf englischsprachige Artikel ein - GDELT
            # deckt Nachrichten global in vielen Sprachen ab, und dieselbe Aussage wird
            # oft von Dutzenden Outlets in unterschiedlichen Sprachen (uebersetzt/
            # umformuliert) gemeldet. Die Text-Duplikaterkennung (Tier 1, difflib) UND
            # der Themen-Abgleich innerhalb einer Charge (siehe orchestrator.py:
            # _partition_duplicates) vergleichen nur auf Zeichenebene und koennen
            # ueber Sprachgrenzen hinweg keine Duplikate erkennen - ohne dieses Filter
            # kam dieselbe Meldung dadurch wiederholt als "neues" Statement durch.
            "query": f'({MARKET_KEYWORDS}) sourcelang:english',
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

                title = (art.get("title") or "").strip()
                if not title:
                    # NICHT als gesehen markieren: GDELT kann einen Artikel schon
                    # gelistet haben, bevor der Titel indexiert ist - wuerde man ihn
                    # trotzdem als "gesehen" vermerken, waere er dauerhaft uebersprungen,
                    # selbst wenn ein spaeterer Poll den (dann befuellten) Titel liefert.
                    continue
                self._seen.add(source_id)

                # GDELT liefert pro Artikel ein 'seendate' (Zeitpunkt, zu dem GDELT den
                # Artikel gesehen hat, ~Veroeffentlichungszeit) - deutlich aussagekraeftiger
                # als der Ingest-Zeitpunkt. Fallback auf jetzt, falls Feld fehlt/kaputt.
                published_at = parse_compact_utc_epoch(art.get("seendate")) or time.time()
                results.append(
                    RawStatement(
                        source=self.name,
                        source_id=source_id,
                        text=f"{title} (Quelle: {art.get('domain', 'unbekannt')})",
                        url=art.get("url"),
                        published_at=published_at,
                    )
                )
            return results
        except Exception as exc:
            logger.exception("GDELT-Abfrage fehlgeschlagen")
            # Ohne diese Meldung waere ein dauerhaft kaputtes GDELT (Endpoint
            # geaendert, Netzsperre, Rate-Limit) von "keine passenden Nachrichten"
            # nicht unterscheidbar - siehe Source.last_failure.
            self.note_failure(exc)
            return []
