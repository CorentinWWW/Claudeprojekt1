import asyncio
import logging

from app.classifier import classify
from app.config import (
    ENABLE_LIVE_AUDIO,
    ENABLE_NEWS,
    ENABLE_TRUTH_SOCIAL,
    LIVE_AUDIO_STREAM_URLS,
    POLL_INTERVAL_SECONDS,
    WHISPER_MODEL_SIZE,
)
from app.db import init_db, insert_statement, is_known, mark_alert_sent
from app.sources.live_audio import LiveAudioSource
from app.sources.news_gdelt import GdeltNewsSource
from app.sources.news_rss import RssNewsSource
from app.sources.truth_social import TruthSocialSource
from app.telegram_alert import send_alert

logger = logging.getLogger(__name__)

ALERT_CONFIDENCE_THRESHOLD = 0.5


def build_sources():
    sources = []
    if ENABLE_NEWS:
        sources.append(GdeltNewsSource())
        sources.append(RssNewsSource())
    if ENABLE_TRUTH_SOCIAL:
        sources.append(TruthSocialSource())
    if ENABLE_LIVE_AUDIO:
        sources.append(LiveAudioSource(LIVE_AUDIO_STREAM_URLS, model_size=WHISPER_MODEL_SIZE))
    return sources


async def process_statement(raw):
    if is_known(raw.source_id):
        return

    loop = asyncio.get_event_loop()
    try:
        classification = await loop.run_in_executor(None, classify, raw.text)
    except Exception:
        logger.exception("Klassifikation fehlgeschlagen fuer: %s", raw.text[:80])
        return

    statement_id = insert_statement(raw, classification)
    logger.info(
        "[%s] relevant=%s sentiment=%s conf=%.2f tickers=%s :: %s",
        raw.source,
        classification.is_market_relevant,
        classification.sentiment,
        classification.confidence,
        classification.tickers,
        raw.text[:100],
    )

    if (
        classification.is_market_relevant
        and classification.confidence >= ALERT_CONFIDENCE_THRESHOLD
    ):
        await send_alert(raw, classification)
        mark_alert_sent(statement_id)


async def poll_once(sources):
    for source in sources:
        try:
            raw_statements = await source.poll()
        except Exception:
            logger.exception("Polling fehlgeschlagen fuer Quelle %s", source.name)
            continue

        for raw in raw_statements:
            await process_statement(raw)


async def run_forever():
    init_db()
    sources = build_sources()
    logger.info("Aktive Quellen: %s", [s.name for s in sources])

    while True:
        await poll_once(sources)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
