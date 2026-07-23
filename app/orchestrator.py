import asyncio
import datetime
import logging
import re
import time

from anthropic import AuthenticationError, NotFoundError, PermissionDeniedError

from app.classifier import CONTEXT_SNIPPET_MAX_CHARS, DailyCapExceeded, FALLBACK_CLASSIFICATION, classify
from app.config import (
    ALERT_CONFIDENCE_THRESHOLD,
    ALERT_DIGEST_THRESHOLD,
    ALERT_MIN_CONVICTION,
    ALERT_MIN_EXPECTED_MOVE_PCT,
    ALERT_MIN_TICKER_CONFIDENCE,
    BLOCKLIST_TICKERS,
    CLAUDE_ESCALATION_MODEL,
    DEDUP_SIMILARITY_THRESHOLD,
    DIVERGENCE_WARN_PCT,
    ENABLE_BORDERLINE_ESCALATION,
    ENABLE_CONVICTION_SCORE,
    ENABLE_HISTORICAL_HITRATE,
    ENABLE_KELLY_SUGGESTION,
    ENABLE_NEWS,
    ENABLE_PREFILTER,
    ENABLE_PRICE_TRACKING,
    ENABLE_RISK_LEVELS,
    ENABLE_TECHNICALS,
    ENABLE_TRUTH_SOCIAL,
    ENABLE_WEEKLY_DIGEST,
    ESCALATION_BAND,
    ENABLE_GAP_PREDICTION,
    GAP_MIN_EXPECTED_MOVE_PCT,
    GAP_NEAR_CLOSE_MINUTES,
    LATE_MOVE_WARN_PCT,
    MAX_ALERTS_PER_HOUR,
    MAX_CLASSIFICATIONS_PER_DAY,
    MAX_CONCURRENT_CLASSIFICATIONS,
    MAX_NEWS_AGE_MINUTES,
    PAPER_TRADING,
    POLL_INTERVAL_SECONDS,
    PRICE_OUTCOME_HORIZON_MINUTES,
    PRIORITY_CLASSIFICATIONS_PER_DAY,
    QUIET_HOURS,
    QUIET_HOURS_MIN_CONVICTION,
    QUIET_HOURS_TZ,
    SECTOR_CLUSTER_MIN,
    SECTOR_CLUSTER_WINDOW_HOURS,
    TECHNICALS_CONVICTION_WEIGHT,
    TECHNICALS_REQUIRE_AGREEMENT,
    TELEGRAM_STARTUP_NOTICE,
    TICKER_ALERT_COOLDOWN_MINUTES,
    TICKER_UNIVERSE,
    TOPIC_CONTEXT_MAX_ITEMS,
    TOPIC_CONTEXT_WINDOW_HOURS,
    WATCHLIST_SECTORS,
    WATCHLIST_TICKERS,
    WEEKLY_DIGEST_MIN_HOUR,
    WEEKLY_DIGEST_WEEKDAY,
)
from app.db import (
    Classification,
    RawStatement,
    count_alerted_statements_since,
    get_corroboration_count,
    get_dedup_candidates,
    get_kelly_inputs,
    get_known_source_ids,
    get_last_alert_direction,
    get_outcomes_awaiting_followup,
    get_pending_alerts,
    get_performance_stats,
    get_recent_alerted,
    get_recent_alerted_sector_counts,
    get_ticker_hitrate,
    get_topic_thread,
    init_db,
    insert_statement,
    mark_alert_sent,
    record_alert_baseline,
    record_ticker_alert,
    set_outcome_followup,
    ticker_in_cooldown,
    try_claim_meta_key,
)
from app import indicators, paper_trading, prices
from app.market_hours import minutes_until_close, us_market_session
from app.prefilter import looks_market_relevant
from app.scoring import (
    conviction_score,
    has_hedge_language,
    high_volatility_text,
    hour_in_window,
    kelly_fraction,
    parse_hour_window,
    position_tier,
    predict_gap,
)
from app.sources.news_gdelt import GdeltNewsSource
from app.sources.news_rss import RssNewsSource
from app.sources.truth_social import TruthSocialSource
from app.telegram_alert import (
    send_alert,
    send_digest_alert,
    send_startup_notice,
    send_text,
    send_weekly_digest,
)
from app.util import text_similarity

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - nur falls die Zeitzonendaten fehlen
    ZoneInfo = None

# Permanente Konfigurationsfehler der Claude-API: ein kaputter/widerrufener API-Key
# (401), fehlende Berechtigung (403) oder ein nicht (mehr) existierendes Modell (404)
# reparieren sich nicht von selbst - im Gegensatz zu transienten Fehlern (Timeouts,
# 429, 529), die das SDK selbst retried und die den Lauf nicht abbrechen sollen.
PERMANENT_CLAUDE_ERRORS = (AuthenticationError, PermissionDeniedError, NotFoundError)

logger = logging.getLogger(__name__)

# In-memory Health-Status pro Quelle, fuer /api/health. Muss nicht persistiert werden -
# nach einem Neustart baut sich der Status einfach durch die naechsten Polls neu auf.
source_health: dict[str, dict] = {}
run_health: dict = {
    "started_at": None,
    "loop_restarts": 0,
    "last_cycle_at": None,
    # Zyklus-Timing (#11): letzte Dauer + die letzten N Dauern fuer einen gleitenden
    # Durchschnitt in /api/health (macht sichtbar, ob ein Poll-Zyklus langsam wird).
    "last_cycle_seconds": None,
    "recent_cycle_seconds": [],
}
_MAX_CYCLE_SAMPLES = 20


def active_gates() -> dict:
    """Uebersicht (#12), welche optionalen, verhaltensaendernden Gates/Features gerade
    aktiv sind - fuer den Startup-Log und /api/health. So ist eine Fehlkonfiguration
    ('warum kommen keine Alerts mehr?') sofort sichtbar, statt sich in vielen einzelnen
    Env-Variablen zu verstecken. None/false = aus."""
    return {
        "prefilter": ENABLE_PREFILTER,
        "stale_filter_minutes": MAX_NEWS_AGE_MINUTES or None,
        "ticker_cooldown_minutes": TICKER_ALERT_COOLDOWN_MINUTES or None,
        "quiet_hours": QUIET_HOURS or None,
        "min_conviction": ALERT_MIN_CONVICTION or None,
        "max_alerts_per_hour": MAX_ALERTS_PER_HOUR or None,
        "ticker_universe": len(TICKER_UNIVERSE) or None,
        "min_expected_move_pct": ALERT_MIN_EXPECTED_MOVE_PCT or None,
        "watchlist": bool(WATCHLIST_TICKERS or WATCHLIST_SECTORS),
        "blocklist": len(BLOCKLIST_TICKERS) or None,
        "price_tracking": ENABLE_PRICE_TRACKING,
        "paper_trading": PAPER_TRADING,
        "technicals": ENABLE_TECHNICALS,
        "technicals_require_agreement": TECHNICALS_REQUIRE_AGREEMENT if ENABLE_TECHNICALS else None,
        "late_move_warn_pct": LATE_MOVE_WARN_PCT or None,
        "gap_prediction": ENABLE_GAP_PREDICTION,
        "borderline_escalation": ENABLE_BORDERLINE_ESCALATION,
        "weekly_digest": ENABLE_WEEKLY_DIGEST,
    }


def _record_cycle_duration(seconds: float) -> None:
    run_health["last_cycle_seconds"] = seconds
    samples = run_health["recent_cycle_seconds"]
    samples.append(seconds)
    if len(samples) > _MAX_CYCLE_SAMPLES:
        del samples[: len(samples) - _MAX_CYCLE_SAMPLES]

# Wortgrenzen-Muster fuer die "besonders wichtig"-Einstufung (siehe is_high_priority).
# Bewusst STRENGER als der GDELT-Ingestion-Filter (der schon 'market'/'stock'/'trade'
# etc. abdeckt) - hier zaehlen nur die haertesten, unmittelbar marktbewegenden Themen,
# unabhaengig davon wer/was sie ausloest (Politik, Zentralbanken, Unternehmen), damit
# die knappe Prioritaets-Reserve nicht sofort von jeder markt-nahen Meldung
# aufgebraucht wird. Rein aus billigen Textsignalen bestimmt (KEIN Claude-Call), da die
# Wichtigkeit ueber das Tages-Limit entscheiden muss, BEVOR ein Call ausgegeben wird.
_HIGH_PRIORITY_PATTERN = re.compile(
    r"\b("
    r"tariff|tariffs|zoll|zoelle|zölle|sanction|sanctions|sanktion|"
    r"federal reserve|interest rate|rate cut|rate hike|zinsen|leitzins|"
    r"executive order|shutdown|default|embargo|nationaliz|verstaatlich|"
    r"bailout|stimulus|export ban|import ban|price cap|"
    r"bankruptcy|insolvenz|recall|rückruf|cyberattack|cyber attack|"
    r"war|krieg|invasion|merger|acquisition|takeover|übernahme|fusion|downgrade"
    r")\b",
    re.IGNORECASE,
)


def is_high_priority(raw) -> bool:
    """Billige, Claude-freie Einschaetzung, ob eine Meldung wichtig genug ist, um die
    Prioritaets-Reserve oberhalb des normalen Tages-Limits nutzen zu duerfen. True bei
    (a) direkten Posts von ueberwachten Original-Quellen (z.B. Truth Social - deren
    unmittelbare eigene Worte statt einer medial paraphrasierten Meldung, ohnehin selten)
    ODER (b) einem der haertesten Wirtschafts-Signalwoerter im Text - unabhaengig davon,
    von wem die Meldung stammt. Bewusst konservativ - lieber ein paar wichtige Meldungen
    verpassen als die Reserve verwaessern."""
    if getattr(raw, "source", "") == "truth_social":
        return True
    return bool(_HIGH_PRIORITY_PATTERN.search(raw.text or ""))


def _strongest_ticker_confidence(classification) -> float:
    """Hoechste Pro-Ticker-Konfidenz unter den Tickern mit klarer Long/Short-Richtung
    (0.0, wenn es keinen solchen Ticker gibt). Robust gegen alte DB-Zeilen ohne
    confidence-Feld (dort None -> zaehlt als 0.0) und gegen fehlerhafte Eintraege."""
    best = 0.0
    for tc in classification.ticker_calls or []:
        if not isinstance(tc, dict):
            continue
        if tc.get("direction") not in ("long", "short"):
            continue
        conf = tc.get("confidence")
        if isinstance(conf, (int, float)) and conf > best:
            best = float(conf)
    return best


def actionable_tickers(classification) -> list[dict]:
    """Die konkret handelbaren Ticker einer Meldung: klare Long/Short-Richtung,
    Pro-Ticker-Konfidenz >= Schwelle und nicht auf der Blockliste. Basis fuer die
    Alarm-Entscheidung, die Chart-Buttons und das Preis-Tracking (eine gemeinsame
    Definition, damit alle drei exakt dieselben Ticker meinen). Bei deaktiviertem
    Praezisions-Filter (Schwelle <= 0) zaehlen alle Ticker mit klarer Richtung."""
    result = []
    for tc in classification.ticker_calls or []:
        if not isinstance(tc, dict):
            continue
        if tc.get("direction") not in ("long", "short"):
            continue
        ticker = (tc.get("ticker") or "").upper()
        if not ticker or ticker in BLOCKLIST_TICKERS:
            continue
        # Liquiditaets-/Universum-Gate (#8): ist ein handelbares Universum konfiguriert,
        # zaehlen nur Ticker daraus - obskure/illiquide Kuerzel loesen keinen Alert aus.
        # Leeres Universum = kein Filter (altes Verhalten).
        if TICKER_UNIVERSE and ticker not in TICKER_UNIVERSE:
            continue
        conf = tc.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else 0.0
        if ALERT_MIN_TICKER_CONFIDENCE > 0 and conf < ALERT_MIN_TICKER_CONFIDENCE:
            continue
        result.append(tc)
    return result


def _passes_watchlist(classification, actionable: list[dict]) -> bool:
    """Persoenlicher Filter: ist keine Watchlist gesetzt, passt alles. Sonst muss
    entweder ein handelbarer Ticker in WATCHLIST_TICKERS sein oder ein betroffener
    Sektor auf WATCHLIST_SECTORS passen (Teilstring, case-insensitive)."""
    if not WATCHLIST_TICKERS and not WATCHLIST_SECTORS:
        return True
    if any((tc.get("ticker") or "").upper() in WATCHLIST_TICKERS for tc in actionable):
        return True
    sectors = [str(s).lower() for s in (classification.sectors or [])]
    return any(w in sec for w in WATCHLIST_SECTORS for sec in sectors)


def is_alert_worthy(classification) -> bool:
    """Zentrale, EINZIGE Stelle, die entscheidet, ob eine Meldung eine Telegram-
    Nachricht ausloest. Es wird bewusst nur noch bei den sichersten, direkt
    handelbaren Signalen alarmiert: die Meldung muss marktrelevant sein UND mindestens
    einen konkreten Boersenticker mit klarer Long/Short-Richtung und einer Pro-Ticker-
    Konfidenz >= ALERT_MIN_TICKER_CONFIDENCE enthalten (und den persoenlichen
    Watchlist-Filter passieren). Reine 'marktrelevant'-Meldungen ohne konkrete,
    hochsichere Aktie loesen KEINEN Alert mehr aus. Wird an allen Alarm-
    Entscheidungspunkten verwendet (poll_once, Resend-Pfad, manuelle Tests), damit die
    strenge Schwelle nirgends umgangen werden kann - insbesondere durfte der Resend-Pfad
    (get_pending_alerts) frueher jede marktrelevante Meldung einen Zyklus spaeter doch
    noch ungefiltert alarmieren."""
    if classification is None or not classification.is_market_relevant:
        return False
    if classification.confidence < ALERT_CONFIDENCE_THRESHOLD:
        return False
    # Optionales Mindest-Erwartungswert-Gate (#3): nur alarmieren, wenn Claudes grobe
    # Schaetzung der erwarteten Bewegung >= Schwelle ist. 0 = aus (Default). Eine fehlende
    # Schaetzung (None) gilt bei aktiver Schwelle als "zu klein" und wird herausgefiltert.
    if ALERT_MIN_EXPECTED_MOVE_PCT > 0:
        move = classification.expected_move_pct
        if not isinstance(move, (int, float)) or move < ALERT_MIN_EXPECTED_MOVE_PCT:
            return False
    if ALERT_MIN_TICKER_CONFIDENCE <= 0:
        # Praezisions-Filter deaktiviert: altes Verhalten (jede marktrelevante Meldung
        # oberhalb von ALERT_CONFIDENCE_THRESHOLD alarmiert) - aber der Watchlist-Filter
        # gilt weiterhin, falls gesetzt.
        return _passes_watchlist(classification, actionable_tickers(classification))
    actionable = actionable_tickers(classification)
    if not actionable:
        return False
    return _passes_watchlist(classification, actionable)


def build_sources():
    sources = []
    if ENABLE_NEWS:
        sources.append(GdeltNewsSource())
        sources.append(RssNewsSource())
    if ENABLE_TRUTH_SOCIAL:
        sources.append(TruthSocialSource())
    for s in sources:
        source_health.setdefault(
            s.name,
            {
                "last_poll_at": None,
                "last_success_at": None,
                "last_error": None,
                "last_error_at": None,
                "total_fetched": 0,
                # Wie viele Roh-Meldungen dieser Quelle der billige Vorfilter (#Kosten,
                # siehe app/prefilter.py) verworfen hat, bevor ein Claude-Call anfiel -
                # macht die Kostenersparnis in /api/health sichtbar.
                "prefiltered": 0,
                # Wie viele Meldungen als zu alt (MAX_NEWS_AGE_MINUTES, #14) verworfen
                # wurden, bevor ein Claude-Call anfiel.
                "stale": 0,
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
    # Batch statt einer DB-Verbindung/Query pro Statement (relevant bei einem
    # Nachrichtenschub mit vielen gleichzeitigen Statements in einem Zyklus).
    known_ids = get_known_source_ids([r.source_id for r in raw_statements])
    candidates = get_dedup_candidates()

    to_classify = []
    duplicate_pairs = []  # (duplicate_raw, primary_raw_or_dbrow)
    batch: list[tuple] = []  # (text, raw) bereits akzeptierter Statements dieser Charge

    for raw in raw_statements:
        if raw.source_id in known_ids:
            continue

        db_duplicate = None
        for cand in candidates:
            if text_similarity(raw.text, cand["text"]) >= DEDUP_SIMILARITY_THRESHOLD:
                db_duplicate = cand
                break
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


async def _notify_daily_cap_once():
    """Schickt beim ERSTEN Zuschlagen des Tages-Limits genau eine Telegram-Notiz -
    sonst saehe ein Tag mit ausgeschoepftem Kostendeckel fuer den Nutzer exakt so aus
    wie ein ruhiger Nachrichtentag, obwohl der Monitor in Wahrheit stummgeschaltet
    ist. try_claim_meta_key ist atomar und ueberlebt einzelne GitHub-Actions-Laeufe,
    daher hoechstens eine Notiz pro UTC-Tag (schlaegt der Telegram-Versand selbst
    fehl, wird bewusst nicht erneut versucht - lieber eine verpasste Notiz als eine
    Wiederholungs-Schleife)."""
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if try_claim_meta_key(f"cap_notice_{today}"):
        if PRIORITY_CLASSIFICATIONS_PER_DAY > 0:
            body = (
                f"⏸️ Normales Tages-Limit von {MAX_CLASSIFICATIONS_PER_DAY} Claude-Analysen "
                "erreicht. Bis Mitternacht (UTC) werden nur noch als besonders wichtig "
                "eingestufte Meldungen analysiert (direkte Original-Quellen-Posts sowie harte "
                "Wirtschaftsthemen wie Zoelle, Sanktionen, Zinsen) - bis zu einer Reserve "
                f"von insgesamt {MAX_CLASSIFICATIONS_PER_DAY + PRIORITY_CLASSIFICATIONS_PER_DAY} "
                "Analysen/Tag. So bleiben die Kosten gedeckelt. Limits anpassbar ueber "
                "MAX_CLASSIFICATIONS_PER_DAY / PRIORITY_CLASSIFICATIONS_PER_DAY "
                "(.env bzw. GitHub-Variable)."
            )
        else:
            body = (
                f"⏸️ Tages-Limit von {MAX_CLASSIFICATIONS_PER_DAY} Claude-Analysen erreicht - "
                "neue Meldungen werden bis Mitternacht (UTC) uebersprungen, damit die "
                "API-Kosten gedeckelt bleiben. Limit anpassbar ueber "
                "MAX_CLASSIFICATIONS_PER_DAY (.env bzw. GitHub-Variable)."
            )
        await send_text(body)


async def _maybe_escalate(text, recent_context, priority, classification):
    """Zweitmeinung fuer Grenzfaelle (#4): liegt die staerkste Ticker-Konfidenz knapp
    um die Alarm-Schwelle (+/- ESCALATION_BAND), wird die Meldung EINMAL zusaetzlich
    mit einem staerkeren Modell klassifiziert und dessen Ergebnis uebernommen. Reduziert
    Fehlentscheidungen genau da, wo es aufs Geld geht, ohne jeden Call zu verteuern.
    Best-effort: Tages-Limit/transiente Fehler behalten die Erstbewertung; nur echte
    Konfigurationsfehler (falsches Eskalations-Modell = 404) schlagen wie ueberall durch."""
    if not ENABLE_BORDERLINE_ESCALATION or not CLAUDE_ESCALATION_MODEL:
        return classification
    if ALERT_MIN_TICKER_CONFIDENCE <= 0:
        # Bei deaktiviertem Praezisions-Filter ist "Grenzfall um die Schwelle" ein
        # sinnloser Begriff (die Schwelle ist 0) - ohne diesen Guard wuerde die
        # Bandpruefung unten zu "0 <= staerkste Konfidenz <= ESCALATION_BAND"
        # entarten, was auf JEDE Meldung OHNE handelbaren Ticker zutrifft (staerkste
        # Konfidenz ist dann 0.0) und faelschlich fast immer eine (kostenpflichtige)
        # Zweitmeinung ausloesen wuerde.
        return classification
    strongest = _strongest_ticker_confidence(classification)
    if not (ALERT_MIN_TICKER_CONFIDENCE - ESCALATION_BAND <= strongest
            <= ALERT_MIN_TICKER_CONFIDENCE + ESCALATION_BAND):
        return classification
    try:
        second = await classify(
            text, recent_context=recent_context,
            # Bewusst IMMER priority=False fuer den Eskalations-Call, unabhaengig vom
            # priority-Flag der urspruenglichen Meldung: die Zweitmeinung ist eine
            # Qualitaetsverbesserung fuer Grenzfaelle, kein Ersatz fuer die knappe
            # Prioritaets-Reserve. Sonst koennte EINE einzelne wichtige Grenzfall-
            # Meldung durch ihre eigene Eskalation zwei Reserve-Slots verbrauchen
            # (Primaer-Call + Eskalations-Call) statt nur einen - und die Reserve
            # damit fuer tatsaechlich NEUE wichtige Meldungen leerraeumen. Ist das
            # normale Tages-Limit bereits ausgeschoepft, faellt die Zweitmeinung
            # einfach aus (DailyCapExceeded unten); die bereits erfolgreich
            # klassifizierte Primaer-Bewertung bleibt in jedem Fall erhalten.
            priority=False,
            model=CLAUDE_ESCALATION_MODEL,
        )
    except DailyCapExceeded:
        logger.info("[borderline] Zweitmeinung wegen Tages-Limit uebersprungen.")
        return classification
    except PERMANENT_CLAUDE_ERRORS:
        logger.critical(
            "[borderline] Permanenter Fehler beim Eskalations-Modell "
            "(CLAUDE_ESCALATION_MODEL=%s) - nicht ANTHROPIC_API_KEY/CLAUDE_MODEL, "
            "sondern diese Eskalations-Konfiguration pruefen.",
            CLAUDE_ESCALATION_MODEL,
        )
        raise
    except Exception:
        logger.warning("[borderline] Zweitmeinung fehlgeschlagen, behalte Erstbewertung.", exc_info=True)
        return classification
    if second is FALLBACK_CLASSIFICATION:
        # classify() kann OHNE Exception einen Fallback liefern, wenn die Antwort des
        # Eskalations-Modells nicht sauber geparst werden konnte (siehe classifier.py:
        # _parse_response) - das ist KEINE bessere Zweitmeinung, sondern ein
        # Nicht-Ergebnis. Ohne diese Pruefung wuerde eine bereits erfolgreich
        # klassifizierte, tatsaechlich alarmwuerdige Meldung stillschweigend zu
        # "nicht marktrelevant" herabgestuft.
        logger.warning(
            "[borderline] Zweitmeinung lieferte keine gueltige Antwort, behalte Erstbewertung."
        )
        return classification
    logger.info(
        "[borderline] Zweitmeinung mit %s: staerkste Ticker-Konfidenz %.2f -> %.2f",
        CLAUDE_ESCALATION_MODEL, strongest, _strongest_ticker_confidence(second),
    )
    return second


async def _classify_and_store(raw, semaphore: asyncio.Semaphore, recent_context: list[dict]):
    priority = is_high_priority(raw)
    async with semaphore:
        try:
            classification = await classify(
                raw.text, recent_context=recent_context, priority=priority
            )
            classification = await _maybe_escalate(
                raw.text, recent_context, priority, classification
            )
        except DailyCapExceeded as exc:
            # Kein logger.exception() (kein Traceback-Spam): sobald das Tages-Limit
            # erreicht ist, trifft das jedes weitere Statement in diesem und allen
            # folgenden Zyklen bis Mitternacht UTC - eine kurze Warnung pro
            # uebersprungenem Statement reicht.
            logger.warning("[%s] %s :: %s", raw.source, exc, raw.text[:80])
            await _notify_daily_cap_once()
            return None
        except PERMANENT_CLAUDE_ERRORS:
            # Bewusst NICHT schlucken: ein kaputter API-Key oder ein geloeschtes
            # Modell trifft jeden weiteren Call genauso - wuerde das hier wie ein
            # normaler Einzelfehler behandelt, bliebe der GitHub-Actions-Lauf ewig
            # gruen, obwohl der Monitor faktisch tot ist (siehe poll_once, das den
            # Fehler gesammelt weiterwirft, und run_once.py, wo er den Lauf mit
            # Exitcode != 0 beendet -> GitHub verschickt eine Fehler-Mail).
            raise
        except Exception:
            logger.exception("Klassifikation fehlgeschlagen fuer: %s", raw.text[:80])
            return None

    if classification.related_topic_id is not None:
        # Claude kann sich die ID ausdenken/verwechseln - nur vertrauen, wenn sie
        # tatsaechlich Teil des angebotenen Kontexts war (sonst haette man ein
        # dangling duplicate_of_id auf eine falsche/nicht existierende Zeile).
        valid_ids = {item["id"] for item in recent_context}
        if classification.related_topic_id not in valid_ids:
            logger.warning(
                "[%s] Claude gab related_topic_id=%s zurueck, das nicht im "
                "angebotenen Themen-Kontext war - ignoriere die Zuordnung.",
                raw.source,
                classification.related_topic_id,
            )
            classification.related_topic_id = None
            classification.is_major_escalation = False

    if classification.related_topic_id is not None and not classification.is_major_escalation:
        # Gleiches Thema wie eine heute schon alarmierte Meldung, aber keine wesentliche
        # Verschaerfung - nicht erneut alarmieren (nur als Duplikat vermerken).
        dup_id = insert_statement(raw, None, duplicate_of_id=classification.related_topic_id)
        logger.info(
            "[%s] Themen-Duplikat von Statement #%d (keine wesentliche Eskalation), "
            "kein erneuter Alert: %s",
            raw.source,
            classification.related_topic_id,
            raw.text[:80],
        )
        # (raw, None, dup_id) statt bare None: dup_id muss in poll_once ueber
        # id_by_source_id auffindbar bleiben, sonst wuerden Batch-Geschwister, die
        # als Tier-1-Textduplikat GENAU DIESES raw erkannt wurden (duplicate_pairs),
        # ihre primary_id nicht finden und komplett verworfen werden (nicht mal als
        # eigene Duplikat-Zeile) - siehe alert_worthy-Filter in poll_once, der
        # classification=None hier korrekt als "nicht alarmwuerdig" behandelt.
        return (raw, None, dup_id)

    statement_id = insert_statement(raw, classification)
    if statement_id is None:
        logger.warning(
            "[%s] Statement konnte nicht gespeichert werden (source_id-Konflikt "
            "ohne auffindbare existierende Zeile?), ueberspringe: %s",
            raw.source,
            raw.text[:80],
        )
        return None

    escalation_note = (
        f" (Eskalation von #{classification.related_topic_id})"
        if classification.related_topic_id is not None and classification.is_major_escalation
        else ""
    )
    logger.info(
        "[%s] relevant=%s sentiment=%s conf=%.2f ticker=%s%s :: %s",
        raw.source,
        classification.is_market_relevant,
        classification.sentiment,
        classification.confidence,
        [tc["ticker"] for tc in classification.ticker_calls],
        escalation_note,
        raw.text[:100],
    )

    if is_alert_worthy(classification):
        # Sofort sichtbar fuer noch laufende Geschwister-Klassifikationen in dieser
        # Charge (recent_context ist eine geteilte, mutable Liste - siehe poll_once):
        # mindert (loest aber nicht vollstaendig fuer die ersten gleichzeitig
        # gestarteten Aufrufe) das Risiko, dieselbe Story zweimal zu alarmieren,
        # wenn sie unterschiedlich formuliert ist und Tier 1 (Textvergleich) das
        # nicht faengt.
        recent_context.append(
            {
                "id": statement_id,
                "text": raw.text[:CONTEXT_SNIPPET_MAX_CHARS],
                "sentiment": classification.sentiment,
            }
        )

    return (raw, classification, statement_id)


async def _fetch_ticker_context(classification, statement_id) -> dict:
    """Best-effort (#2/#5/#8): fuer die handelbaren Ticker den aktuellen Kurs holen, ihn
    als Ausgangspunkt fuers Backtesting speichern und {'change': {...}, 'risk': {...}}
    zurueckgeben - die heutige Bewegung (Open->jetzt) und, falls ENABLE_RISK_LEVELS,
    vorgeschlagene Stop-/Ziel-Marken (#5) je Ticker fuer den Alert. Nur wenn
    ENABLE_PRICE_TRACKING gesetzt ist. Jede Kursabfrage ist best-effort (prices.get_quote
    gibt bei Problemen None) - ein nicht erreichbarer Kursdienst darf den Alert NIE
    aufhalten."""
    result: dict[str, dict] = {"change": {}, "risk": {}}
    if not ENABLE_PRICE_TRACKING or statement_id is None:
        return result
    now = time.time()
    for tc in actionable_tickers(classification):
        ticker = (tc.get("ticker") or "").upper()
        quote = await prices.get_quote(ticker)
        if not quote or quote.get("price") is None:
            continue
        record_alert_baseline(
            statement_id, ticker, tc.get("direction"), tc.get("confidence"),
            now, quote.get("price"),
        )
        if quote.get("change_pct") is not None:
            result["change"][ticker] = quote["change_pct"]
        if ENABLE_RISK_LEVELS:
            levels = prices.suggest_risk_levels(
                quote.get("price"), tc.get("direction"), quote.get("high"), quote.get("low")
            )
            if levels:
                result["risk"][ticker] = levels
    return result


async def _ticker_technical(ticker: str, direction: str | None) -> dict | None:
    """Best-effort: historische Tageskurse holen, das TradingView-artige Indikator-Panel
    berechnen und um die Uebereinstimmung mit der eingeschaetzten Richtung ergaenzen.
    None, wenn keine/zu wenige Kursdaten vorliegen (Feature entfaellt dann still)."""
    hist = await prices.get_history(ticker)
    if not hist:
        return None
    summary = indicators.technical_summary(
        hist.get("high"), hist.get("low"), hist.get("close"), hist.get("volume")
    )
    if not summary:
        return None
    summary["agrees"] = indicators.agreement(summary.get("label"), direction)
    summary["contradicts_strongly"] = indicators.strongly_contradicts(
        summary.get("label"), direction
    )
    return summary


async def _fetch_technicals(classification) -> dict:
    """Technik-Bewertung fuer ALLE handelbaren Ticker einer Meldung (fuer die Anzeige im
    Alert). {TICKER: summary}. Kursabfragen gleichzeitig statt nacheinander."""
    actionable = actionable_tickers(classification)
    if not actionable:
        return {}
    tickers = [(tc.get("ticker") or "").upper() for tc in actionable]
    directions = [tc.get("direction") for tc in actionable]
    results = await asyncio.gather(
        *(_ticker_technical(t, d) for t, d in zip(tickers, directions))
    )
    return {t: r for t, r in zip(tickers, results) if r is not None}


def _apply_technical_conviction(score: int, summary: dict | None) -> int:
    """Hebt/senkt den Ueberzeugungs-Score, je nachdem ob die Technik das Signal
    bestaetigt (agrees True) oder ihm widerspricht (agrees False). Gewicht ueber
    TECHNICALS_CONVICTION_WEIGHT; 0 = Score unveraendert. Ergebnis bleibt in [0,100]."""
    if not summary or TECHNICALS_CONVICTION_WEIGHT <= 0:
        return score
    agrees = summary.get("agrees")
    if agrees is True:
        return min(100, score + TECHNICALS_CONVICTION_WEIGHT)
    if agrees is False:
        return max(0, score - TECHNICALS_CONVICTION_WEIGHT)
    return score


def _freshness_minutes(published_at) -> float | None:
    """Alter einer Meldung in Minuten aus der echten Veroeffentlichungszeit (Epoch) -
    oder None, wenn kein brauchbarer Zeitstempel vorliegt. Negatives (leichte Uhr-
    Abweichung) wird auf 0 geklemmt."""
    if not isinstance(published_at, (int, float)) or published_at <= 0:
        return None
    return max(0.0, (time.time() - published_at) / 60.0)


def _compute_conviction(raw, classification, statement_id) -> tuple[int, int, bool]:
    """Berechnet (billig, ohne Kurs-/Claude-Call) den Ueberzeugungs-Score (#1) sowie die
    Korroborations-Quellenzahl (#15) und ob der Text spekulativ formuliert ist (#2).
    Rueckgabe: (score, corroboration, hedged). Wird sowohl fuer die Zustell-Gates
    (Ruhezeiten) als auch fuer die Alert-Anreicherung verwendet, damit beide denselben
    Wert sehen und er nur einmal berechnet wird."""
    corroboration = get_corroboration_count(statement_id) if statement_id else 1
    hedged = has_hedge_language(raw.text)
    high_vol = high_volatility_text(raw.text)
    strongest = _strongest_ticker_confidence(classification)
    score = conviction_score(
        classification.confidence, strongest,
        freshness_minutes=_freshness_minutes(raw.published_at),
        corroboration_sources=corroboration, hedged=hedged, high_volatility=high_vol,
    )
    return score, corroboration, hedged


async def _build_alert_extras(raw, classification, statement_id, score, corroboration, hedged) -> dict:
    """Sammelt die Zusatzinfos fuer einen Einzel-Alert: Ueberzeugungs-Score + Positions-
    einordnung (#1/#4), Korroboration (#15), Hedge-Hinweis (#2), heutige Bewegung +
    Stop/Ziel je Ticker (#8/#5, inkl. Baseline-Erfassung fuers Backtesting #2),
    historische Trefferquote je Ticker (#9) und - bei einer Eskalation - die Themen-
    Zeitleiste (#5)."""
    extras: dict = {}
    if ENABLE_CONVICTION_SCORE:
        extras["conviction"] = score
        extras["position_tier"] = position_tier(score)
    if corroboration > 1:
        extras["corroboration"] = corroboration
    if hedged:
        extras["hedged"] = True

    price_ctx = await _fetch_ticker_context(classification, statement_id)
    if price_ctx.get("change"):
        extras["ticker_change"] = price_ctx["change"]
    if price_ctx.get("risk"):
        extras["ticker_risk"] = price_ctx["risk"]

    actionable = actionable_tickers(classification)

    if ENABLE_HISTORICAL_HITRATE:
        hitrates = {}
        for tc in actionable:
            ticker = (tc.get("ticker") or "").upper()
            hr = get_ticker_hitrate(ticker)
            if hr:
                hitrates[ticker] = hr
        if hitrates:
            extras["ticker_hitrate"] = hitrates

    # Richtungswechsel (#2): weicht die Richtung des staerksten Tickers von der zuletzt
    # fuer ihn alarmierten Richtung ab (innerhalb 48h)?
    if actionable:
        top = max(actionable, key=lambda tc: tc.get("confidence") or 0.0)
        ticker = (top.get("ticker") or "").upper()
        prev = get_last_alert_direction(ticker, time.time() - 48 * 3600)
        if prev and top.get("direction") in ("long", "short") and prev != top.get("direction"):
            extras["direction_flip"] = prev

    # Sektor-Cluster (#4): mehrere Werte derselben Branche zuletzt alarmiert.
    if SECTOR_CLUSTER_MIN > 1 and classification is not None and classification.sectors:
        counts = get_recent_alerted_sector_counts(SECTOR_CLUSTER_WINDOW_HOURS)
        best = None
        for sec in classification.sectors:
            if not isinstance(sec, str):
                continue
            total = counts.get(sec.strip(), 0) + 1  # + diese Meldung selbst
            if total >= SECTOR_CLUSTER_MIN and (best is None or total > best["count"]):
                best = {"sector": sec.strip(), "count": total}
        if best:
            extras["sector_cluster"] = best

    # Kurs-Divergenz (#3): laeuft der Kurs heute bereits gegen die These?
    if DIVERGENCE_WARN_PCT > 0 and price_ctx.get("change"):
        against = []
        for tc in actionable:
            ticker = (tc.get("ticker") or "").upper()
            change = price_ctx["change"].get(ticker)
            if not isinstance(change, (int, float)):
                continue
            direction = tc.get("direction")
            if (direction == "long" and change <= -DIVERGENCE_WARN_PCT) or (
                direction == "short" and change >= DIVERGENCE_WARN_PCT
            ):
                against.append(f"{ticker} heute {change:+.1f}%")
        if against:
            extras["divergence"] = ", ".join(against)

    # "Zu spaet"-Warnung: laeuft der Kurs heute bereits stark MIT der These, ist die
    # Bewegung moeglicherweise schon groesstenteils gelaufen ("er predigt erst, wenn es
    # schon hardcore im Geschehen ist"). Betrag der bereits gelaufenen, richtungs-
    # konformen Tagesbewegung des staerksten handelbaren Tickers.
    if LATE_MOVE_WARN_PCT > 0 and price_ctx.get("change") and actionable:
        top = max(actionable, key=lambda tc: tc.get("confidence") or 0.0)
        ticker = (top.get("ticker") or "").upper()
        change = price_ctx["change"].get(ticker)
        direction = top.get("direction")
        if isinstance(change, (int, float)):
            with_thesis = change if direction == "long" else -change if direction == "short" else 0.0
            if with_thesis >= LATE_MOVE_WARN_PCT:
                extras["late_move"] = with_thesis

    # Uebernacht-/Vorboersen-Gap-Antizipation: klar gerichteter Katalysator in einem
    # Fenster, in dem der Markt ihn nicht mehr voll einpreisen kann (nachboerslich, ueber
    # Nacht, uebers Wochenende, kurz vor Schluss) -> Hinweis auf ein wahrscheinliches Gap
    # am naechsten Open, damit man einsteigen kann, BEVOR es vorboerslich hochschiesst.
    if ENABLE_GAP_PREDICTION and actionable:
        top = max(actionable, key=lambda tc: tc.get("confidence") or 0.0)
        gap = predict_gap(
            top.get("direction"),
            us_market_session(),
            minutes_until_close(),
            expected_move_pct=(classification.expected_move_pct if classification else None),
            near_close_minutes=GAP_NEAR_CLOSE_MINUTES,
            min_expected_move_pct=GAP_MIN_EXPECTED_MOVE_PCT,
        )
        if gap:
            gap = {**gap, "ticker": (top.get("ticker") or "").upper()}
            extras["overnight_gap"] = gap

    # Kelly-lite Positionsanteil (#5): global aus der bisherigen Trefferquote/Gewinn/
    # Verlust. Erst ab genuegend ausgewerteten Ergebnissen, damit die Zahl nicht auf
    # zwei Zufallstreffern beruht.
    if ENABLE_KELLY_SUGGESTION:
        ki = get_kelly_inputs()
        if (ki.get("n") or 0) >= 10:
            f = kelly_fraction(ki["hit_rate"], ki["avg_win_pct"], ki["avg_loss_pct"])
            if f and f > 0:
                extras["kelly_fraction"] = f

    # Technische Gesamtbewertung (TradingView-Stil) je handelbarem Ticker fuer die Anzeige
    # (die Score-Wirkung/das Gate greifen bereits vorher in _send_alerts). Die Historie
    # ist gecacht, der staerkste Ticker ist hier daher meist ein Cache-Treffer.
    if ENABLE_TECHNICALS and actionable:
        tech = await _fetch_technicals(classification)
        if tech:
            extras["technical"] = tech

    if classification is not None and classification.related_topic_id is not None:
        thread = get_topic_thread(classification.related_topic_id)
        if len(thread) > 1:
            extras["thread"] = thread
    return extras


def _record_ticker_alerts(classification, statement_id) -> None:
    """Protokolliert alle handelbaren Ticker+Richtungen eines gerade verschickten Alerts
    (fuer den Cooldown, #6)."""
    now = time.time()
    for tc in actionable_tickers(classification):
        record_ticker_alert(statement_id, (tc.get("ticker") or "").upper(), tc.get("direction"), now)


def _passes_cooldown(classification) -> bool:
    """Ticker-Cooldown (#6): True, wenn MINDESTENS ein handelbarer Ticker+Richtung NICHT
    innerhalb des Cooldown-Fensters bereits alarmiert wurde (es gibt also etwas Neues zu
    melden). Nur wenn alle handelbaren Ticker noch im Cooldown sind, wird unterdrueckt.
    Cooldown = 0 -> immer True (aus)."""
    if TICKER_ALERT_COOLDOWN_MINUTES <= 0:
        return True
    actionable = actionable_tickers(classification)
    if not actionable:
        return True
    since = time.time() - TICKER_ALERT_COOLDOWN_MINUTES * 60
    return any(
        not ticker_in_cooldown((tc.get("ticker") or "").upper(), tc.get("direction"), since)
        for tc in actionable
    )


def _in_quiet_hours_now() -> bool:
    """True, wenn gerade Ruhezeit ist (#7) - Stunde im konfigurierten Fenster, in der
    lokalen Zeit QUIET_HOURS_TZ. Fehlt die Zeitzone (tzdata), wird auf UTC ausgewichen."""
    window = parse_hour_window(QUIET_HOURS)
    if window is None:
        return False
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(QUIET_HOURS_TZ)
        except Exception:
            tz = None
    now = datetime.datetime.now(tz) if tz else datetime.datetime.now(datetime.timezone.utc)
    return hour_in_window(now.hour, window)


def _passes_quiet_hours(score: int) -> bool:
    """Ruhezeiten-Gate (#7): ausserhalb der Ruhezeit immer True. Innerhalb nur, wenn der
    Ueberzeugungs-Score hoch genug ist (>= QUIET_HOURS_MIN_CONVICTION); schwaechere Alerts
    warten - der Resend-Pfad stellt sie nach Fensterende automatisch zu."""
    if not _in_quiet_hours_now():
        return True
    return score >= QUIET_HOURS_MIN_CONVICTION


async def _send_alerts(alert_worthy: list[tuple]):
    """Schickt einzelne Alerts bei wenigen Treffern, sonst eine gebuendelte
    Sammel-Nachricht - verhindert eine Alert-Flut bei einem Nachrichtenschub
    (z.B. wenn ploetzlich viele echte, unterschiedliche Meldungen gleichzeitig
    marktrelevant sind). Vor dem Versand greifen die Zustell-Gates Cooldown (#6) und
    Ruhezeiten (#7); unterdrueckte Meldungen bleiben unmarkiert und werden vom
    Resend-Pfad spaeter erneut versucht (z.B. nach Ende der Ruhezeit)."""
    if not alert_worthy:
        return

    # Ueberzeugung + Gate-Pruefung (alles billig, ohne Kurs-/Claude-Call), damit die
    # teuren Kursabfragen nur fuer tatsaechlich zuzustellende Alerts anfallen.
    gated: list[tuple] = []
    for raw, classification, statement_id in alert_worthy:
        score, corroboration, hedged = _compute_conviction(raw, classification, statement_id)

        # Technische Zweitmeinung (TradingView-Stil): fuer den staerksten handelbaren
        # Ticker die Gesamtbewertung holen. Sie hebt/senkt den Ueberzeugungs-Score (so
        # wirkt sie auf ALLE nachgelagerten Gates UND das Paper-Sizing) und kann - falls
        # TECHNICALS_REQUIRE_AGREEMENT aktiv - einen klar widersprechenden Alert
        # unterdruecken. Best-effort; ohne erreichbare Kurshistorie passiert nichts.
        if ENABLE_TECHNICALS:
            actionable = actionable_tickers(classification)
            if actionable:
                top = max(actionable, key=lambda tc: tc.get("confidence") or 0.0)
                summary = await _ticker_technical(
                    (top.get("ticker") or "").upper(), top.get("direction")
                )
                if summary is not None:
                    if TECHNICALS_REQUIRE_AGREEMENT and summary.get("contradicts_strongly"):
                        logger.info(
                            "[%s] Alert unterdrueckt (Technik widerspricht klar: %s vs %s): %s",
                            raw.source, summary.get("label"), top.get("direction"),
                            raw.text[:80],
                        )
                        continue
                    score = _apply_technical_conviction(score, summary)

        if ALERT_MIN_CONVICTION > 0 and score < ALERT_MIN_CONVICTION:
            logger.info(
                "[%s] Alert unter globaler Ueberzeugungs-Schwelle (%d < %d) - "
                "zurueckgestellt: %s",
                raw.source, score, ALERT_MIN_CONVICTION, raw.text[:80],
            )
            continue
        if not _passes_cooldown(classification):
            logger.info(
                "[%s] Alert unterdrueckt (Ticker-Cooldown aktiv): %s",
                raw.source, raw.text[:80],
            )
            continue
        if not _passes_quiet_hours(score):
            logger.info(
                "[%s] Alert waehrend Ruhezeit zurueckgestellt (Ueberzeugung %d < %d): %s",
                raw.source, score, QUIET_HOURS_MIN_CONVICTION, raw.text[:80],
            )
            continue
        gated.append((raw, classification, statement_id, score, corroboration, hedged))

    if not gated:
        return

    # Anti-Fatigue-Ratelimit (#6): hoechstens MAX_ALERTS_PER_HOUR Meldungen je rollierender
    # Stunde. Ueberzaehlige (nach Ueberzeugung schwaechere) werden zurueckgestellt und vom
    # Resend-Pfad spaeter erneut versucht. 0 = aus.
    if MAX_ALERTS_PER_HOUR > 0:
        already = count_alerted_statements_since(time.time() - 3600)
        allowance = MAX_ALERTS_PER_HOUR - already
        if allowance <= 0:
            logger.info(
                "Alert-Ratelimit erreicht (%d/Std bereits verschickt) - %d Meldung(en) "
                "zurueckgestellt, werden spaeter erneut versucht.",
                already, len(gated),
            )
            return
        if len(gated) > allowance:
            gated.sort(key=lambda t: t[3], reverse=True)  # staerkste Ueberzeugung zuerst
            deferred = len(gated) - allowance
            gated = gated[:allowance]
            logger.info(
                "Alert-Ratelimit: nur die %d ueberzeugendsten Meldung(en) jetzt, "
                "%d zurueckgestellt (Resend spaeter).",
                allowance, deferred,
            )

    if len(gated) <= ALERT_DIGEST_THRESHOLD:
        for raw, classification, statement_id, score, corroboration, hedged in gated:
            extras = await _build_alert_extras(
                raw, classification, statement_id, score, corroboration, hedged
            )
            sent = await send_alert(raw, classification, extras=extras)
            if sent:
                mark_alert_sent(statement_id)
                _record_ticker_alerts(classification, statement_id)
                # Paper-Trading: virtuelle Position(en) fuer die handelbaren Ticker
                # eroeffnen (Sizing aus dem Ueberzeugungs-Score). Best-effort, nach dem
                # erfolgreichen Alert - ein Fehler hier darf den Alert nicht ruinieren.
                if PAPER_TRADING:
                    try:
                        await paper_trading.open_positions_for_alert(
                            classification, statement_id, score
                        )
                    except Exception:
                        logger.warning("[paper] Position eroeffnen fehlgeschlagen.", exc_info=True)
    else:
        # Sammel-Nachricht: die Baselines fuers Backtesting trotzdem erfassen (damit ein
        # Nachrichtenschub keine Luecke in der Erfolgsmessung reisst), aber ohne die
        # reichhaltigen Einzel-Extras (Thread/Buttons) - der Digest bleibt kompakt.
        # Gleichzeitig statt nacheinander abgefragt: ein sequenzieller Kurs-Abruf pro
        # Alert wuerde ausgerechnet in dem Nachrichtenschub-Fall, fuer den der Digest
        # ueberhaupt existiert, den Poll-Zyklus spuerbar verzoegern koennen.
        await asyncio.gather(*(
            _fetch_ticker_context(classification, statement_id)
            for _, classification, statement_id, _, _, _ in gated
        ))
        digest_items = [(raw, c, sid) for raw, c, sid, _, _, _ in gated]
        sent = await send_digest_alert(digest_items)
        if sent:
            for raw, classification, statement_id, score, _, _ in gated:
                mark_alert_sent(statement_id)
                _record_ticker_alerts(classification, statement_id)
                if PAPER_TRADING:
                    try:
                        await paper_trading.open_positions_for_alert(
                            classification, statement_id, score
                        )
                    except Exception:
                        logger.warning("[paper] Position eroeffnen fehlgeschlagen.", exc_info=True)


def _row_to_alert_tuple(row: dict) -> tuple:
    # published_at mitnehmen, damit ein spaeter nachgeschickter Alert (Resend-Pfad) das
    # echte Alter der Meldung anzeigen kann statt gar keins.
    raw = RawStatement(
        source=row["source"], source_id=row["source_id"], text=row["text"],
        url=row["url"], published_at=row.get("published_at"),
    )
    classification = Classification(
        is_market_relevant=bool(row["is_market_relevant"]),
        sentiment=row["sentiment"],
        confidence=row["confidence"] or 0.0,
        ticker_calls=row["tickers"],
        sectors=row["sectors"],
        reasoning=row["reasoning"] or "",
        related_topic_id=row.get("related_topic_id"),
        is_major_escalation=bool(row.get("is_major_escalation")),
        expected_move_pct=row.get("expected_move_pct"),
        expected_horizon=row.get("expected_horizon"),
    )
    return (raw, classification, row["id"])


async def _resend_pending_alerts():
    """Versucht Alerts erneut, die als marktrelevant eingestuft aber nie tatsaechlich
    verschickt wurden (z.B. weil ein vorheriger Lauf mitten drin abgebrochen wurde,
    oder Telegram beim ersten Versuch nicht erreichbar war). Ohne das wuerden solche
    Statements fuer immer stumm bleiben, da is_known() sie ab dem ersten Insert als
    'schon gesehen' behandelt."""
    pending = get_pending_alerts()
    if not pending:
        return
    # Denselben strengen Alarm-Filter wie im Primaerpfad anwenden: get_pending_alerts
    # liefert JEDE marktrelevante, noch nicht alarmierte Meldung - ohne diesen Filter
    # wuerde eine Meldung, die die strenge Ticker-Schwelle nicht erreicht, hier einen
    # Zyklus spaeter doch noch alarmiert (die Schwelle waere faktisch wirkungslos).
    # Nicht-alarmwuerdige Eintraege bleiben unmarkiert und altern nach 24h aus dem
    # get_pending_alerts-Fenster heraus (kein erneuter Claude-Call, nur ein Filter).
    worthy = [t for t in (_row_to_alert_tuple(row) for row in pending) if is_alert_worthy(t[1])]
    if not worthy:
        return
    logger.info(
        "%d handelbare(s) Statement(s) ohne erfolgreichen Alert aus vorherigem(n) "
        "Lauf/Laeufen gefunden, versuche erneut.",
        len(worthy),
    )
    await _send_alerts(worthy)


async def _evaluate_alert_outcomes():
    """Backtesting (#2/#3): fuer alarmierte Ticker, deren Horizont abgelaufen ist, den
    aktuellen Kurs nachmessen und die tatsaechliche Bewegung + ob sie zur Long/Short-
    Einschaetzung passte festhalten. Best-effort; nur wenn ENABLE_PRICE_TRACKING."""
    if not ENABLE_PRICE_TRACKING:
        return
    pending = get_outcomes_awaiting_followup(PRICE_OUTCOME_HORIZON_MINUTES * 60)
    if not pending:
        return
    now = time.time()
    # Alle Kursabfragen gleichzeitig statt nacheinander (bis zu 50 Ticker, siehe
    # get_outcomes_awaiting_followup-Limit): sequenziell koennte das bei einem
    # traegen Kursdienst mehrere Minuten dauern und den gesamten Poll-Zyklus unnoetig
    # verzoegern. Die eigentlichen DB-Schreibzugriffe bleiben synchron/sequenziell
    # danach (kein Nebenlaeufigkeits-Risiko fuer SQLite).
    quotes = await asyncio.gather(*(prices.get_quote(o["ticker"]) for o in pending))
    for o, quote in zip(pending, quotes):
        if not quote or not quote.get("price"):
            continue
        alert_price = o["alert_price"]
        if not alert_price:
            continue
        followup_price = quote["price"]
        return_pct = (followup_price - alert_price) / alert_price * 100.0
        # "correct", wenn sich der Kurs in die eingeschaetzte Richtung bewegt hat.
        correct = (return_pct > 0 and o["direction"] == "long") or (
            return_pct < 0 and o["direction"] == "short"
        )
        set_outcome_followup(o["id"], now, followup_price, return_pct, correct)


async def _manage_paper_positions():
    """Paper-Trading (virtuelles Depot): offene Positionen zum aktuellen Kurs bewerten,
    bei Stop-Loss/Take-Profit automatisch schliessen (mit Sofort-Meldung) und - gedrosselt
    auf PAPER_STATUS_INTERVAL_MINUTES - einen Depot-Status schicken ("auf wie viel steht
    alles"). Best-effort; ein Fehler darf den Poll-Zyklus nicht abbrechen."""
    if not PAPER_TRADING:
        return
    try:
        await paper_trading.manage_open_positions()
    except Exception:
        logger.warning("[paper] Positionsverwaltung fehlgeschlagen.", exc_info=True)


async def _maybe_send_weekly_digest():
    """Woechentlicher Performance-Digest (#10): einmal pro Kalenderwoche (am
    WEEKLY_DIGEST_WEEKDAY, ab WEEKLY_DIGEST_MIN_HOUR UTC) eine Telegram-Zusammenfassung
    der ausgewerteten Alerts. try_claim_meta_key (Jahr+Woche im Key) sorgt fuer genau
    EINEN Versand pro Woche, auch ueber einzelne GitHub-Actions-Laeufe hinweg. Wird nur
    verschickt, wenn es ueberhaupt ausgewertete Ergebnisse gibt (sonst waere der Digest
    leer - relevant nur mit ENABLE_PRICE_TRACKING)."""
    if not ENABLE_WEEKLY_DIGEST:
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.weekday() != WEEKLY_DIGEST_WEEKDAY or now.hour < WEEKLY_DIGEST_MIN_HOUR:
        return
    iso_year, iso_week, _ = now.isocalendar()
    # Zuerst pruefen, ob es ueberhaupt etwas zu berichten gibt - BEVOR der Wochen-Slot
    # beansprucht wird, damit ein Versand nicht verpufft, solange noch keine Ergebnisse
    # vorliegen (z.B. Preis-Tracking gerade erst aktiviert).
    stats = get_performance_stats()
    if not stats.get("evaluated"):
        return
    if not try_claim_meta_key(f"weekly_digest_{iso_year}_W{iso_week:02d}"):
        return
    await send_weekly_digest(stats)


async def poll_once(sources, semaphore: asyncio.Semaphore):
    await _resend_pending_alerts()
    await _evaluate_alert_outcomes()
    await _manage_paper_positions()
    await _maybe_send_weekly_digest()

    # Wird gesetzt, sobald ein permanenter Claude-Konfigurationsfehler (kaputter Key,
    # geloeschtes Modell) auftaucht - erst NACH Abschluss der kompletten Buchhaltung
    # (Duplikate speichern, erfolgreiche Alerts senden) weitergeworfen, damit keine
    # bereits gewonnenen Daten dieses Zyklus verloren gehen.
    permanent_error: Exception | None = None

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

        # Erste Trichter-Stufe: billiger, Claude-FREIER Vorfilter VOR der Dedup/
        # Klassifikation. Verwirft offensichtlich nicht marktbewegende Schlagzeilen
        # (Listicles, Ratgeber, Personal-Finance-Clickbait), damit fuer sie kein
        # Claude-Call anfaellt und kein Slot des Tages-Kostendeckels verbraucht wird -
        # relevant, seit die Quellen (nach dem Wegfall des Personen-/Themenfilters)
        # deutlich mehr Rohmaterial liefern. Konservativ (siehe app/prefilter.py):
        # entscheidet NICHT ueber Alerts, sondern nur ueber "eine Claude-Analyse wert".
        if ENABLE_PREFILTER:
            kept = [r for r in raw_statements if looks_market_relevant(r.text)]
            dropped = len(raw_statements) - len(kept)
            if dropped:
                health["prefiltered"] = health.get("prefiltered", 0) + dropped
                logger.info(
                    "[%s] Vorfilter: %d/%d Meldung(en) als offensichtlich nicht "
                    "marktbewegend verworfen (kein Claude-Call).",
                    source.name, dropped, len(raw_statements),
                )
            raw_statements = kept
            if not raw_statements:
                continue

        # Stale-News-Filter (#14): Meldungen, deren echte Veroeffentlichung laenger als
        # MAX_NEWS_AGE_MINUTES zurueckliegt, gar nicht erst klassifizieren - alte
        # Nachrichten sind meist eingepreist und kosten sonst nur einen Claude-Call.
        # Meldungen OHNE verlaesslichen Zeitstempel werden bewusst NICHT verworfen
        # (konservativ - lieber ein Call zu viel als ein echtes Ereignis verlieren).
        if MAX_NEWS_AGE_MINUTES > 0:
            max_age_seconds = MAX_NEWS_AGE_MINUTES * 60
            now_ts = time.time()
            fresh = [
                r for r in raw_statements
                if not (isinstance(r.published_at, (int, float)) and r.published_at > 0
                        and (now_ts - r.published_at) > max_age_seconds)
            ]
            dropped = len(raw_statements) - len(fresh)
            if dropped:
                health["stale"] = health.get("stale", 0) + dropped
                logger.info(
                    "[%s] Stale-Filter: %d/%d Meldung(en) aelter als %d Min - "
                    "verworfen (kein Claude-Call).",
                    source.name, dropped, len(raw_statements), MAX_NEWS_AGE_MINUTES,
                )
            raw_statements = fresh
            if not raw_statements:
                continue

        to_classify, duplicate_pairs = _partition_duplicates(raw_statements)

        # Wichtige Meldungen zuerst klassifizieren: an einem Tag mit ausgeschoepftem
        # Budget soll das (knappe) verbleibende Kontingent bevorzugt fuer die
        # wichtigsten Meldungen ausgegeben werden, statt es an fruehere, unwichtigere
        # Statements derselben Charge zu verlieren. Stabile Sortierung -> innerhalb
        # gleicher Prioritaet bleibt die urspruengliche Reihenfolge erhalten.
        to_classify.sort(key=is_high_priority, reverse=True)

        # Mutable statt statischer Snapshot (siehe _classify_and_store): waechst
        # waehrend der Verarbeitung dieser Charge, wenn zuvor gestartete Aufgaben
        # bereits fertig sind, bevor spaeter gestartete ihren Klassifikations-Call
        # tatsaechlich abschicken.
        recent_context = get_recent_alerted(
            hours=TOPIC_CONTEXT_WINDOW_HOURS, limit=TOPIC_CONTEXT_MAX_ITEMS
        )
        raw_results = await asyncio.gather(
            *(_classify_and_store(raw, semaphore, recent_context) for raw in to_classify),
            return_exceptions=True,
        )
        results = []
        for r in raw_results:
            if isinstance(r, PERMANENT_CLAUDE_ERRORS):
                if permanent_error is None:
                    permanent_error = r
                continue
            if isinstance(r, BaseException):
                logger.error("Unerwarteter Fehler bei der Klassifikation im Batch.", exc_info=r)
                continue
            if r is not None:
                results.append(r)
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

        alert_worthy = [r for r in results if is_alert_worthy(r[1])]
        await _send_alerts(alert_worthy)

    if permanent_error is not None:
        # Ein 401/403/404 der Claude-API repariert sich nicht von selbst - nach oben
        # durchreichen: run_once.py (GitHub-Actions-Modus) beendet den Lauf damit mit
        # Exitcode != 0 (roter Lauf + Fehler-Mail von GitHub) statt fuer immer gruen
        # zu bleiben, waehrend kein einziges Statement mehr klassifiziert wird. Im
        # Dauerbetrieb faengt _poll_loop den Fehler und loggt ihn pro Zyklus als
        # CRITICAL.
        logger.critical(
            "Permanenter Claude-Konfigurationsfehler - ANTHROPIC_API_KEY/CLAUDE_MODEL "
            "pruefen: %s",
            permanent_error,
        )
        raise permanent_error


async def _poll_loop():
    init_db()
    sources = build_sources()
    logger.info("Aktive Quellen: %s", [s.name for s in sources])
    # Uebersicht der aktiven optionalen Gates/Features (#12) - macht Fehlkonfiguration
    # ('warum kommen keine Alerts?') schon im Log sofort sichtbar.
    logger.info(
        "Aktive optionale Gates/Features: %s",
        {k: v for k, v in active_gates().items() if v},
    )

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
        _record_cycle_duration(elapsed)  # #11: Zyklus-Timing fuer /api/health
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
