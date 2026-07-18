"""Pollt mehrere kostenlose Finanz-/Markt-RSS-Feeds.

RSS-Feeds aktualisieren oft schneller als GDELT (Minuten statt ~15 Min), sind aber
pro Feed nur so vollstaendig wie der jeweilige Anbieter. Bewusst KEIN Themen-/
Personenfilter (z.B. "nur Trump-Erwaehnungen") - die Feeds selbst sind bereits
finanz-/marktfokussiert (MarketWatch, CNBC, Yahoo Finance, Investing.com), jeder
Eintrag wird durchgereicht und die eigentliche Praezision entsteht nachgelagert
durch die strenge Claude-Klassifikation + den Tages-Kostendeckel.
"""
import asyncio
import calendar
import logging
import time

import feedparser
import httpx

from app.db import RawStatement
from app.sources.base import Source
from app.util import BoundedSeenSet, retry_async

logger = logging.getLogger(__name__)


def _entry_published_epoch(entry) -> float:
    """Echte Veroeffentlichungszeit eines RSS-Eintrags (feedparser normalisiert
    published_parsed/updated_parsed auf einen UTC-struct_time) -> Unix-Epoch. Fallback
    auf jetzt, wenn der Feed kein (parsebares) Datum liefert. Wichtig fuers Alter im
    Alert: eine 5 Stunden alte Meldung ist fuer einen Trade oft schon eingepreist."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st is not None:
            try:
                return calendar.timegm(st)
            except (TypeError, ValueError, OverflowError):
                continue
    return time.time()

FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://www.cnbc.com/id/15839135/device/rss/rss.html",  # CNBC Markets
    "https://finance.yahoo.com/news/rssindex",
    # Zusaetzliche, oft schnell aktualisierte Quellen (#1 Latenz senken):
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Top News
    "https://feeds.marketwatch.com/marketwatch/marketpulse/",  # MarketWatch MarketPulse
    "https://www.investing.com/rss/news_25.rss",  # Investing.com Economy
]


class RssNewsSource(Source):
    name = "news_rss"

    def __init__(self):
        self._seen: BoundedSeenSet = BoundedSeenSet(maxlen=5000)

    async def _fetch_feed(self, feed_url: str, client: httpx.AsyncClient) -> list:
        async def _fetch():
            resp = await client.get(feed_url)
            resp.raise_for_status()
            return resp.content

        try:
            content = await retry_async(
                _fetch, retries=2, backoff_seconds=2.0, retry_on=(httpx.TransportError,)
            )
            parsed = feedparser.parse(content)
            if parsed.bozo and not parsed.entries:
                # HTTP 200 mit nicht-RSS-Inhalt (Bot-Challenge, Paywall-Zwischenseite,
                # Umleitungsziel) faellt durch raise_for_status() nicht auf (Status ist
                # ja 200) - ohne dieses Signal saehe ein so dauerhaft degradierter Feed
                # fuer immer genauso aus wie "gerade keine Meldungen". bozo_exception
                # ist bei aelteren feedparser-Versionen ein Objekt, kein reiner String -
                # str() macht das robust fuers Logging.
                logger.warning(
                    "RSS-Feed lieferte kein gueltiges Feed-Format (evtl. Bot-Challenge/"
                    "Paywall/Redirect): %s (%s)",
                    feed_url, str(parsed.get("bozo_exception", "")),
                )
            return parsed.entries
        except Exception:
            logger.warning("RSS-Feed nicht erreichbar: %s", feed_url, exc_info=True)
            return []

    async def poll(self) -> list[RawStatement]:
        # Alle Feeds GLEICHZEITIG abfragen statt nacheinander (frueher: ein einzelner
        # sequenzieller for-Loop): bei 6 Feeds (vorher 3) wuerde ein sequenzieller
        # Abruf im ungluecklichen Fall (mehrere Feeds nahe am 15s-Timeout mit vollen
        # Retries) die gesamte Zykluszeit direkt in Richtung mehrerer Minuten treiben -
        # genau das Gegenteil des "Latenz senken"-Ziels, das die zusaetzlichen Feeds
        # ueberhaupt erst motiviert hat, und ein Risikofaktor fuer ueberlappende
        # GitHub-Actions-Laeufe beim jetzt engeren */15-Cron. httpx.AsyncClient ist
        # fuer nebenlaeufige Requests ueber denselben Client ausgelegt (gemeinsamer
        # Connection-Pool), ein einzelner haengender Feed blockiert die anderen nicht
        # mehr. Jeder Feed faengt seine eigenen Fehler ab (_fetch_feed) und liefert im
        # Fehlerfall einfach eine leere Liste, damit gather() nie wegen eines einzelnen
        # kaputten Feeds abbricht.
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            all_entries = await asyncio.gather(
                *(self._fetch_feed(feed_url, client) for feed_url in FEEDS)
            )

        results: list[RawStatement] = []
        for entries in all_entries:
            for entry in entries:
                try:
                    link = entry.get("link", "")
                    if not link or link in self._seen:
                        continue

                    title = entry.get("title", "") or ""
                    summary = entry.get("summary", "") or ""

                    self._seen.add(link)
                    text = title if not summary else f"{title} — {summary}"
                    results.append(
                        RawStatement(
                            source=self.name,
                            source_id=link,
                            text=text.strip(),
                            url=link,
                            published_at=_entry_published_epoch(entry),
                        )
                    )
                except Exception:
                    # Ein einzelner kaputter Feed-Eintrag soll nicht die
                    # Ergebnisse der restlichen Feeds in diesem Zyklus kosten.
                    logger.warning("Konnte RSS-Eintrag nicht verarbeiten, ueberspringe.", exc_info=True)
                    continue
        return results
