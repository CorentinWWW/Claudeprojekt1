"""GDELT DOC 2.0 API: kostenlos, kein API-Key, aktualisiert im ~15-Minuten-Takt.

Limitation: GDELT liefert Artikel-Titel, nicht das woertliche Zitat. Das reicht als
Signal ("worueber berichten Medien gerade im Marktkontext"), ersetzt aber keine
woertliche Aussage. Fuer woertliche Trump-Zitate siehe truth_social.py.
"""
import datetime
import logging
import time
from typing import Optional
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


# GDELT deckelt eine einzelne DOC-2.0-Abfrage hart auf 250 Treffer - es gibt KEINE
# Offset-/Seiten-Parameter, um darueber hinauszukommen. Fuer den Live-Poll (kurzes
# Zeitfenster, MAXRECORDS=75) ist das nie relevant, aber fuer historische Abfragen
# ueber laengere Zeitraeume (siehe fetch_range/app/backtest.py) heisst das: an einem
# nachrichtenreichen Tag koennen mehr als 250 passende Artikel schlicht nicht alle
# abgeholt werden. Der Backtest chunked deshalb in kurze Zeitfenster (Standard:
# taeglich), um das Risiko zu verkleinern, nicht zu eliminieren - das ist eine
# dokumentierte, nicht wegprogrammierbare Grenze der kostenlosen API.
GDELT_MAX_RECORDS_PER_QUERY = 250


def _parse_gdelt_response(data, seen: set) -> list[RawStatement]:
    """Gemeinsamer Parser fuer Live-Poll UND historische Abfragen (app/backtest.py).
    `seen` wird sowohl gelesen (Duplikate ueberspringen) als auch befuellt (Aufrufer
    entscheidet, ob das ein langlebiges BoundedSeenSet oder ein einmaliges set() fuer
    einen einzelnen Backtest-Chunk ist).

    Bewusst innerhalb dieser Funktion keine eigene Fehlerbehandlung: GDELT liefert bei
    manchen Rand-/Fehlerfaellen valides JSON, das aber nicht die erwartete Dict-
    Struktur hat (z.B. eine Fehlermeldung als Liste/String) - das darf hier nicht mit
    einem AttributeError abschiessen, die Aufrufer faengt das ab."""
    articles = data.get("articles", []) or [] if isinstance(data, dict) else []
    results: list[RawStatement] = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        source_id = art.get("url", "")
        if not source_id or source_id in seen:
            continue

        title = (art.get("title") or "").strip()
        if not title:
            # NICHT als gesehen markieren: GDELT kann einen Artikel schon gelistet
            # haben, bevor der Titel indexiert ist - wuerde man ihn trotzdem als
            # "gesehen" vermerken, waere er dauerhaft uebersprungen, selbst wenn ein
            # spaeterer Poll den (dann befuellten) Titel liefert.
            continue
        seen.add(source_id)

        # GDELT liefert pro Artikel ein 'seendate' (Zeitpunkt, zu dem GDELT den Artikel
        # gesehen hat, ~Veroeffentlichungszeit) - deutlich aussagekraeftiger als der
        # Ingest-Zeitpunkt. Fallback auf jetzt, falls Feld fehlt/kaputt.
        published_at = parse_compact_utc_epoch(art.get("seendate")) or time.time()
        results.append(
            RawStatement(
                source="news_gdelt",
                source_id=source_id,
                text=f"{title} (Quelle: {art.get('domain', 'unbekannt')})",
                url=art.get("url"),
                published_at=published_at,
            )
        )
    return results


async def fetch_range(
    start: datetime.datetime, end: datetime.datetime, client: Optional[httpx.AsyncClient] = None
) -> list[RawStatement]:
    """Historische GDELT-Abfrage fuer einen festen Zeitraum (statt des rollierenden
    `timespan`-Fensters von poll()) - fuer app/backtest.py. `start`/`end` muessen
    UTC-aware sein.

    WICHTIG (ehrlich, nicht beworben): Ob und wie weit GDELTs DOC-2.0-API tatsaechlich
    rueckwirkend Daten liefert, ist von hier aus nicht verifizierbar (Netzsperre in der
    Entwicklungsumgebung) und oeffentlich nicht mit letzter Sicherheit dokumentiert -
    es kursieren unterschiedliche Angaben zum Umfang des Suchfensters. Diese Funktion
    behauptet NICHTS ueber die Reichweite; leere Ergebnisse fuer einen weit
    zurueckliegenden Zeitraum sind das ehrliche Signal, dass GDELT dafuer nichts (mehr)
    hat - kein Bug. Der Dry-Run in app/backtest.py macht genau das sichtbar, bevor
    irgendein Claude-Call bezahlt wird."""
    def _fmt(dt: datetime.datetime) -> str:
        return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")

    params = {
        "query": f'({MARKET_KEYWORDS}) sourcelang:english',
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(GDELT_MAX_RECORDS_PER_QUERY),
        "sort": "DateAsc",  # chronologisch - passend fuer einen Backtest-Replay
        "startdatetime": _fmt(start),
        "enddatetime": _fmt(end),
    }
    url = f"{GDELT_ENDPOINT}?{urlencode(params)}"

    async def _fetch():
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.get(url)
        resp.raise_for_status()
        return resp.json()

    data = await retry_async(_fetch, retries=2, backoff_seconds=2.0, retry_on=(httpx.TransportError,))
    return _parse_gdelt_response(data, seen=set())


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
            return _parse_gdelt_response(data, seen=self._seen)
        except Exception as exc:
            logger.exception("GDELT-Abfrage fehlgeschlagen")
            # Ohne diese Meldung waere ein dauerhaft kaputtes GDELT (Endpoint
            # geaendert, Netzsperre, Rate-Limit) von "keine passenden Nachrichten"
            # nicht unterscheidbar - siehe Source.last_failure.
            self.note_failure(exc)
            return []
