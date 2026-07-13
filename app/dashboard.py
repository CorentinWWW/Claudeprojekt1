import asyncio
import logging
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config
from app.classifier import classify, selftest
from app.db import (
    RawStatement,
    get_recent,
    get_stats,
    init_db,
    insert_statement,
    mark_alert_sent,
)
from app.orchestrator import run_forever, run_health, source_health
from app.telegram_alert import send_alert

logger = logging.getLogger(__name__)

app = FastAPI(title="Trump Market Impact Monitor")

_startup_errors: list[str] = []
_startup_warnings: list[str] = []


@app.on_event("startup")
async def startup():
    init_db()

    errors, warnings = config.validate()
    _startup_errors.clear()
    _startup_errors.extend(errors)
    _startup_warnings.clear()
    _startup_warnings.extend(warnings)

    for w in warnings:
        logger.warning(w)
    for e in errors:
        logger.critical(e)

    if errors:
        logger.critical(
            "Monitoring-Loop wird NICHT gestartet wegen obiger Fehler. "
            "Dashboard laeuft trotzdem, siehe /api/health. .env korrigieren und neu starten."
        )
        return

    try:
        await selftest()
        logger.info("Claude-Selftest erfolgreich.")
    except Exception as exc:
        _startup_errors.append(f"Claude-Selftest fehlgeschlagen: {exc}")
        logger.critical("Claude-Selftest fehlgeschlagen, Monitoring-Loop startet nicht: %s", exc)
        return

    asyncio.create_task(run_forever())


@app.get("/api/statements")
def api_statements(limit: int = 50, only_relevant: bool = False):
    return get_recent(limit=limit, only_relevant=only_relevant)


@app.get("/api/stats")
def api_stats():
    return get_stats()


@app.get("/api/health")
def api_health():
    now = time.time()
    started_at = run_health.get("started_at")
    return {
        "ok": not _startup_errors,
        "errors": _startup_errors,
        "warnings": _startup_warnings,
        "uptime_seconds": (now - started_at) if started_at else None,
        "loop_restarts": run_health.get("loop_restarts", 0),
        "last_cycle_at": run_health.get("last_cycle_at"),
        "sources": source_health,
    }


class TestRequest(BaseModel):
    text: str
    send_telegram: bool = True


@app.post("/api/test")
async def api_test(req: TestRequest):
    """Jagt einen manuell eingegebenen Text durch die volle Pipeline (Claude +
    optional Telegram), ohne auf eine echte Quelle zu warten. Dient dazu, direkt
    nach dem Setup zu verifizieren, dass API-Key und Telegram korrekt konfiguriert sind."""
    raw = RawStatement(
        source="manual_test",
        source_id=f"manual_test:{time.time()}",
        text=req.text,
    )
    classification = await classify(raw.text)
    statement_id = insert_statement(raw, classification)

    alert_sent = False
    if req.send_telegram and classification.is_market_relevant:
        alert_sent = await send_alert(raw, classification)
        if alert_sent:
            mark_alert_sent(statement_id)

    return {
        "statement_id": statement_id,
        "classification": classification,
        "alert_sent": alert_sent,
    }


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
