import asyncio
import logging
import time

from fastapi import Depends, FastAPI, Header, HTTPException
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

# Maximale Textlaenge fuer /api/test - ohne Cap koennte jemand ein riesiges Dokument
# einfuegen und damit unnoetig Claude-Tokens/Kosten verursachen.
MAX_TEST_TEXT_LENGTH = 4000


async def require_api_key(x_api_key: str | None = Header(default=None)):
    """Schuetzt alle datenaendernden/-preisgebenden Endpunkte, falls DASHBOARD_API_KEY
    gesetzt ist. Ohne das waere z.B. /api/test (echter Claude-Call + moeglicher
    Telegram-Alert) fuer jeden erreichbar, der die IP:Port kennt - siehe README zur
    Oracle-Cloud-Anleitung, die einen offenen Port 8000 voraussetzt."""
    if config.DASHBOARD_API_KEY and x_api_key != config.DASHBOARD_API_KEY:
        raise HTTPException(status_code=401, detail="Fehlender oder falscher X-API-Key Header")


@app.on_event("startup")
async def startup():
    # Bewusst die GESAMTE Startup-Logik in try/except: ein unerwarteter Fehler hier
    # (z.B. eine nicht beschreibbare SQLite-Datei) wuerde sonst den FastAPI-Lifespan
    # abbrechen und auch /api/health mit runterreissen - genau das Gegenteil von
    # "Dashboard laeuft trotzdem, siehe /api/health".
    try:
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
    except Exception as exc:
        _startup_errors.append(f"Unerwarteter Fehler beim Start: {exc}")
        logger.critical("Unerwarteter Fehler beim Dashboard-Start.", exc_info=True)


@app.get("/api/statements", dependencies=[Depends(require_api_key)])
def api_statements(limit: int = 50, only_relevant: bool = False):
    return get_recent(limit=limit, only_relevant=only_relevant)


@app.get("/api/stats", dependencies=[Depends(require_api_key)])
def api_stats():
    return get_stats()


@app.get("/api/health")
def api_health():
    # Bewusst OHNE Auth: soll auch von einem externen Uptime-Check ohne Secret
    # abgefragt werden koennen. Gibt nur Status/Fehlertexte preis, keine Statement-Inhalte.
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


@app.post("/api/test", dependencies=[Depends(require_api_key)])
async def api_test(req: TestRequest):
    """Jagt einen manuell eingegebenen Text durch die volle Pipeline (Claude +
    optional Telegram), ohne auf eine echte Quelle zu warten. Dient dazu, direkt
    nach dem Setup zu verifizieren, dass API-Key und Telegram korrekt konfiguriert sind."""
    if len(req.text) > MAX_TEST_TEXT_LENGTH:
        raise HTTPException(
            status_code=413,
            detail=f"Text zu lang (max. {MAX_TEST_TEXT_LENGTH} Zeichen).",
        )

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
