import asyncio
import logging
import time

from app.classifier import classify
from app.config import (
    ALERT_CONFIDENCE_THRESHOLD,
    ALERT_DIGEST_THRESHOLD,
    DEDUP_SIMILARITY_THRESHOLD,
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
from app.telegram_alert import send_alert, send_digest_alert, send_startup_notice
from app.util import text_similarity

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


def _partition_duplicates(raw_statements: list):
    """Trennt eine Charge frischer Statements in (zu klassifizierende, Duplikate).

    Prueft nicht nur gegen bereits in der DB gespeicherte Statements, sondern auch
    gegeneinander INNERHALB der Charge: bei vielen gleichzeitig eintreffenden
    Meldungen (z.B. dieselbe Agenturmeldung, von zig Portalen wortgleich
    syndiziert) wuerde ein reiner DB-Check das nicht erkennen, weil zum
    Pruefzeitpunkt noch keine der Geschwister-Meldungen in der DB steht - das
    fuehrte in der Praxis dazu, dass ein einzelner Nachrichtenschub dieselbe
    Meldung mehrfach alarmiert hat.

    Gibt (to_classify, duplicate_pairs) zurueck: duplicate_pairs enthaelt
    (duplicate_raw, primary_raw) - primary_raw ist entweder ein Element aus
    to_classify (noch nicht in der DB) oder hat bereits eine DB-ID (siehe Aufrufer).
    """
    to_classify = []
    duplicate_pairs = []  # (duplicate_raw, primary_raw_or_dbrow)
    batch: list[tuple] = []  # (text, raw) bereits akzeptierter Statements dieser Charge

    for raw in raw_statements:
        if is_known(raw.source_id):
            continue

        db_duplicate = find_recent_duplicate(raw.text)
        if db_duplicate is not None:
            duplicate_pairs.append((raw, db_duplicate))
            continue

        batch_duplicate = None
        for seen_text, seen_raw in batch:
            if text_similarity(raw.text, seen_text) >= DEDUP_SIMILARITY_THRESHOLD:
                batch_duplicate = seen_raw
                break

        if batch_duplicate is not None:
            duplicate_pairs.append((raw, batch_duplicate))
            continue

        to_classify.append(raw)
        batch.append((raw.text, raw))

    return to_classify, duplicate_pairs


async def _classify_and_store(raw, semaphore: asyncio.Semaphore):
    async with semaphore:
        try:
            classification = await classify(raw.text)
        except Exception:
            logger.exception("Klassifikation fehlgeschlagen fuer: %s", raw.text[:80])
            return None

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
    return (raw, classification, statement_id)


async def _send_alerts(alert_worthy: list[tuple]):
    """Schickt einzelne Alerts bei wenigen Treffern, sonst eine gebuendelte
    Sammel-Nachricht - verhindert eine Alert-Flut bei einem Nachrichtenschub
    (z.B. wenn ploetzlich viele echte, unterschiedliche Meldungen gleichzeitig
    marktrelevant sind)."""
    if not alert_worthy:
        return

    if len(alert_worthy) <= ALERT_DIGEST_THRESHOLD:
        for raw, classification, statement_id in alert_worthy:
            sent = await send_alert(raw, classification)
            if sent:
                mark_alert_sent(statement_id)
    else:
        sent = await send_digest_alert(alert_worthy)
        if sent:
            for _, _, statement_id in alert_worthy:
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

        to_classify, duplicate_pairs = _partition_duplicates(raw_statements)

        results = await asyncio.gather(
            *(_classify_and_store(raw, semaphore) for raw in to_classify)
        )
        results = [r for r in results if r is not None]
        id_by_source_id = {r[0].source_id: r[2] for r in results}

        for dup_raw, primary in duplicate_pairs:
            # primary ist entweder eine DB-Row (dict, hat "id") oder ein RawStatement
            # aus derselben Charge (dessen DB-ID erst jetzt, nach der Klassifikation, bekannt ist).
            primary_id = primary["id"] if isinstance(primary, dict) else id_by_source_id.get(primary.source_id)
            if primary_id is None:
                # Primary-Statement konnte nicht klassifiziert werden (z.B. Claude-Fehler) -
                # Duplikat einfach verwerfen, es wurde bereits geloggt.
                continue
            insert_statement(dup_raw, None, duplicate_of_id=primary_id)
            logger.info(
                "[%s] Duplikat von Statement #%d erkannt, ueberspringe Klassifikation: %s",
                dup_raw.source,
                primary_id,
                dup_raw.text[:80],
            )

        alert_worthy = [
            r for r in results
            if r[1].is_market_relevant and r[1].confidence >= ALERT_CONFIDENCE_THRESHOLD
        ]
        await _send_alerts(alert_worthy)


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
