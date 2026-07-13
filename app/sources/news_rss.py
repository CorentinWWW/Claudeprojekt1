"""Pollt mehrere kostenlose Finanz-/Markt-RSS-Feeds und filtert auf Trump-Erwaehnungen.

RSS-Feeds aktualisieren oft schneller als GDELT (Minuten statt ~15 Min), sind aber
pro Feed nur so vollstaendig wie der jeweilige Anbieter.
"""
import logging
import time

import feedparser
import httpx

from app.db import RawStatement
from app.sources.base import Source

logger = logging.getLogger(__name__)

FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://www.cnbc.com/id/15839135/device/rss/rss.html",  # CNBC Markets
    "https://finance.yahoo.com/news/rssindex",
]


class RssNewsSource(Source):
    name = "news_rss"

    def __init__(self):
        self._seen: set[str] = set()

    async def poll(self) -> list[RawStatement]:
        results: list[RawStatement] = []
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for feed_url in FEEDS:
                try:
                    resp = await client.get(feed_url)
                    resp.raise_for_status()
                    parsed = feedparser.parse(resp.content)
                except Exception:
                    logger.warning("RSS-Feed nicht erreichbar: %s", feed_url, exc_info=True)
                    continue

                for entry in parsed.entries:
                    link = entry.get("link", "")
                    if not link or link in self._seen:
                        continue

                    title = entry.get("title", "") or ""
                    summary = entry.get("summary", "") or ""
                    haystack = f"{title} {summary}".lower()
                    if "trump" not in haystack:
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
        return results
