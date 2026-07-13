"""Fuehrt genau einen Poll-Zyklus aus und beendet sich dann.

Gedacht fuer Betrieb via Scheduler (z.B. GitHub Actions Cron) statt als
Dauerprozess: kein Dashboard, kein Supervisor-Loop, keine Start-Heartbeat-
Nachricht - der Scheduler selbst uebernimmt die Wiederholung.

Der SQLite-State (welche Statements schon bekannt sind, siehe DB_PATH) MUSS
zwischen Aufrufen erhalten bleiben, sonst werden dieselben Nachrichten bei
jedem Lauf erneut klassifiziert/alarmiert. In GitHub Actions uebernimmt das
der Cache-Schritt in .github/workflows/monitor.yml.
"""
import asyncio
import logging
import os
import sys
import time

from app.config import MAX_CONCURRENT_CLASSIFICATIONS, validate
from app.db import RawStatement, init_db
from app.orchestrator import build_sources, poll_once, process_statement

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> int:
    errors, warnings = validate()
    for w in warnings:
        logger.warning(w)
    if errors:
        for e in errors:
            logger.critical(e)
        return 1

    init_db()
    sources = build_sources()
    logger.info("Aktive Quellen: %s", [s.name for s in sources])

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CLASSIFICATIONS)
    await poll_once(sources, semaphore)

    test_text = os.getenv("MANUAL_TEST_TEXT", "").strip()
    if test_text:
        logger.info("Manueller Test-Text gesetzt, jage ihn durch die Pipeline: %s", test_text[:100])
        raw = RawStatement(
            source="manual_test",
            source_id=f"manual_test:{time.time()}",
            text=test_text,
        )
        await process_statement(raw, semaphore)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
