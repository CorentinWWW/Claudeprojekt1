import asyncio
import logging
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


def text_similarity(a: str, b: str) -> float:
    norm_a = " ".join(a.lower().split())
    norm_b = " ".join(b.lower().split())
    if not norm_a or not norm_b:
        return 0.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()
