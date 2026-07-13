import asyncio
import logging
import time

from app.classifier import classify
from app.config import (
    ALERT_CONFIDENCE_THRESHOLD,
    ENABLE_LIVE_AUDIO,
    ENABLE_NEWS,
    ENABLE_TRUTH_SOCIAL,
    LIVE_AUDIO_STREAM_URLS,
    MAX_CONCURRENT_CLASSIFICATIONS,
    POLL_INTERVAL_SECONDS,
    TELEGRAM_STARTUP_NOTICE,
    WHISPER_MODEL_SIZE,
)
from app.db import find_recent_duplicate, init_db, insert_statement, is_known, mark_alert_sent
from app.sources.live_audio import LiveAudioSource
from app.sources.news_gdelt import GdeltNewsSource
from app.sources.news_rss import RssNewsSource
from app.sources.truth_social import TruthSocialSource
from app.telegram_alert import send_alert, send_startup_notice

logger = logging.getLogger(__name__)

# In-memory Health-Status pro Quelle, fuer /api/health. Muss nicht persistiert werden -
# nach einem Neustart baut sich der Status einfach durch die naechsten Polls neu auf.
source_health: dict[str, dict] = {}
run_health: dict = {"started_at": None, "loop_restarts": 0, "last_cycle_at": None}


def build_sources():
    sources = []
    if ENABLE_NEWS:
        sources.append(GdeltNewsSource())
        sources.append(RssNewsSource())
    if ENABLE_TRUTH_SOCIAL:
        sources.append(TruthSocialSource())
    if ENABLE_LIVE_AUDIO:
        sources.append(LiveAudioSource(LIVE_AUDIO_STREAM_URLS, model_size=WHISPER_MODEL_SIZE))
    for s in sources:
        source_health.setdefault(
            s.name,
            {
                "last_poll_at": None,
                "last_success_at": None,
                "last_error": None,
                "last_error_at": None,
                "total_fetched": 0,
            },
        )
    return sources


async def process_statement(raw, semaphore: asyncio.Semaphore):
    if is_known(raw.source_id):
        return

    duplicate = find_recent_duplicate(raw.text)
    if duplicate is not None:
        # Gleiche reale Aussage wurde bereits (evtl. von einer anderen Quelle)
        # klassifiziert - nicht erneut Claude bemuehen oder erneut alarmieren.
        insert_statement(
            raw,
            None,
            duplicate_of_id=duplicate["id"],
        )
        logger.info(
            "[%s] Duplikat von Statement #%d erkannt, ueberspringe Klassifikation: %s",
            raw.source,
            duplicate["id"],
            raw.text[:80],
        )
        return

    async with semaphore:
        try:
            classification = await classify(raw.text)
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
        sent = await send_alert(raw, classification)
        if sent:
            mark_alert_sent(statement_id)


async def poll_once(sources, semaphore: asyncio.Semaphore):
    for source in sources:
        health = source_health[source.name]
        health["last_poll_at"] = time.time()
        try:
            raw_statements = await source.poll()
        except Exception as exc:
            logger.exception("Polling fehlgeschlagen fuer Quelle %s", source.name)
            health["last_error"] = str(exc)
            health["last_error_at"] = time.time()
            continue

        health["last_success_at"] = time.time()
        health["total_fetched"] += len(raw_statements)

        if not raw_statements:
            continue

        await asyncio.gather(
            *(process_statement(raw, semaphore) for raw in raw_statements)
        )


async def _poll_loop():
    init_db()
    sources = build_sources()
    logger.info("Aktive Quellen: %s", [s.name for s in sources])

    if TELEGRAM_STARTUP_NOTICE:
        await send_startup_notice([s.name for s in sources])

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CLASSIFICATIONS)
    run_health["started_at"] = time.time()

    while True:
        cycle_start = time.time()
        try:
            await poll_once(sources, semaphore)
        except Exception:
            logger.exception("Unerwarteter Fehler im Poll-Zyklus, mache trotzdem weiter.")
        run_health["last_cycle_at"] = time.time()

        elapsed = time.time() - cycle_start
        await asyncio.sleep(max(1.0, POLL_INTERVAL_SECONDS - elapsed))


async def run_forever():
    """Supervisor: startet den Poll-Loop neu, falls er unerwartet mit einer Exception
    stirbt, statt dass das Monitoring fuer den Rest der Prozesslaufzeit stillschweigend
    steht (die Faelle innerhalb des Loops selbst sind bereits abgefangen; das hier ist
    ein zweites Sicherheitsnetz, z.B. falls build_sources() selbst einen Bug hat)."""
    backoff = 5
    while True:
        try:
            await _poll_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            run_health["loop_restarts"] += 1
            logger.critical(
                "Poll-Loop ist abgestuerzt, Neustart in %ds (Neustart #%d)",
                backoff,
                run_health["loop_restarts"],
                exc_info=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
