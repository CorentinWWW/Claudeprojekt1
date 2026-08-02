import asyncio
import csv
import datetime
import hmac
import io
import json
import logging
import sys
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config, ensemble
from app.classifier import DailyCapExceeded, classify, selftest
from app.db import (
    RawStatement,
    analyze_gap_impact,
    compare_model_performance,
    get_alerts_sent_today,
    get_calibration_stats,
    get_classification_calls_today,
    get_gate_statistics,
    get_hourly_performance,
    get_kelly_inputs,
    get_outcomes_for_export,
    get_paper_closed_stats,
    get_paper_realized_pnl,
    get_performance_stats,
    get_pipeline_statistics,
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

# Sicherheitsdeckel fuer /api/backtest, UNABHAENGIG davon, was das Formular schickt -
# schuetzt vor einem (versehentlich) riesigen Zeitraum/Call-Budget ueber die rohe API,
# nicht nur ueber die UI-Eingabefelder (die dieselben Grenzen als min/max setzen).
BACKTEST_MAX_CALLS_LIMIT = 100
BACKTEST_MAX_RANGE_DAYS = 60
BACKTEST_MAX_HORIZON_DAYS = 30
BACKTEST_MAX_HORIZONS_COUNT = 6
# Grosszuegig genug fuer den Standard-Rahmen (60 Tage x taeglicher GDELT-Chunk +
# BACKTEST_MAX_CALLS_LIMIT Claude-Calls), verhindert aber, dass ein haengender GDELT-
# oder Claude-Call einen Dashboard-Request auf unbestimmte Zeit offen haelt.
BACKTEST_TIMEOUT_SECONDS = 600
# Verhindert, dass ein mehrfacher Klick auf "Ausfuehren" mehrere kostenpflichtige
# Backtest-Subprozesse gleichzeitig lostreten.
_backtest_lock = asyncio.Lock()
_REPO_ROOT = Path(__file__).resolve().parent.parent


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


@app.get("/api/gate-stats", dependencies=[Depends(require_api_key)])
def api_gate_stats(hours: int = 24):
    """Datensammlung (#Gate-Evaluation): je Zustell-Gate (Technik-Uebereinstimmung,
    historische Performance, Ueberzeugungs-Schwelle, Cooldown, Ruhezeiten,
    Stunden-Ratelimit) wie oft geprueft und wie oft blockiert in den letzten `hours`
    Stunden - zeigt, welches Gate den meisten Effekt hat."""
    return get_gate_statistics(hours=hours)


@app.get("/api/model-performance", dependencies=[Depends(require_api_key)])
def api_model_performance():
    """Datensammlung (#Claude-Model-Tracking): Trefferquote je verwendetem
    Claude-Modell (Haiku vs. Sonnet-Eskalation) - belegt, ob die teurere Eskalation
    tatsaechlich bessere Alerts liefert. Nur mit ENABLE_PRICE_TRACKING befuellt."""
    return compare_model_performance()


@app.get("/api/pipeline-stats", dependencies=[Depends(require_api_key)])
def api_pipeline_stats(hours: int = 24):
    """Datensammlung (#Pipeline-Timing): Durchschnitts-/Min-/Max-Dauer je
    Verarbeitungsphase eines Poll-Zyklus der letzten `hours` Stunden - macht
    Bottlenecks sichtbar (z.B. eine langsame Quelle oder viele Claude-Calls)."""
    return get_pipeline_statistics(hours=hours)


@app.get("/api/hourly-performance", dependencies=[Depends(require_api_key)])
def api_hourly_performance():
    """Datensammlung (#Time-of-Day): Trefferquote/Durchschnittsrendite je Alarm-STUNDE
    (UTC) - zeigt, ob der Bot zu bestimmten Tageszeiten systematisch besser/schlechter
    liegt (z.B. weil dort andere Quellen/Themen dominieren). Nur mit
    ENABLE_PRICE_TRACKING befuellt."""
    return {"by_hour_utc": get_hourly_performance()}


@app.get("/api/gap-impact", dependencies=[Depends(require_api_key)])
def api_gap_impact(threshold_pct: float = 3.0):
    """Datensammlung (#Gap-Tracking): Trefferquote von Alerts mit grossem
    Uebernacht-/Vorboersen-Gap (|gap_pct| >= threshold_pct) im Vergleich zu allen
    anderen - zeigt, ob bereits stark gegappte Ticker ein schlechteres Signal sind.
    Nur mit ENABLE_PRICE_TRACKING befuellt."""
    return analyze_gap_impact(large_gap_threshold_pct=threshold_pct)


@app.get("/api/ensemble-status", dependencies=[Depends(require_api_key)])
def api_ensemble_status():
    """Trainingsstatus des Ensemble-Modells (#Ensemble-Model, siehe app/ensemble.py):
    ob genug Daten vorliegen, um es zu aktivieren, und falls ja die Trainingsgroesse.
    Nur mit ENABLE_ENSEMBLE_MODEL + ENABLE_PRICE_TRACKING befuellt."""
    if not config.ENABLE_ENSEMBLE_MODEL:
        return {"enabled": False}
    model = ensemble.get_model(config.ENSEMBLE_MIN_TRAINING_SAMPLES, config.ENSEMBLE_RETRAIN_SECONDS)
    if model is None:
        return {
            "enabled": True,
            "trained": False,
            "min_training_samples": config.ENSEMBLE_MIN_TRAINING_SAMPLES,
        }
    return {
        "enabled": True,
        "trained": True,
        "training_samples": model["n"],
        "hit_docs": model["hit_docs"],
        "miss_docs": model["miss_docs"],
        "vocab_size": model["vocab_size"],
    }


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


class BacktestRequest(BaseModel):
    start: str
    end: str
    # Mehrere Horizonte werden aus DERSELBEN Klassifikation ausgewertet (siehe
    # app/backtest.py: evaluate_ticker_outcomes_multi) - kostet also nicht mehr als ein
    # einzelner Horizont, beantwortet aber "welcher Haltezeitraum funktioniert besser".
    horizon_days: list[int] = [1, 3, 5, 10]
    max_calls: int = 30
    execute: bool = False


async def _run_backtest_subprocess(cmd: list[str]) -> dict:
    """Fuehrt app/backtest.py in einem EIGENEN Prozess aus, statt run_backtest()
    direkt hier zu importieren/aufzurufen. Grund: run_backtest() isoliert sich vom
    Produktions-Betrieb, indem es die globale app.db.DB_PATH auf eine temporaere Datei
    umbiegt (siehe app/backtest.py-Docstring) - fuer das eigenstaendige CLI-Tool ist
    das unproblematisch (frischer Prozess, frischer Modul-Zustand), aber DIESER
    Dashboard-Prozess laesst gleichzeitig den echten Orchestrator-Loop laufen
    (run_forever(), siehe startup()), der ueber genau dieselbe globale DB_PATH
    dauerhaft liest/schreibt. Ein In-Prozess-Aufruf wuerde also fuer die Laufzeit des
    Backtests (und danach dauerhaft) den Live-Betrieb auf die Backtest-DB umleiten.
    Ein Subprozess haelt beides sauber getrennt, ohne app/db.py auf eine
    kontextabhaengige Verbindung umbauen zu muessen."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(_REPO_ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=BACKTEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(
            status_code=504,
            detail=f"Backtest nach {BACKTEST_TIMEOUT_SECONDS}s abgebrochen (Zeitlimit) - "
                   "kleineren Zeitraum oder weniger --max-calls versuchen.",
        )
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Backtest-Subprozess fehlgeschlagen: {stderr.decode(errors='replace')[-1500:]}",
        )
    try:
        # app.backtest.main() gibt GENAU EIN JSON-Objekt auf stdout aus (siehe
        # print(json.dumps(report, ...)) dort) - das ist die einzige Ausgabe des CLI-Tools.
        return json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500, detail=f"Backtest-Ausgabe nicht als JSON lesbar: {exc}"
        ) from exc


@app.post("/api/backtest", dependencies=[Depends(require_api_key)])
async def api_backtest(req: BacktestRequest):
    """Startet app/backtest.py fuer einen Zeitraum und liefert den Report als JSON.
    Ohne execute=True (Standard) macht das NIE einen echten Claude-Call - siehe
    app/backtest.py-Docstring. Mit execute=True kostet das echtes Geld, begrenzt durch
    max_calls (hier zusaetzlich hart gedeckelt, siehe BACKTEST_MAX_CALLS_LIMIT)."""
    try:
        start = datetime.date.fromisoformat(req.start)
        end = datetime.date.fromisoformat(req.end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Ungueltiges Datum: {exc}") from exc
    if end < start:
        raise HTTPException(status_code=400, detail="'end' darf nicht vor 'start' liegen.")
    if (end - start).days > BACKTEST_MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Zeitraum zu gross (max. {BACKTEST_MAX_RANGE_DAYS} Tage) - schuetzt vor "
                   "einem versehentlich sehr langen Lauf ueber das Dashboard.",
        )

    # Deckel unabhaengig von den Eingabefeldern: max. Anzahl Horizonte, jeder einzelne
    # Horizont max. BACKTEST_MAX_HORIZON_DAYS, doppelte/negative Werte bereinigt.
    horizons = sorted({min(max(int(h), 1), BACKTEST_MAX_HORIZON_DAYS) for h in req.horizon_days})[
        :BACKTEST_MAX_HORIZONS_COUNT
    ]
    if not horizons:
        horizons = [3]
    max_calls = min(max(req.max_calls, 0), BACKTEST_MAX_CALLS_LIMIT)

    if _backtest_lock.locked():
        raise HTTPException(
            status_code=409, detail="Es laeuft bereits ein Backtest - bitte warten, bis er fertig ist."
        )

    cmd = [
        sys.executable, "-m", "app.backtest",
        "--start", start.isoformat(), "--end", end.isoformat(),
        "--horizon-days", ",".join(str(h) for h in horizons), "--max-calls", str(max_calls),
    ]
    if req.execute:
        cmd.append("--execute")

    async with _backtest_lock:
        return await _run_backtest_subprocess(cmd)


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
