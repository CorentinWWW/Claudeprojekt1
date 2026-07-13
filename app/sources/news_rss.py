"""Pollt mehrere kostenlose Finanz-/Markt-RSS-Feeds und filtert auf Trump-Erwaehnungen.

RSS-Feeds aktualisieren oft schneller als GDELT (Minuten statt ~15 Min), sind aber
pro Feed nur so vollstaendig wie der jeweilige Anbieter.
"""
import logging
import re
import time

import feedparser
import httpx

from app.db import RawStatement
from app.sources.base import Source
from app.util import BoundedSeenSet, retry_async

logger = logging.getLogger(__name__)

FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://www.cnbc.com/id/15839135/device/rss/rss.html",  # CNBC Markets
    "https://finance.yahoo.com/news/rssindex",
]

# Wortgrenze, damit "trumpet"/"trumped-up" etc. nicht faelschlich matchen, aber
# gaengige Ableitungen wie "Trumpism"/"Trumpcare"/"Trumpian" trotzdem erfassen.
TRUMP_WORD_PATTERN = re.compile(r"\btrump(?:ism|care|ian)?\b", re.IGNORECASE)


class RssNewsSource(Source):
    name = "news_rss"

    def __init__(self):
        self._seen: BoundedSeenSet = BoundedSeenSet(maxlen=5000)

    async def poll(self) -> list[RawStatement]:
        results: list[RawStatement] = []
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for feed_url in FEEDS:

                async def _fetch(feed_url=feed_url):
                    resp = await client.get(feed_url)
                    resp.raise_for_status()
                    return resp.content

                try:
                    content = await retry_async(
                        _fetch, retries=2, backoff_seconds=2.0, retry_on=(httpx.TransportError,)
                    )
                    parsed = feedparser.parse(content)
                except Exception:
                    logger.warning("RSS-Feed nicht erreichbar: %s", feed_url, exc_info=True)
                    continue

                for entry in parsed.entries:
                    try:
                        link = entry.get("link", "")
                        if not link or link in self._seen:
                            continue

                        title = entry.get("title", "") or ""
                        summary = entry.get("summary", "") or ""
                        haystack = f"{title} {summary}"
                        if not TRUMP_WORD_PATTERN.search(haystack):
                            continue

                        self._seen.add(link)
                        text = title if not summary else f"{title} — {summary}"
                        results.append(
                            RawStatement(
                                source=self.name,
                                source_id=link,
                                text=text.strip(),
                                url=link,
                                published_at=time.time(),
                            )
                        )
                    except Exception:
                        # Ein einzelner kaputter Feed-Eintrag soll nicht die
                        # Ergebnisse der restlichen Feeds in diesem Zyklus kosten.
                        logger.warning("Konnte RSS-Eintrag nicht verarbeiten, ueberspringe.", exc_info=True)
                        continue
        return results
