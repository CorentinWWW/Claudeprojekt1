import asyncio
import logging
from collections import deque
from difflib import SequenceMatcher
from typing import Awaitable, Callable, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    retries: int = 2,
    backoff_seconds: float = 2.0,
    retry_on: tuple[type[Exception], ...] = (httpx.HTTPError,),
) -> T:
    """Ruft fn() auf und wiederholt bei Exceptions aus retry_on mit linearem Backoff."""
    attempt = 0
    while True:
        try:
            return await fn()
        except retry_on as exc:
            attempt += 1
            if attempt > retries:
                raise
            wait = backoff_seconds * attempt
            logger.warning(
                "Versuch %d/%d fehlgeschlagen (%s), retry in %.1fs",
                attempt,
                retries,
                exc,
                wait,
            )
            await asyncio.sleep(wait)


# SequenceMatcher.ratio() ist im schlechtesten Fall O(n*m) - eine Laengenkappung
# verhindert, dass ein ungewoehnlich langer Statement-Text (z.B. ein sehr langes
# RSS-Summary) den (synchron, ohne Executor laufenden) Vergleich spuerbar verlangsamt.
_MAX_COMPARE_LENGTH = 500


def text_similarity(a: str, b: str) -> float:
    norm_a = " ".join(a.lower().split())[:_MAX_COMPARE_LENGTH]
    norm_b = " ".join(b.lower().split())[:_MAX_COMPARE_LENGTH]
    if not norm_a or not norm_b:
        return 0.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()


class BoundedSeenSet:
    """Set-artiger "schon gesehen"-Speicher mit fester Obergrenze (FIFO-Verdraengung).

    Quellen wie news_rss.py/news_gdelt.py/truth_social.py laufen in einem lang lebigen
    Prozess (z.B. systemd-Dauerbetrieb ueber Wochen) potenziell unbegrenzt weiter und
    wuerden mit einem einfachen set() sonst immer weiter wachsen. API-kompatibel zu
    einem set() fuer die hier benoetigten Operationen (`in`, `.add(...)`).
    """

    def __init__(self, maxlen: int = 5000):
        self._maxlen = maxlen
        self._order: deque = deque()
        self._set: set = set()

    def __contains__(self, item) -> bool:
        return item in self._set

    def __len__(self) -> int:
        return len(self._set)

    def add(self, item) -> None:
        if item in self._set:
            return
        self._set.add(item)
        self._order.append(item)
        if len(self._order) > self._maxlen:
            oldest = self._order.popleft()
            self._set.discard(oldest)
