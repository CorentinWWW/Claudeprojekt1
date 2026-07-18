import asyncio
import hmac
import logging
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config
from app.classifier import DailyCapExceeded, classify, selftest
from app.db import (
    RawStatement,
    get_alerts_sent_today,
    get_calibration_stats,
    get_classification_calls_today,
    get_performance_stats,
    get_recent,
    get_stats,
    init_db,
    insert_statement,
    mark_alert_sent,
)
from app.orchestrator import is_alert_worthy, run_forever, run_health, source_health
from app.telegram_alert import send_alert

logger = logging.getLogger(__name__)

app = FastAPI(title="Market Impact Predictor")

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
    if config.DASHBOARD_API_KEY and not hmac.compare_digest(
        x_api_key or "", config.DASHBOARD_API_KEY
    ):
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


@app.get("/api/calibration", dependencies=[Depends(require_api_key)])
def api_calibration():
    """Echte Trefferquote der bisherigen Alerts (#3, nur mit ENABLE_PRICE_TRACKING
    befuellt): wie oft sich der Kurs tatsaechlich in die eingeschaetzte Richtung
    bewegt hat - insgesamt, je Konfidenz-Bucket und je Richtung. Damit wird die
    ALERT_MIN_TICKER_CONFIDENCE-Schwelle vom Bauchgefuehl zum belegten Wert."""
    return get_calibration_stats()


@app.get("/api/performance", dependencies=[Depends(require_api_key)])
def api_performance():
    """Aggregierte Performance der ausgewerteten Alerts (#11, nur mit
    ENABLE_PRICE_TRACKING befuellt): Gesamt-Trefferquote/-Durchschnittsrendite sowie die
    besten/schlechtesten Ticker nach mittlerer Rendite. Grundlage fuer den woechentlichen
    Telegram-Digest und die Beurteilung, auf welchen Werten die Signale wirklich tragen."""
    return get_performance_stats()


@app.get("/api/health")
def api_health():
    # Bewusst OHNE Auth: soll auch von einem externen Uptime-Check ohne Secret
    # abgefragt werden koennen. Gibt nur Status/Fehlertexte preis, keine Statement-Inhalte.
    now = time.time()
    started_at = run_health.get("started_at")

    # Kostentransparenz: wie viele Claude-Calls der Tages-Kostendeckel heute schon
    # verbraucht hat. Defensiv, damit /api/health auch dann noch antwortet, wenn die
    # DB gar nicht initialisiert werden konnte (genau dann ist der Endpunkt zur
    # Diagnose ja am wichtigsten).
    try:
        calls_today = get_classification_calls_today()
    except Exception:
        calls_today = None

    # Zusaetzliche Kennzahl (#12): heute tatsaechlich verschickte Alerts. Defensiv, damit
    # /api/health auch bei nicht initialisierbarer DB weiter antwortet.
    try:
        alerts_today = get_alerts_sent_today()
    except Exception:
        alerts_today = None

    return {
        "ok": not _startup_errors,
        "errors": _startup_errors,
        "warnings": _startup_warnings,
        "uptime_seconds": (now - started_at) if started_at else None,
        "loop_restarts": run_health.get("loop_restarts", 0),
        "last_cycle_at": run_health.get("last_cycle_at"),
        "classification_calls_today": calls_today,
        "alerts_sent_today": alerts_today,
        "classification_calls_limit": config.MAX_CLASSIFICATIONS_PER_DAY,
        # Absolutes Tages-Maximum inkl. der Reserve fuer wichtige Meldungen - oberhalb
        # von classification_calls_limit werden nur noch als wichtig eingestufte
        # Meldungen analysiert (siehe orchestrator.py: is_high_priority).
        "classification_calls_priority_limit": (
            config.MAX_CLASSIFICATIONS_PER_DAY + config.PRIORITY_CLASSIFICATIONS_PER_DAY
        ),
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
    try:
        # priority=True: ein manueller Test wird vom Nutzer bewusst ausgeloest (und ist
        # selten + auth-geschuetzt), soll also nicht am normalen Tages-Limit scheitern,
        # solange die Prioritaets-Reserve noch Luft hat.
        classification = await classify(raw.text, priority=True)
    except DailyCapExceeded as exc:
        # Sonst wuerde ein bereits ausgeschoepftes Tages-Limit hier als undurchsichtiger
        # 500er landen, statt derselben klaren, erwarteten Meldung wie ueberall sonst
        # im Code (siehe orchestrator.py: _classify_and_store).
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    statement_id = insert_statement(raw, classification)

    alert_sent = False
    # Gleicher strenger Filter wie im echten Betrieb: ein Alert wird nur ausgeloest,
    # wenn eine konkrete Aktie mit ausreichend hoher Konfidenz betroffen ist - so ist
    # der Test-Button ein echter Test dessen, was spaeter auch tatsaechlich alarmiert.
    if req.send_telegram and is_alert_worthy(classification):
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
