import asyncio
import datetime
import logging
import re
import time

from anthropic import AuthenticationError, NotFoundError, PermissionDeniedError

from app.classifier import CONTEXT_SNIPPET_MAX_CHARS, DailyCapExceeded, classify
from app.config import (
    ALERT_CONFIDENCE_THRESHOLD,
    ALERT_DIGEST_THRESHOLD,
    ALERT_MIN_TICKER_CONFIDENCE,
    DEDUP_SIMILARITY_THRESHOLD,
    ENABLE_LIVE_AUDIO,
    ENABLE_NEWS,
    ENABLE_TRUTH_SOCIAL,
    LIVE_AUDIO_STREAM_URLS,
    MAX_CLASSIFICATIONS_PER_DAY,
    MAX_CONCURRENT_CLASSIFICATIONS,
    POLL_INTERVAL_SECONDS,
    PRIORITY_CLASSIFICATIONS_PER_DAY,
    TELEGRAM_STARTUP_NOTICE,
    TOPIC_CONTEXT_MAX_ITEMS,
    TOPIC_CONTEXT_WINDOW_HOURS,
    WHISPER_MODEL_SIZE,
)
from app.db import (
    Classification,
    RawStatement,
    get_dedup_candidates,
    get_known_source_ids,
    get_pending_alerts,
    get_recent_alerted,
    init_db,
    insert_statement,
    mark_alert_sent,
    try_claim_meta_key,
)
from app.sources.live_audio import LiveAudioSource
from app.sources.news_gdelt import GdeltNewsSource
from app.sources.news_rss import RssNewsSource
from app.sources.truth_social import TruthSocialSource
from app.telegram_alert import send_alert, send_digest_alert, send_startup_notice, send_text
from app.util import text_similarity

# Permanente Konfigurationsfehler der Claude-API: ein kaputter/widerrufener API-Key
# (401), fehlende Berechtigung (403) oder ein nicht (mehr) existierendes Modell (404)
# reparieren sich nicht von selbst - im Gegensatz zu transienten Fehlern (Timeouts,
# 429, 529), die das SDK selbst retried und die den Lauf nicht abbrechen sollen.
PERMANENT_CLAUDE_ERRORS = (AuthenticationError, PermissionDeniedError, NotFoundError)

logger = logging.getLogger(__name__)

# In-memory Health-Status pro Quelle, fuer /api/health. Muss nicht persistiert werden -
# nach einem Neustart baut sich der Status einfach durch die naechsten Polls neu auf.
source_health: dict[str, dict] = {}
run_health: dict = {"started_at": None, "loop_restarts": 0, "last_cycle_at": None}

# Wortgrenzen-Muster fuer die "besonders wichtig"-Einstufung (siehe is_high_priority).
# Bewusst STRENGER als der GDELT-Ingestion-Filter (der schon 'market'/'stock'/'trade'
# etc. abdeckt) - hier zaehlen nur die haertesten, unmittelbar marktbewegenden Themen,
# damit die knappe Prioritaets-Reserve nicht sofort von jeder markt-nahen Meldung
# aufgebraucht wird. Rein aus billigen Textsignalen bestimmt (KEIN Claude-Call), da die
# Wichtigkeit ueber das Tages-Limit entscheiden muss, BEVOR ein Call ausgegeben wird.
_HIGH_PRIORITY_PATTERN = re.compile(
    r"\b("
    r"tariff|tariffs|zoll|zoelle|zölle|sanction|sanctions|sanktion|"
    r"federal reserve|interest rate|rate cut|rate hike|zinsen|leitzins|"
    r"executive order|shutdown|default|embargo|nationaliz|verstaatlich|"
    r"bailout|stimulus|export ban|import ban|price cap"
    r")\b",
    re.IGNORECASE,
)


def is_high_priority(raw) -> bool:
    """Billige, Claude-freie Einschaetzung, ob eine Meldung wichtig genug ist, um die
    Prioritaets-Reserve oberhalb des normalen Tages-Limits nutzen zu duerfen. True bei
    (a) direkten Trump-Posts von Truth Social (seine eigenen Worte, am unmittelbarsten
    handlungsrelevant und ohnehin selten) ODER (b) einem der haertesten Wirtschafts-
    Signalwoerter im Text. Bewusst konservativ - lieber ein paar wichtige Meldungen
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


def is_alert_worthy(classification) -> bool:
    """Zentrale, EINZIGE Stelle, die entscheidet, ob eine Meldung eine Telegram-
    Nachricht ausloest. Es wird bewusst nur noch bei den sichersten, direkt
    handelbaren Signalen alarmiert: die Meldung muss marktrelevant sein UND mindestens
    einen konkreten Boersenticker mit klarer Long/Short-Richtung und einer Pro-Ticker-
    Konfidenz >= ALERT_MIN_TICKER_CONFIDENCE enthalten. Reine 'marktrelevant'-Meldungen
    ohne konkrete, hochsichere Aktie loesen KEINEN Alert mehr aus (der Nutzer will
    ausschliesslich die klarsten 'diese Aktie geht hoch/runter'-Signale). Wird an allen
    Alarm-Entscheidungspunkten verwendet (poll_once, Resend-Pfad, manuelle Tests),
    damit die strenge Schwelle nirgends umgangen werden kann - insbesondere durfte der
    Resend-Pfad (get_pending_alerts) frueher jede marktrelevante Meldung einen Zyklus
    spaeter doch noch ungefiltert alarmieren."""
    if classification is None or not classification.is_market_relevant:
        return False
    if classification.confidence < ALERT_CONFIDENCE_THRESHOLD:
        return False
    if ALERT_MIN_TICKER_CONFIDENCE <= 0:
        # Praezisions-Filter deaktiviert: altes Verhalten (jede marktrelevante Meldung
        # oberhalb von ALERT_CONFIDENCE_THRESHOLD alarmiert).
        return True
    return _strongest_ticker_confidence(classification) >= ALERT_MIN_TICKER_CONFIDENCE


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
                "eingestufte Meldungen analysiert (direkte Trump-Posts sowie harte "
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


async def _classify_and_store(raw, semaphore: asyncio.Semaphore, recent_context: list[dict]):
    priority = is_high_priority(raw)
    async with semaphore:
        try:
            classification = await classify(
                raw.text, recent_context=recent_context, priority=priority
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


def _row_to_alert_tuple(row: dict) -> tuple:
    raw = RawStatement(source=row["source"], source_id=row["source_id"], text=row["text"], url=row["url"])
    classification = Classification(
        is_market_relevant=bool(row["is_market_relevant"]),
        sentiment=row["sentiment"],
        confidence=row["confidence"] or 0.0,
        ticker_calls=row["tickers"],
        sectors=row["sectors"],
        reasoning=row["reasoning"] or "",
        related_topic_id=row.get("related_topic_id"),
        is_major_escalation=bool(row.get("is_major_escalation")),
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


async def poll_once(sources, semaphore: asyncio.Semaphore):
    await _resend_pending_alerts()

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
