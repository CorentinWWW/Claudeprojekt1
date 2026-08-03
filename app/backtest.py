"""Backtest-Engine: spielt einen historischen Zeitraum durch dieselbe Gating-Logik wie
die Live-Pipeline (Vorfilter -> Duplikat-Erkennung -> Claude-Klassifikation ->
is_alert_worthy()/actionable_tickers() aus app/orchestrator.py) und wertet aus, wie sich
die aufgerufenen Ticker in den Handelstagen danach tatsaechlich bewegt haben -
aufgeschluesselt nach Marktkapitalisierungs-Klasse (app/prices.py: MARKET_CAP_TIERS),
um die These "Small-Caps haben weniger Verzoegerungs-Nachteil" mit echten Daten zu
pruefen statt nur zu behaupten.

WICHTIG, ehrlich (keine Beschoenigung):

1. Kostenkontrolle: dieser Backtest laeuft NIE gegen die Produktions-Datenbank (siehe
   run_backtest: eigene/isolierte DB_PATH) und macht NIEMALS einen echten Claude-Call
   ohne das explizite --execute-Flag - ohne dieses Flag liefert er ausschliesslich
   einen Kostenvoranschlag (estimate_call_cost_usd, grob aus den tatsaechlichen
   Prompt-Bestandteilen hergeleitet). Mit --execute begrenzt --max-calls die Anzahl
   ECHTER Calls pro Lauf hart. Das normale Tages-Limit der Live-Pipeline
   (MAX_CLASSIFICATIONS_PER_DAY) gilt hier NICHT (classify() wird mit
   _bypass_daily_cap=True aufgerufen) - genau deshalb braucht es die eigene,
   purpose-built Obergrenze.

2. Historische Abdeckung: ob/wie weit GDELTs DOC-2.0-API tatsaechlich rueckwirkend
   Daten liefert, ist von hier aus nicht verifizierbar (siehe
   app/sources/news_gdelt.py:fetch_range) - ein leerer/kleiner raw_fetched-Wert fuer
   einen weit zurueckliegenden Zeitraum ist das ehrliche Signal dafuer, kein Bug.

3. Ausfuehrungs-Horizont: die Live-Pipeline bewertet ein Ergebnis ueber ein kurzes
   Intraday-Fenster (PRICE_OUTCOME_HORIZON_MINUTES, Standard 60 Minuten). Fuer die
   Vergangenheit liefern die hier genutzten kostenlosen Quellen (Stooq/Yahoo, siehe
   app/prices.py) aber nur TAGES-Schlusskurse, keine historischen Intraday-Kurse - der
   Backtest misst die Bewegung deshalb zwangslaeufig ueber HANDELSTAGE
   (--horizon-days, Standard '1,3,5,10' - kommagetrennt, ALLE aus DERSELBEN
   Klassifikation ausgewertet, kostet also nicht mehr als ein einzelner Horizont,
   siehe evaluate_ticker_outcomes_multi), nicht Minuten. Das ist keine gleichwertige
   Nachbildung des Live-Verhaltens, sondern die beste mit kostenlosen Daten moegliche
   Naeherung - wird im Report als horizon_days/by_horizon explizit ausgewiesen, nicht
   verschleiert.

4. Vereinfachungen ggue. der Live-Pipeline (bewusst, fuer vorhersehbare Kosten): keine
   Grenzfall-Eskalation (ENABLE_BORDERLINE_ESCALATION) - haette variable statt
   planbare Kosten pro Call zur Folge. Kein Themen-Kontext ueber mehrere Meldungen
   hinweg (jede Meldung wird unabhaengig bewertet) - related_topic_id bleibt daher
   immer None. Nur GDELT als Quelle (RSS/Truth Social/Fed-Audio haben keine
   historische Abfrage-API).

Aufruf: python -m app.backtest --start 2026-06-01 --end 2026-06-07 [--execute]
"""
import argparse
import asyncio
import datetime
import json
import logging
import random
import tempfile
from dataclasses import dataclass
from typing import Optional

import httpx

from app import prices
from app.classifier import CLASSIFY_TOOL, DailyCapExceeded, _system_prompt, classify
from app.config import CLAUDE_MODEL
from app.market_hours import current_market_date
from app.orchestrator import _partition_duplicates, actionable_tickers, is_alert_worthy, is_high_priority
from app.prefilter import looks_market_relevant
from app.sources.news_alphavantage import AlphaVantageError
from app.sources.news_alphavantage import fetch_range as av_fetch_range
from app.sources.news_gdelt import fetch_range

logger = logging.getLogger(__name__)

# --- Kostenschaetzung ---------------------------------------------------------------
# classify() gibt die tatsaechliche Claude-Token-Nutzung nicht zurueck (der live
# genutzte Rueckgabewert ist die geparste Classification, keine Antwort mit .usage) -
# daher nur eine Naeherung: Zeichen der bekannten festen Prompt-Bestandteile
# (System-Prompt + Tool-Schema, siehe app/classifier.py) plus Artikeltext, ueber die
# gaengige Faustregel ~4 Zeichen/Token fuer englischen Text in Token umgerechnet.
# PAUSCHAL_OUTPUT_TOKENS ist ein grosszuegig (eher zu hoch als zu niedrig) geschaetzter
# Erfahrungswert fuer die kompakte tool_use-JSON-Antwort - dafuer gibt es keine
# vergleichbare feste Referenz wie beim Input.
CHARS_PER_TOKEN_ESTIMATE = 4.0
PAUSCHAL_OUTPUT_TOKENS = 350

# Preise in USD je 1M Token (Stand: siehe README/claude-api-Skill-Cache). Nur Modelle
# eingetragen, die in diesem Projekt tatsaechlich konfigurierbar sind (CLAUDE_MODEL /
# CLAUDE_ESCALATION_MODEL, siehe app/config.py).
_MODEL_PRICING_USD_PER_MILLION = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}

_prompt_overhead_chars_cache: Optional[int] = None


def _prompt_overhead_chars() -> int:
    """Zeichenlaenge der festen Prompt-Bestandteile (System-Prompt + Tool-Schema) -
    einmal berechnet und gecacht, da sie sich waehrend eines Laufs nicht aendern."""
    global _prompt_overhead_chars_cache
    if _prompt_overhead_chars_cache is None:
        _prompt_overhead_chars_cache = len(_system_prompt()) + len(json.dumps(CLASSIFY_TOOL))
    return _prompt_overhead_chars_cache


def _pricing_for_model(model: str) -> tuple[float, float]:
    if model in _MODEL_PRICING_USD_PER_MILLION:
        return _MODEL_PRICING_USD_PER_MILLION[model]
    logger.warning(
        "Keine hinterlegte Preistabelle fuer Modell %s - schaetze mit Haiku-4.5-Preisen "
        "(zu niedrig, falls tatsaechlich ein teureres Modell konfiguriert ist).", model,
    )
    return _MODEL_PRICING_USD_PER_MILLION["claude-haiku-4-5"]


def estimate_call_cost_usd(article_text: str, model: str = CLAUDE_MODEL) -> float:
    """Grobe (siehe Modul-Docstring Punkt 1), aber aus den tatsaechlichen
    Prompt-Bestandteilen hergeleitete Kostenschaetzung fuer EINEN einzelnen
    Claude-Klassifikations-Call."""
    input_price, output_price = _pricing_for_model(model)
    input_chars = _prompt_overhead_chars() + len(article_text or "")
    input_tokens = input_chars / CHARS_PER_TOKEN_ESTIMATE
    return (
        input_tokens / 1_000_000 * input_price
        + PAUSCHAL_OUTPUT_TOKENS / 1_000_000 * output_price
    )


# --- Ergebnis-Auswertung --------------------------------------------------------------
@dataclass
class TickerOutcome:
    ticker: str
    direction: str
    confidence: float
    published_at: float
    entry_date: str
    entry_close: float
    exit_date: str
    exit_close: float
    signed_move_pct: float
    hit: bool
    market_cap_tier: Optional[str] = None


def _find_entry_index(dates: list[str], entry_date: str) -> Optional[int]:
    """Erster Index mit Handelstag-Datum >= entry_date (naechster verfuegbarer
    Handelstag ab/nach der Veroeffentlichung - deckt Wochenenden/Feiertage ab). None,
    wenn kein solcher Tag in der Historie vorkommt."""
    for i, d in enumerate(dates):
        if d >= entry_date:
            return i
    return None


def evaluate_ticker_outcome(
    ticker: str,
    direction: Optional[str],
    confidence,
    published_at,
    history: Optional[dict],
    horizon_days: int,
) -> Optional[TickerOutcome]:
    """Wertet aus, wie sich `ticker` `horizon_days` Handelstage nach der Meldung bewegt
    hat (Schlusskurs zu Schlusskurs). signed_move_pct ist bereits auf die Signalrichtung
    projiziert (positiv = die Einschaetzung waere richtig gewesen). None, wenn keine
    Historie vorliegt, kein passender Handelstag gefunden wird, oder die Historie noch
    nicht `horizon_days` Handelstage nach dem Einstieg reicht (Ergebnis schlicht noch
    nicht auswertbar - kein Fehler, siehe outcomes_pending im Report)."""
    if not history or direction not in ("long", "short"):
        return None
    dates = history.get("date") or []
    closes = history.get("close") or []
    if len(dates) != len(closes) or not dates:
        return None
    entry_date = current_market_date(published_at)
    if not entry_date:
        return None
    entry_idx = _find_entry_index(dates, entry_date)
    if entry_idx is None:
        return None
    exit_idx = entry_idx + horizon_days
    if exit_idx >= len(dates):
        return None
    entry_close = closes[entry_idx]
    exit_close = closes[exit_idx]
    if not entry_close or entry_close <= 0:
        return None
    raw_move = (exit_close - entry_close) / entry_close * 100.0
    signed_move = raw_move if direction == "long" else -raw_move
    return TickerOutcome(
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        published_at=published_at,
        entry_date=dates[entry_idx],
        entry_close=entry_close,
        exit_date=dates[exit_idx],
        exit_close=exit_close,
        signed_move_pct=signed_move,
        hit=signed_move > 0,
    )


def evaluate_ticker_outcomes_multi(
    ticker: str,
    direction: Optional[str],
    confidence,
    published_at,
    history: Optional[dict],
    horizon_days_list: list[int],
) -> dict[int, TickerOutcome]:
    """Wertet denselben Ticker/dieselbe Meldung fuer MEHRERE Horizonte gleichzeitig aus -
    nutzt dieselbe (ohnehin schon gecachte) Kurshistorie, kostet also KEINEN
    zusaetzlichen Claude-Call oder Netzwerk-Request pro zusaetzlichem Horizont. Ein
    einziger bezahlter Klassifikations-Lauf beantwortet damit "welcher Haltezeitraum
    funktioniert besser" mit, statt dass jeder Horizont separat (und separat bezahlt)
    ausgewertet werden muesste. {horizon_days: TickerOutcome}, fehlt ein Horizont im
    Ergebnis, war er (noch) nicht auswertbar (siehe evaluate_ticker_outcome)."""
    result: dict[int, TickerOutcome] = {}
    for h in horizon_days_list:
        outcome = evaluate_ticker_outcome(ticker, direction, confidence, published_at, history, h)
        if outcome is not None:
            result[h] = outcome
    return result


def _bucket_summary(outcomes: list[TickerOutcome]) -> dict:
    return {
        "n": len(outcomes),
        "hit_rate": round(sum(o.hit for o in outcomes) / len(outcomes), 3),
        "avg_signed_move_pct": round(sum(o.signed_move_pct for o in outcomes) / len(outcomes), 2),
    }


def sample_within_budget(
    statements: list, budget_usd: float, safety_margin: float = 0.9, seed: int = 42
) -> tuple[list, bool]:
    """Zieht eine ZUFAELLIGE Stichprobe, die in `budget_usd` passt.

    Zufaellig und nicht "die ersten N": bei einem chronologisch sortierten Zeitraum
    wuerde ein Schnitt am Anfang nur die ersten Tage abdecken und den Rest des
    Zeitraums komplett verlieren - die Stichprobe waere dann systematisch verzerrt
    (z.B. auf eine einzelne Marktphase). Fester Seed, damit derselbe Aufruf dasselbe
    Ergebnis liefert und ein Lauf nachvollziehbar bleibt.

    safety_margin: es wird nur bis 90% des Budgets geplant. Die Kostenschaetzung ist
    eine Naeherung aus der Zeichenlaenge (siehe estimate_call_cost_usd), kein echter
    Token-Zaehler - der Puffer verhindert, dass ein paar ungewoehnlich lange Artikel
    das zugesagte Limit reissen.

    Rueckgabe (stichprobe, wurde_gekuerzt)."""
    if budget_usd <= 0 or not statements:
        return statements, False
    effective = budget_usd * safety_margin
    total = sum(estimate_call_cost_usd(s.text) for s in statements)
    if total <= effective:
        return statements, False

    shuffled = list(statements)
    random.Random(seed).shuffle(shuffled)
    picked = []
    spent = 0.0
    for s in shuffled:
        cost = estimate_call_cost_usd(s.text)
        if spent + cost > effective:
            continue
        picked.append(s)
        spent += cost
    # Chronologie wiederherstellen: der Replay soll die Meldungen in der Reihenfolge
    # verarbeiten, in der sie erschienen sind (published_at kann None sein - dann ans
    # Ende, statt beim Sortieren abzustuerzen).
    picked.sort(key=lambda s: s.published_at or float("inf"))
    return picked, True


def _tier_breakdown(outcomes: list[TickerOutcome]) -> dict:
    by_tier: dict[str, list[TickerOutcome]] = {}
    for o in outcomes:
        key = o.market_cap_tier or "unbekannt"
        by_tier.setdefault(key, []).append(o)
    return {tier: _bucket_summary(items) for tier, items in by_tier.items()}


# --- Historischer Abruf ----------------------------------------------------------------
def _daterange_chunks(start: datetime.date, end: datetime.date):
    """Taegliche UTC-Zeitfenster von start bis einschliesslich end - kleinere Fenster
    verkleinern das Risiko, an einem nachrichtenreichen Tag ueber GDELTs
    250-Treffer-Grenze pro Abfrage zu laufen (siehe news_gdelt.GDELT_MAX_RECORDS_PER_QUERY)."""
    day = start
    while day <= end:
        chunk_start = datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.timezone.utc)
        chunk_end = datetime.datetime.combine(day, datetime.time.max, tzinfo=datetime.timezone.utc)
        yield chunk_start, chunk_end
        day += datetime.timedelta(days=1)


async def fetch_all_raw_alphavantage(
    start: datetime.date, end: datetime.date, chunk_days: int = 7
) -> tuple[list, int, int]:
    """Historischer Abruf ueber Alpha Vantage (siehe app/sources/news_alphavantage.py).

    Bewusst in WOCHEN-Chunks statt taeglich wie bei GDELT: der Endpunkt liefert bis zu
    1000 Artikel pro Abfrage, und der kostenlose Tarif erlaubt nur sehr wenige Abfragen
    pro Tag - drei Monate kosten so ~13 Abfragen statt ~90. Rueckgabe wie
    fetch_all_raw(): (statements, chunks_failed, chunks_total)."""
    seen_ids: set = set()
    out = []
    failed = 0
    total = 0
    async with httpx.AsyncClient(timeout=60) as client:
        day = start
        while day <= end:
            chunk_end_day = min(day + datetime.timedelta(days=chunk_days - 1), end)
            total += 1
            chunk_start = datetime.datetime.combine(
                day, datetime.time.min, tzinfo=datetime.timezone.utc
            )
            chunk_end = datetime.datetime.combine(
                chunk_end_day, datetime.time.max, tzinfo=datetime.timezone.utc
            )
            try:
                batch = await av_fetch_range(chunk_start, chunk_end, client=client)
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Alpha-Vantage-Abfrage fuer %s bis %s fehlgeschlagen: %s",
                    day, chunk_end_day, exc,
                )
                # Ein Rate-Limit/Key-Fehler trifft jede weitere Abfrage genauso - dann
                # ist Weitermachen sinnlos und wuerde die restlichen Chunks nur mit
                # derselben Fehlermeldung fluten (Lehre aus dem GDELT-Vorfall).
                if isinstance(exc, AlphaVantageError):
                    logger.error(
                        "Alpha Vantage meldet ein grundsaetzliches Problem - breche den "
                        "Abruf ab, statt es fuer jeden weiteren Zeitraum zu wiederholen."
                    )
                    break
                day = chunk_end_day + datetime.timedelta(days=1)
                continue
            for r in batch:
                if r.source_id in seen_ids:
                    continue
                seen_ids.add(r.source_id)
                out.append(r)
            day = chunk_end_day + datetime.timedelta(days=1)
            if day <= end:
                # Freundlicher Abstand zwischen Abfragen (kein dokumentiertes
                # Sekunden-Limit, aber nach dem GDELT-Vorfall bewusst konservativ).
                await asyncio.sleep(2.0)
    return out, failed, total


async def fetch_all_raw(start: datetime.date, end: datetime.date) -> tuple[list, int, int]:
    """Holt alle GDELT-Artikel im Zeitraum, taeglich gechunked (siehe
    _daterange_chunks), dedupliziert ueber Chunk-Grenzen hinweg per URL. Ein
    fehlschlagender einzelner Tag ueberspringt nur diesen Tag (best-effort, wie der Rest
    der Kursquellen in diesem Projekt) statt den gesamten Lauf abzubrechen.

    Rueckgabe (raw_statements, days_failed, days_total): days_failed macht im Report
    sichtbar, ob ein raw_fetched=0 an fehlender historischer GDELT-Abdeckung liegt
    (Punkt 2 im Modul-Docstring, days_failed=0) oder daran, dass der Abruf trotz Retries
    fehlgeschlagen ist (days_failed>0, meist Rate-Limit) - beides saehe in raw_fetched
    allein identisch aus, bedeutet aber etwas komplett anderes fuer die Interpretation
    des Ergebnisses."""
    seen_ids: set = set()
    out = []
    days_failed = 0
    days_total = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for chunk_start, chunk_end in _daterange_chunks(start, end):
            days_total += 1
            try:
                batch = await fetch_range(chunk_start, chunk_end, client=client)
            except Exception as exc:
                days_failed += 1
                # logger.warning() statt logger.exception(): fetch_range() hat schon mit
                # 5 Retries + langem Backoff versucht (siehe dort) - landet ein Tag
                # trotzdem hier, ist das erwartet-selten und braucht keinen vollen
                # Traceback pro Tag (bei einem laengeren Zeitraum sonst Bildschirmfuellend).
                logger.warning(
                    "GDELT-Abfrage fuer %s fehlgeschlagen (nach Retries), ueberspringe "
                    "diesen Tag: %s", chunk_start.date(), exc,
                )
                continue
            for r in batch:
                if r.source_id in seen_ids:
                    continue
                seen_ids.add(r.source_id)
                out.append(r)
            # GDELT nennt das Limit inzwischen SCHWARZ AUF WEISS in seiner eigenen
            # 429-Antwort: "Please limit requests to one every 5 seconds" - vorher nur
            # vermutet, jetzt bestaetigt (siehe fetch_range()-Docstring). 3s lagen
            # UNTER diesem Minimum - das war der eigentliche Grund, warum ein 30-Tage-
            # Lauf trotz Retries durchgehend scheiterte. 8s als Default (deutliche
            # Sicherheitsmarge, da derselbe Zeitdeckel vermutlich auch den parallel
            # laufenden Live-Poll mit einschliesst, nicht nur diesen Prozess).
            await asyncio.sleep(8.0)
    return out, days_failed, days_total


# --- Hauptlauf ---------------------------------------------------------------------
async def run_backtest(
    start: datetime.date,
    end: datetime.date,
    horizon_days=3,
    max_calls: int = 30,
    execute: bool = False,
    db_path: Optional[str] = None,
    source: str = "gdelt",
    budget_usd: float = 0.0,
) -> dict:
    """Fuehrt den Backtest aus. Ohne execute=True werden NIEMALS Claude-Calls gemacht -
    Rueckgabe ist dann nur der Kostenvoranschlag (siehe Modul-Docstring Punkt 1). Laeuft
    IMMER gegen eine isolierte DB (db_path oder ein automatisch angelegtes Temp-File),
    nie gegen die Produktions-Datenbank.

    horizon_days: ein einzelner int (Standard, ein Horizont) ODER eine Liste mehrerer
    Handelstage-Horizonte (z.B. [1, 3, 5, 10]) - wird ueber evaluate_ticker_outcomes_multi
    aus DERSELBEN Klassifikation ausgewertet, kostet also nicht mehr als ein einzelner
    Horizont (siehe dort). Bei mehreren Horizonten liefert der Report zusaetzlich
    by_horizon; hit_rate/avg_signed_move_pct/by_tier beziehen sich auf den ERSTEN
    (primaeren) Horizont der Liste - so bleibt der Report auch mit nur einem Horizont
    unveraendert lesbar."""
    import app.db as db

    horizons = [horizon_days] if isinstance(horizon_days, int) else sorted(
        {int(h) for h in horizon_days}
    ) or [3]
    primary_horizon = horizons[0]

    if db_path:
        db.DB_PATH = db_path
    else:
        tmp = tempfile.NamedTemporaryFile(prefix="backtest_", suffix=".db", delete=False)
        tmp.close()
        db.DB_PATH = tmp.name
    db.init_db()

    if source == "alphavantage":
        raw, days_failed, days_total = await fetch_all_raw_alphavantage(start, end)
    else:
        raw, days_failed, days_total = await fetch_all_raw(start, end)
    report: dict = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source": source,
        "horizon_days": horizons,
        "db_path": db.DB_PATH,
        "raw_fetched": len(raw),
        "days_failed": days_failed,
        "days_total": days_total,
    }
    # Macht den Unterschied zwischen "GDELT hat fuer den Zeitraum nichts" und "der
    # Abruf ist trotz Retries fehlgeschlagen" sichtbar - beides sieht in raw_fetched
    # allein identisch aus (siehe fetch_all_raw()-Docstring), bedeutet aber etwas
    # komplett anderes: im ersten Fall ist raw_fetched=0 das ehrliche Endergebnis, im
    # zweiten ist es ein unvollstaendiger Lauf, der wiederholt werden sollte.
    fetch_note = (
        f"{days_failed}/{days_total} Tag(e) konnten trotz Retry nicht von GDELT "
        "abgerufen werden (Rate-Limit, GDELT selbst nennt max. 1 Anfrage/5s) - "
        "raw_fetched ist dadurch moeglicherweise niedriger als die tatsaechliche "
        "Abdeckung. Etwas warten (das Limit erholt sich von selbst) und bei einem "
        "erneuten Lauf ggf. einen kleineren Zeitraum probieren."
        if days_failed else ""
    )

    kept = [r for r in raw if looks_market_relevant(r.text)]
    report["after_prefilter"] = len(kept)
    if not kept:
        report["after_dedup"] = 0
        report["estimated_calls"] = 0
        report["estimated_cost_usd"] = 0.0
        report["dry_run"] = not execute
        report["note"] = (
            "Keine Artikel nach dem Vorfilter uebrig - siehe raw_fetched fuer die "
            "GDELT-Rohtreffer im Zeitraum." + (f" {fetch_note}" if fetch_note else "")
        )
        return report

    to_classify, _dupes = _partition_duplicates(kept)
    report["after_dedup"] = len(to_classify)
    report["cost_without_budget_usd"] = round(
        sum(estimate_call_cost_usd(r.text) for r in to_classify), 4
    )

    # Budget-Stichprobe VOR der Prioritaets-Sortierung: sonst wuerde die Zufallsauswahl
    # aus einer bereits nach Wichtigkeit vorsortierten Liste ziehen und die Stichprobe
    # waere nicht mehr repraesentativ fuer das, was die Pipeline im Alltag sieht.
    if budget_usd > 0:
        to_classify, was_sampled = sample_within_budget(to_classify, budget_usd)
        report["budget_usd"] = budget_usd
        report["budget_sampled"] = was_sampled
        if was_sampled:
            report["budget_note"] = (
                f"Zufallsstichprobe: {len(to_classify)} von {report['after_dedup']} "
                f"Artikeln passen ins Budget von {budget_usd:.2f} $ (mit Sicherheitspuffer). "
                "Zufaellig ueber den GESAMTEN Zeitraum gezogen, nicht die ersten N - "
                "sonst waere nur der Anfang des Zeitraums abgedeckt."
            )

    to_classify.sort(key=is_high_priority, reverse=True)
    report["estimated_calls"] = len(to_classify)
    report["estimated_cost_usd"] = round(
        sum(estimate_call_cost_usd(r.text) for r in to_classify), 4
    )

    if not execute:
        report["dry_run"] = True
        report["note"] = (
            "Dry-Run: KEIN Claude-Call ausgefuehrt. Mit --execute (und --max-calls bzw. "
            "--budget-usd als harter Obergrenze) tatsaechlich klassifizieren."
            + (f" {fetch_note}" if fetch_note else "")
        )
        return report

    report["dry_run"] = False
    to_run = to_classify[:max_calls] if max_calls > 0 else to_classify
    report["truncated_by_max_calls"] = max_calls > 0 and len(to_classify) > max_calls

    outcomes_by_horizon: dict[int, list[TickerOutcome]] = {h: [] for h in horizons}
    alert_worthy_count = 0
    ticker_call_count = 0
    history_cache: dict[str, Optional[dict]] = {}
    actual_cost = 0.0
    calls_made = 0

    for raw_stmt in to_run:
        # Zweite, UNABHAENGIGE Reissleine: sample_within_budget() plant im Voraus anhand
        # einer Schaetzung - hier wird waehrend des Laufs mitgezaehlt und hart gestoppt.
        # Ohne das koennte eine zu niedrige Schaetzung das zugesagte Limit ueberschreiten,
        # und der Nutzer haette echtes Geld ausgegeben, das er nicht freigegeben hat.
        if budget_usd > 0 and actual_cost + estimate_call_cost_usd(raw_stmt.text) > budget_usd:
            report["stopped_by_budget"] = True
            logger.warning(
                "Budget von %.2f $ erreicht (%.4f $ ausgegeben) - breche nach %d Calls ab.",
                budget_usd, actual_cost, calls_made,
            )
            break
        try:
            classification = await classify(raw_stmt.text, recent_context=[], _bypass_daily_cap=True)
        except DailyCapExceeded:
            # Kann bei _bypass_daily_cap=True eigentlich nicht auftreten (siehe
            # classifier.classify) - hier trotzdem defensiv abgefangen statt den
            # gesamten Lauf abzubrechen, falls sich das je aendert.
            logger.warning("Unerwartetes DailyCapExceeded trotz _bypass_daily_cap, breche Lauf ab.")
            break
        except Exception:
            logger.exception("Klassifikation im Backtest fehlgeschlagen fuer: %s", raw_stmt.text[:80])
            continue
        calls_made += 1
        actual_cost += estimate_call_cost_usd(raw_stmt.text)
        db.insert_statement(raw_stmt, classification, claude_model=CLAUDE_MODEL)

        if not is_alert_worthy(classification):
            continue
        alert_worthy_count += 1
        for tc in actionable_tickers(classification):
            ticker_call_count += 1
            ticker = (tc.get("ticker") or "").upper()
            if ticker not in history_cache:
                history_cache[ticker] = await prices.get_history(ticker)
            per_horizon = evaluate_ticker_outcomes_multi(
                ticker,
                tc.get("direction"),
                tc.get("confidence"),
                raw_stmt.published_at,
                history_cache[ticker],
                horizons,
            )
            for h, outcome in per_horizon.items():
                outcomes_by_horizon[h].append(outcome)

    report["classified"] = calls_made
    report["actual_cost_usd"] = round(actual_cost, 4)
    report["alert_worthy"] = alert_worthy_count
    report["ticker_calls_total"] = ticker_call_count
    primary_outcomes = outcomes_by_horizon[primary_horizon]
    # outcomes_resolved/-pending beziehen sich bewusst auf den PRIMAEREN Horizont (nicht
    # "irgendeinen"/"alle") - sonst waere die Zahl bei mehreren Horizonten von der
    # Reihenfolge/Anzahl der angefragten Horizonte abhaengig und nicht mehr konsistent
    # interpretierbar.
    report["outcomes_resolved"] = len(primary_outcomes)
    report["outcomes_pending"] = ticker_call_count - len(primary_outcomes)

    if any(outcomes_by_horizon.values()):
        all_tickers = sorted({o.ticker for items in outcomes_by_horizon.values() for o in items})
        caps = await prices.get_market_caps(all_tickers)
        for items in outcomes_by_horizon.values():
            for o in items:
                o.market_cap_tier = prices.market_cap_tier(caps.get(o.ticker))

        if len(horizons) > 1:
            report["by_horizon"] = {
                str(h): _bucket_summary(items) for h, items in outcomes_by_horizon.items() if items
            }
        if primary_outcomes:
            report["hit_rate"] = round(
                sum(o.hit for o in primary_outcomes) / len(primary_outcomes), 3
            )
            report["avg_signed_move_pct"] = round(
                sum(o.signed_move_pct for o in primary_outcomes) / len(primary_outcomes), 2
            )
            report["by_tier"] = _tier_breakdown(primary_outcomes)
    else:
        report["note"] = report.get("note", "") + (
            " Keine auswertbaren Ergebnisse (kein alarmwuerdiger Ticker hatte "
            "genug Kurshistorie fuer die gewaehlten Horizonte)."
        )

    # Deckt den Erfolgspfad ab (oben nur bei den fruehen return-Pfaden inline
    # angehaengt) - ein teilweise fehlgeschlagener Abruf soll sichtbar bleiben, selbst
    # wenn der Rest des Laufs ganz normal Ergebnisse liefert.
    if fetch_note:
        report["note"] = (report.get("note", "") + " " + fetch_note).strip()

    return report


def _parse_horizon_list(value: str) -> list[int]:
    try:
        horizons = [int(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Ungueltige --horizon-days Liste: {value!r}") from exc
    if not horizons:
        raise argparse.ArgumentTypeError("--horizon-days darf nicht leer sein")
    return horizons


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backtest: spielt einen historischen Zeitraum durch dieselbe "
            "Live-Gating-Logik (is_alert_worthy/actionable_tickers) und wertet die "
            "Ticker-Ergebnisse aus. Ohne --execute wird NIE ein echter Claude-Call "
            "gemacht - siehe Modul-Docstring."
        )
    )
    parser.add_argument("--start", required=True, help="Start YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="Ende YYYY-MM-DD (UTC, inklusive)")
    parser.add_argument(
        "--horizon-days", type=_parse_horizon_list, default=[1, 3, 5, 10],
        help="Kommagetrennte Liste von Handelstage-Horizonten (Standard '1,3,5,10') - "
             "ALLE werden aus DERSELBEN Klassifikation ausgewertet (siehe "
             "evaluate_ticker_outcomes_multi), kostet also nicht mehr als ein einzelner "
             "Horizont. Der Report zeigt zusaetzlich by_horizon zum Vergleich; "
             "hit_rate/by_tier beziehen sich auf den ERSTEN (kleinsten) Horizont.",
    )
    parser.add_argument(
        "--max-calls", type=int, default=30,
        help="Harte Obergrenze fuer ECHTE Claude-Calls pro Lauf (0 = keine Anzahl-Grenze, "
             "dann zaehlt nur --budget-usd). Nur mit --execute relevant.",
    )
    parser.add_argument(
        "--source", choices=("gdelt", "alphavantage"), default="gdelt",
        help="Historische Nachrichtenquelle. 'alphavantage' braucht ALPHAVANTAGE_API_KEY "
             "in der .env (kostenlos) und reicht Jahre zurueck; 'gdelt' braucht keinen "
             "Key, ist aber anfaellig fuer laengere IP-Sperren (siehe README).",
    )
    parser.add_argument(
        "--budget-usd", type=float, default=0.0,
        help="Hartes Kostenlimit in USD (0 = aus). Passen nicht alle Artikel hinein, wird "
             "eine ZUFAELLIGE Stichprobe ueber den gesamten Zeitraum gezogen (nicht die "
             "ersten N - das wuerde nur den Anfang abdecken). Wirkt doppelt: bei der "
             "Planung und als Abbruch waehrend des Laufs.",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Tatsaechlich Claude-Calls ausfuehren (kostet echtes Geld, siehe zuerst den "
             "Kostenvoranschlag ohne dieses Flag). Ohne --execute: reiner Dry-Run.",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Eigene SQLite-Datei statt eines automatisch angelegten Temp-Files. NIEMALS "
             "die Produktions-DB angeben.",
    )
    parser.add_argument("--out", default=None, help="Report zusaetzlich als JSON-Datei schreiben")
    args = parser.parse_args()

    start = datetime.date.fromisoformat(args.start)
    end = datetime.date.fromisoformat(args.end)
    if end < start:
        parser.error("--end darf nicht vor --start liegen")

    report = asyncio.run(
        run_backtest(
            start, end, horizon_days=args.horizon_days, max_calls=args.max_calls,
            execute=args.execute, db_path=args.db_path,
            source=args.source, budget_usd=args.budget_usd,
        )
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nReport geschrieben nach {args.out}")


if __name__ == "__main__":
    main()
