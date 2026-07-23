import asyncio
import csv
import hmac
import io
import logging
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config
from app.classifier import DailyCapExceeded, classify, selftest
from app.db import (
    RawStatement,
    get_alerts_sent_today,
    get_calibration_stats,
    get_classification_calls_today,
    get_kelly_inputs,
    get_outcomes_for_export,
    get_paper_closed_stats,
    get_paper_realized_pnl,
    get_performance_stats,
    get_recent,
    get_source_reliability,
    get_stats,
    init_db,
    insert_statement,
    mark_alert_sent,
)
from app.orchestrator import active_gates, is_alert_worthy, run_forever, run_health, source_health
from app.paper_trading import account_snapshot
from app.scoring import kelly_fraction
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


def _recommend_min_confidence(calibration: dict, min_samples: int = 5) -> dict | None:
    """Empfiehlt aus der Kalibrierung eine ALERT_MIN_TICKER_CONFIDENCE-Untergrenze: den
    niedrigsten Konfidenz-Bucket (mit genug Datenpunkten), ab dem die Trefferquote noch
    ueberzeugend ist - so wird die Schwelle vom Bauchgefuehl zum belegten Wert. None,
    wenn es noch zu wenig Daten gibt."""
    buckets = [
        b for b in (calibration.get("by_confidence_bucket") or [])
        if (b.get("n") or 0) >= min_samples and b.get("hit_rate") is not None
    ]
    if not buckets:
        return None
    # Bester Bucket nach Trefferquote; bei Gleichstand der mit mehr Datenpunkten.
    best = max(buckets, key=lambda b: (b["hit_rate"], b["n"]))
    # bucket-Label ist z.B. "80-90%"; die Untergrenze als Empfehlung nehmen.
    try:
        lower = int(str(best["bucket"]).split("-")[0].replace("%", ""))
        recommended = round(lower / 100.0, 2)
    except (ValueError, KeyError):
        recommended = None
    return {
        "recommended_min_ticker_confidence": recommended,
        "based_on_bucket": best["bucket"],
        "bucket_hit_rate": best["hit_rate"],
        "bucket_n": best["n"],
    }


@app.get("/api/calibration", dependencies=[Depends(require_api_key)])
def api_calibration():
    """Echte Trefferquote der bisherigen Alerts (#3, nur mit ENABLE_PRICE_TRACKING
    befuellt): wie oft sich der Kurs tatsaechlich in die eingeschaetzte Richtung
    bewegt hat - insgesamt, je Konfidenz-Bucket und je Richtung. Plus eine daraus
    abgeleitete Schwellen-Empfehlung (#8). Damit wird die ALERT_MIN_TICKER_CONFIDENCE-
    Schwelle vom Bauchgefuehl zum belegten Wert."""
    stats = get_calibration_stats()
    stats["recommendation"] = _recommend_min_confidence(stats)
    return stats


@app.get("/api/performance", dependencies=[Depends(require_api_key)])
def api_performance():
    """Aggregierte Performance der ausgewerteten Alerts (#11, nur mit
    ENABLE_PRICE_TRACKING befuellt): Gesamt-Trefferquote/-Durchschnittsrendite, die
    besten/schlechtesten Ticker, die Trefferquote JE QUELLE (#7) sowie ein grober,
    unverbindlicher Kelly-lite Positionsanteil (#5). Grundlage fuer den woechentlichen
    Telegram-Digest und die Beurteilung, auf welchen Werten die Signale wirklich tragen."""
    stats = get_performance_stats()
    stats["by_source"] = get_source_reliability()
    ki = get_kelly_inputs()
    stats["kelly"] = {
        "inputs": ki,
        "suggested_fraction": (
            kelly_fraction(ki["hit_rate"], ki["avg_win_pct"], ki["avg_loss_pct"])
            if (ki.get("n") or 0) >= 10 else None
        ),
    }
    return stats


@app.get("/api/paper", dependencies=[Depends(require_api_key)])
def api_paper():
    """Paper-Trading Depot-Status: aktueller Wert, Gewinn/Verlust, Anzahl offener und
    geschlossener Positionen, Win-Rate. Nur mit PAPER_TRADING befuellt."""
    if not config.PAPER_TRADING:
        return {"error": "Paper-Trading ist nicht aktiviert", "enabled": False}
    snap = account_snapshot()
    closed = get_paper_closed_stats()
    return {
        "enabled": True,
        "starting_capital": config.PAPER_STARTING_CAPITAL,
        "account_value": snap["account_value"],
        "free_cash": snap["free_cash"],
        "open_stake": snap["open_stake"],
        "realized_pnl": snap["realized_pnl"],
        "closed_trades": closed["closed"],
        "wins": closed["wins"],
        "win_rate": closed["wins"] / closed["closed"] if closed["closed"] else None,
        "total_return_pct": (snap["account_value"] - config.PAPER_STARTING_CAPITAL) / config.PAPER_STARTING_CAPITAL * 100.0 if config.PAPER_STARTING_CAPITAL else 0.0,
    }


@app.get("/api/outcomes.csv", dependencies=[Depends(require_api_key)])
def api_outcomes_csv(limit: int = 1000):
    """Ergebnis-Datensaetze als CSV fuer die Offline-Analyse (#9, z.B. in einem
    Spreadsheet). Nur mit ENABLE_PRICE_TRACKING befuellt."""
    rows = get_outcomes_for_export(limit=limit)
    buf = io.StringIO()
    fieldnames = [
        "id", "statement_id", "source", "ticker", "direction", "confidence",
        "alert_ts", "alert_price", "followup_ts", "followup_price", "return_pct", "correct",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return PlainTextResponse(content=buf.getvalue(), media_type="text/csv")


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

    # Zyklus-Timing (#11): letzte Dauer + gleitender Durchschnitt der letzten Zyklen.
    recent = run_health.get("recent_cycle_seconds") or []
    avg_cycle = (sum(recent) / len(recent)) if recent else None

    return {
        "ok": not _startup_errors,
        "errors": _startup_errors,
        "warnings": _startup_warnings,
        "uptime_seconds": (now - started_at) if started_at else None,
        "loop_restarts": run_health.get("loop_restarts", 0),
        "last_cycle_at": run_health.get("last_cycle_at"),
        "last_cycle_seconds": run_health.get("last_cycle_seconds"),
        "avg_cycle_seconds": avg_cycle,
        # Uebersicht der aktiven optionalen Gates/Features (#12) - macht sofort sichtbar,
        # ob z.B. ein Ratelimit/eine Ruhezeit gerade Alerts zurueckhaelt.
        "active_gates": active_gates(),
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
