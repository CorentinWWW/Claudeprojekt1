"""Klassifiziert einen Statement-Text mit Claude: marktrelevant? Sentiment? betroffene
Ticker mit Long/Short-Einschaetzung? Ist es dasselbe Thema wie eine heute schon
gemeldete Meldung, und falls ja, eskaliert es genug fuer einen erneuten Alert?
"""
import datetime
import logging
import re
from typing import Optional

from anthropic import APIStatusError, AsyncAnthropic

from app.config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MAX_RETRIES,
    CLAUDE_MODEL,
    CLAUDE_TIMEOUT_SECONDS,
    MAX_CLASSIFICATIONS_PER_DAY,
    PRIORITY_CLASSIFICATIONS_PER_DAY,
)
from app.db import Classification, record_classification_call, reserve_classification_call_slot

logger = logging.getLogger(__name__)

_client = (
    AsyncAnthropic(
        api_key=ANTHROPIC_API_KEY,
        max_retries=CLAUDE_MAX_RETRIES,
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    if ANTHROPIC_API_KEY
    else None
)

CLASSIFY_TOOL = {
    "name": "classify_statement",
    "description": (
        "Bewerte eine Nachricht, Aussage oder ein Ereignis im Hinblick auf ihre "
        "Relevanz und Auswirkung fuer Aktienmaerkte."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_market_relevant": {
                "type": "boolean",
                "description": (
                    "true, wenn die Aussage voraussichtlich Aktienkurse, Sektoren, "
                    "Zinsen, Handelspolitik oder Maerkte allgemein beeinflusst."
                ),
            },
            "sentiment": {
                "type": "string",
                "enum": ["positive", "negative", "neutral"],
                "description": (
                    "Erwartete Kursrichtung fuer die genannten Ticker/Sektoren: "
                    "positive = eher steigend, negative = eher fallend, "
                    "neutral = kein klarer Effekt oder gemischt/unklar."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "Konfidenz der Einschaetzung zwischen 0.0 und 1.0.",
            },
            "ticker_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "direction": {
                            "type": "string",
                            "enum": ["long", "short"],
                            "description": (
                                "long = steigende Kurse erwartet, short = fallende "
                                "Kurse erwartet, jeweils bezogen auf DIESEN Ticker "
                                "(nicht zwangslaeufig identisch mit dem "
                                "Gesamt-Sentiment - z.B. koennen Zoelle auf Stahl "
                                "fuer Stahlproduzenten long und fuer Autobauer, die "
                                "Stahl einkaufen, short bedeuten)."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "description": (
                                "Konfidenz (0.0-1.0) SPEZIELL fuer diesen Ticker - kann von der "
                                "Gesamt-Konfidenz abweichen (z.B. ist der Sektor-Zusammenhang "
                                "klar, aber die Auswirkung auf GENAU dieses Unternehmen weniger "
                                "sicher, oder umgekehrt)."
                            ),
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "Ein kurzer Satz, warum long/short fuer diesen Ticker.",
                        },
                    },
                    "required": ["ticker", "direction", "confidence", "reasoning"],
                },
                "description": (
                    "Konkret betroffene Boersenticker mit je einer Long/Short-Einschaetzung. "
                    "Nur Ticker verwenden, bei denen du dir wirklich sicher bist, dass sie "
                    "korrekt und real sind. Leer lassen, wenn keine konkrete Firma "
                    "genannt/eindeutig gemeint ist oder du dir beim Ticker unsicher bist."
                ),
            },
            "sectors": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Betroffene Sektoren/Themen, z.B. ['Halbleiter', 'Automobil', "
                    "'Ruestung', 'Energie', 'Gesamtmarkt']."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Ein bis zwei Saetze Begruendung auf Deutsch.",
            },
            "related_topic_id": {
                "type": ["integer", "null"],
                "description": (
                    "Falls im Prompt eine Liste 'bereits heute gemeldete Themen' mit IDs "
                    "vorhanden ist UND diese neue Aussage im Kern zu einem dieser Themen "
                    "gehoert (auch bei anderem Wortlaut/neuen Details): dessen ID hier "
                    "eintragen. Sonst null."
                ),
            },
            "is_major_escalation": {
                "type": "boolean",
                "description": (
                    "Nur relevant falls related_topic_id gesetzt ist: true, wenn diese neue "
                    "Information eine derart bedeutende Verschaerfung/neue Entwicklung "
                    "darstellt (z.B. von Androhung zu tatsaechlicher Umsetzung, deutliche "
                    "Eskalation eines Konflikts, neue harte Zahlen), dass eine erneute "
                    "Nachricht an den Nutzer trotzdem gerechtfertigt ist. Bei blosser "
                    "Wiederholung/Umformulierung derselben Fakten: false."
                ),
            },
        },
        "required": [
            "is_market_relevant",
            "sentiment",
            "confidence",
            "ticker_calls",
            "sectors",
            "reasoning",
        ],
    },
}


def _system_prompt() -> str:
    today = datetime.date.today().isoformat()
    return (
        f"Du bist ein Finanzanalyse-Assistent. Heutiges Datum: {today}. "
        "Du bekommst eine einzelne Nachricht, Aussage oder Meldung mit potenzieller "
        "Marktrelevanz - aus Reden, Social-Media-Posts, Pressemitteilungen, "
        "Quartalszahlen/Earnings-Calls, Wirtschaftsdaten-Veroeffentlichungen oder "
        "allgemeiner Wirtschafts-/Finanzberichterstattung, teils nur als Nachrichten-"
        "Ueberschrift vorliegend statt als woertliches Zitat. Die Quelle kann JEDE "
        "einflussreiche Person oder Institution sein (Staats- und Regierungschefs, "
        "Zentralbanker/Notenbanken, Minister/Behoerden, Unternehmenslenker/CEOs, "
        "Regulierer) oder ein Ereignis ohne einzelne Person (Quartalszahlen, "
        "Wirtschaftsdaten wie Inflation/Arbeitsmarkt/BIP, Fusionen/Uebernahmen, "
        "Rating-Aenderungen, Naturkatastrophen, geopolitische Ereignisse). "
        "Schaetze ein, ob und wie diese Meldung Aktienmaerkte beeinflussen koennte. "
        "Sei konservativ: setze is_market_relevant nur auf true, wenn ein plausibler "
        "wirtschaftlicher Zusammenhang besteht (z.B. Zoelle, Handelspolitik, "
        "Zentralbank/Zinsen, konkrete Unternehmen/Branchen, Regulierung, Sanktionen, "
        "Steuerpolitik, Ausgabenprogramme, Aussenpolitik mit Marktrelevanz, "
        "Unternehmenszahlen/-ausblick, M&A, Lieferketten, Rohstoffpreise). Reine "
        "politische/persoenliche Aussagen ohne Marktbezug sind is_market_relevant=false. "
        "WICHTIG - unterscheide konkrete, handlungsrelevante Meldungen von allgemeiner "
        "Wirtschaftsrhetorik: eine konkrete NEUE Entwicklung (z.B. eine bezifferte "
        "Zollrate, eine namentlich genannte Firma/Uebernahme/ein Deal, eine konkrete "
        "Sanktion, eine Personalie bei der Fed, ein tatsaechliches Quartalsergebnis) ist "
        "marktrelevant; vage Stimmungsmache ohne neuen Informationsgehalt ('die "
        "Wirtschaft laeuft grossartig', 'wir gewinnen') ist es nicht oder nur mit "
        "niedriger Konfidenz. "
        "Konfidenz kalibrieren: hohe Werte (>0.7) NUR bei konkreten, spezifischen, "
        "unmittelbar marktbewegenden Meldungen; niedrige Werte bei Vagem, Unklarem oder "
        "wenn die Meldung nur eine laengst bekannte Position wiederholt, ohne dass sich "
        "etwas Neues ergibt (blosse Wiederholung != neues Signal). "
        "Nenne nur Ticker, bei denen du dir des Kuerzels wirklich sicher bist - erfinde "
        "niemals einen Ticker und rate nicht; im Zweifel lieber nur den Sektor nennen "
        "und ticker_calls leer lassen. Verwende AUSSCHLIESSLICH das offizielle "
        "Boersenkuerzel der US-Boerse (NYSE/NASDAQ), in GROSSBUCHSTABEN, OHNE Boersen- "
        "oder Waehrungsprefix - also 'NVDA' (nicht 'NASDAQ:NVDA', 'Nvidia' oder "
        "'$NVDA'), 'XOM' fuer ExxonMobil, 'BRK.B' fuer Berkshire Hathaway Klasse B. "
        "Bevorzuge das meistgehandelte Primaerlisting. Ist ein betroffenes Unternehmen "
        "nicht boersennotiert oder kennst du sein exaktes Kuerzel nicht sicher, lass es "
        "weg und nenne stattdessen nur den Sektor. Bei jedem Ticker gib eine "
        "long/short-Einschaetzung UND eine eigene Konfidenz dafuer ab: ueberlege konkret, "
        "ob diese Meldung fuer GENAU dieses Unternehmen eher steigende (long) oder "
        "fallende (short) Kurse erwarten laesst - das kann pro Ticker unterschiedlich "
        "sein (Gewinner vs. Verlierer derselben Massnahme, z.B. Zoelle die einer Branche "
        "nuetzen und einer anderen schaden), und die Konfidenz pro Ticker kann von der "
        "Gesamt-Konfidenz abweichen (z.B. sicher, DASS ein Sektor betroffen ist, aber "
        "weniger sicher, WIE STARK genau dieser Ticker reagiert). Falls eine Liste "
        "bereits heute gemeldeter Themen mitgegeben wird, "
        "prüfe ob die neue Meldung im Kern dazugehoert und ob sie eine derart deutliche "
        "Verschaerfung darstellt, dass ein erneuter Alert gerechtfertigt ist (siehe "
        "related_topic_id/is_major_escalation). Deine Einschaetzung ist Analyse "
        "auf Basis der vorliegenden Meldung, keine Finanzberatung - der Nutzer trifft "
        "eigene Anlageentscheidungen auf eigenes Risiko."
    )


# Kontext-Snippet-Laenge: reicht, um ein Thema wiederzuerkennen (Tier-2-Dedup), spart
# aber gegenueber dem frueheren Wert (150) Input-Tokens pro Call - ohne die Anzahl der
# sichtbaren Themen (TOPIC_CONTEXT_MAX_ITEMS) zu reduzieren, die Dedup-Abdeckung bleibt
# also unveraendert. Gleicht die etwas ausfuehrlicheren Relevanz-/Konfidenz-Hinweise im
# System-Prompt kostenmaessig aus.
CONTEXT_SNIPPET_MAX_CHARS = 100


def _build_context_block(recent_context: Optional[list[dict]]) -> str:
    if not recent_context:
        return ""
    lines = [
        f"[{item['id']}] {item['text'][:CONTEXT_SNIPPET_MAX_CHARS]} "
        f"(sentiment: {item.get('sentiment') or 'unbekannt'})"
        for item in recent_context
    ]
    return (
        "\n\nBereits heute gemeldete Themen (IDs zur Referenz, neueste zuerst):\n"
        + "\n".join(lines)
        + "\n\nPruefe fuer die folgende neue Aussage, ob sie im Kern zu einem dieser "
        "Themen gehoert (related_topic_id) und ob sie eine wesentliche Eskalation "
        "darstellt (is_major_escalation)."
    )


FALLBACK_CLASSIFICATION = Classification(
    is_market_relevant=False,
    sentiment="neutral",
    confidence=0.0,
    ticker_calls=[],
    sectors=[],
    reasoning="Konnte nicht klassifiziert werden (keine gueltige Antwort erhalten).",
)


class DailyCapExceeded(RuntimeError):
    """Signalisiert, dass MAX_CLASSIFICATIONS_PER_DAY erreicht ist - eigene Klasse
    (statt generischem RuntimeError), damit Aufrufer das gezielt und ohne vollen
    Traceback pro uebersprungenem Statement loggen koennen (siehe orchestrator.py:
    _classify_and_store)."""


def _clamped_confidence(value) -> float:
    # Ein explizit gesetztes JSON "null" (statt fehlendem Feld) liefert bei .get()
    # trotzdem None zurueck - float(None) wuerde mit TypeError crashen. Ausserdem
    # koennte ein Modell ausserhalb des Schemas einen Wert > 1, < 0 oder einen
    # nicht-numerischen String liefern.
    try:
        parsed = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(1.0, parsed))


# Gueltiges US-Boersenkuerzel: 1-5 Grossbuchstaben, optional ein Klassen-/Vorzugs-
# Suffix wie ".B" (BRK.B) oder "-A" (manche Broker-Notationen). Bewusst streng, damit
# offensichtliche Nicht-Ticker (ganze Firmennamen, Saetze, "N/A", "TBD", eine Branche)
# gar nicht erst als vermeintliches Kuerzel beim Nutzer landen.
_TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?$")
# Fuehrendes Boersen-/Marktkuerzel-Prefix ("NASDAQ:NVDA", "NYSE: XOM", "NYSEARCA:SPY"),
# das manche Modelle mitliefern - wird vor der Validierung entfernt.
_EXCHANGE_PREFIX = re.compile(r"^[A-Z]{2,8}:\s*", re.IGNORECASE)
# Platzhalter, die zufaellig das Ticker-Format erfuellen, aber offensichtlich kein
# echtes Kuerzel meinen (kommt vor, wenn das Modell keinen konkreten Ticker hat, das
# Feld aber trotzdem fuellt). Werden verworfen.
_TICKER_PLACEHOLDERS = {"TBD", "TBA", "NONE", "NULL", "UNKNOWN", "NA", "XYZ"}


def _normalize_ticker(symbol) -> Optional[str]:
    """Bereinigt ein von Claude geliefertes Ticker-Feld und gibt ein gueltiges Kuerzel
    in Grossbuchstaben zurueck - oder None, wenn es kein plausibles Boersenkuerzel ist
    (dann wird der Eintrag verworfen statt Muell an den Nutzer zu schicken)."""
    if not isinstance(symbol, str):
        return None
    s = symbol.strip()
    s = _EXCHANGE_PREFIX.sub("", s)  # "NASDAQ:NVDA" -> "NVDA"
    s = s.lstrip("$").strip()        # "$AAPL" -> "AAPL" (Cashtag)
    s = s.upper()
    if s in _TICKER_PLACEHOLDERS:
        return None
    if _TICKER_PATTERN.match(s):
        return s
    return None


def _clean_ticker_calls(raw_ticker_calls) -> list[dict]:
    """Normalisiert/validiert die Ticker und dedupliziert sie (bei mehrfach genanntem
    Kuerzel gewinnt der Eintrag mit der hoechsten Ticker-Konfidenz)."""
    by_ticker: dict[str, dict] = {}
    for tc in raw_ticker_calls:
        if not isinstance(tc, dict):
            continue
        ticker = _normalize_ticker(tc.get("ticker"))
        if ticker is None:
            continue
        entry = {
            "ticker": ticker,
            "direction": tc.get("direction") or "long",
            "confidence": _clamped_confidence(tc.get("confidence")),
            "reasoning": tc.get("reasoning", ""),
        }
        existing = by_ticker.get(ticker)
        if existing is None or entry["confidence"] > existing["confidence"]:
            by_ticker[ticker] = entry
    return list(by_ticker.values())


def _classification_from_tool_input(data: dict) -> Classification:
    ticker_calls = _clean_ticker_calls(data.get("ticker_calls") or [])
    confidence = _clamped_confidence(data.get("confidence"))
    return Classification(
        is_market_relevant=bool(data.get("is_market_relevant", False)),
        sentiment=data.get("sentiment") or "neutral",
        confidence=confidence,
        ticker_calls=ticker_calls,
        sectors=data.get("sectors") or [],
        reasoning=data.get("reasoning") or "",
        related_topic_id=data.get("related_topic_id"),
        is_major_escalation=bool(data.get("is_major_escalation", False)),
    )


def _parse_response(response) -> Classification:
    for block in response.content:
        if block.type == "tool_use" and block.name == "classify_statement":
            try:
                return _classification_from_tool_input(block.input)
            except (TypeError, ValueError, AttributeError):
                # Ein Modell (insbesondere ein guenstigeres wie das aktuelle Default
                # Haiku) koennte ausserhalb des Schemas liegende Werte liefern (z.B.
                # confidence als String, ticker_calls-Eintraege ohne dict-Struktur) -
                # das soll classify() nicht mit einer haesslichen Exception abschiessen,
                # sondern sauber auf den Fallback zurueckfallen wie bei komplett
                # fehlender tool_use-Antwort.
                logger.warning(
                    "Unerwartete/fehlerhafte tool_use-Antwort von Claude, fallback auf neutral",
                    exc_info=True,
                )
                return FALLBACK_CLASSIFICATION
    logger.warning("Keine tool_use Antwort von Claude erhalten, fallback auf neutral")
    return FALLBACK_CLASSIFICATION


async def classify(
    text: str,
    recent_context: Optional[list[dict]] = None,
    _bypass_daily_cap: bool = False,
    priority: bool = False,
    model: Optional[str] = None,
) -> Classification:
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY ist nicht gesetzt")

    # Zentraler Kostendeckel: JEDER Aufrufer (Orchestrator, Dashboard-/api/test,
    # manueller Test in run_once.py) laeuft ueber diese eine Funktion, daher genuegt
    # die Pruefung hier statt an jeder einzelnen Aufrufstelle. _bypass_daily_cap ist
    # ausschliesslich fuer selftest() gedacht - ein bereits ausgeschoepftes Tages-
    # Limit soll nicht dazu fuehren, dass der Claude-Erreichbarkeits-Check beim Start
    # fehlschlaegt und der gesamte Monitoring-Loop deswegen gar nicht erst anspringt.
    # Der Selftest-Call soll aber trotzdem GEZAEHLT werden (record_classification_call)
    # - sonst waere jeder Prozess-Neustart ein unsichtbarer, nicht mitgezaehlter
    # Kostenpunkt ausserhalb des dokumentierten "harten" Tages-Limits.
    #
    # priority=True (als besonders wichtig eingestufte Meldung, siehe orchestrator.py:
    # is_high_priority) darf die Reserve oberhalb des normalen Limits nutzen: derselbe
    # atomare Zaehler, nur mit hoeherem Limit (MAX + PRIORITY). Dadurch bleiben normale
    # Meldungen ab MAX gesperrt, waehrend wichtige Meldungen bis zum absoluten
    # Tages-Maximum (MAX + PRIORITY) noch durchkommen.
    if _bypass_daily_cap:
        record_classification_call()
    else:
        limit = MAX_CLASSIFICATIONS_PER_DAY
        if priority:
            limit += PRIORITY_CLASSIFICATIONS_PER_DAY
        if not reserve_classification_call_slot(limit):
            raise DailyCapExceeded(
                f"Taegliches Claude-Klassifikations-Limit ({limit}) erreicht - um die "
                "API-Kosten zu begrenzen, werden bis zum naechsten Tag (UTC) "
                + ("auch keine wichtigen " if priority else "keine weiteren ")
                + "Statements klassifiziert. Siehe MAX_CLASSIFICATIONS_PER_DAY / "
                "PRIORITY_CLASSIFICATIONS_PER_DAY in .env."
            )

    context_block = _build_context_block(recent_context)
    response = await _client.messages.create(
        # model kann fuer die Zweitmeinung bei Grenzfaellen (orchestrator: Borderline-
        # Eskalation) durch ein staerkeres Modell ueberschrieben werden.
        model=model or CLAUDE_MODEL,
        max_tokens=2048,
        system=_system_prompt(),
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_statement"},
        messages=[
            {
                "role": "user",
                "content": f'Aussage:\n"""\n{text}\n"""{context_block}',
            }
        ],
    )
    if response.stop_reason == "max_tokens":
        # Die Tool-Use-Antwort wurde mitten im JSON abgeschnitten (z.B. bei vielen
        # ticker_calls mit langen Begruendungen) - ein Parse-Versuch koennte
        # scheitern ODER (schlimmer) ein unvollstaendiges/korruptes Ergebnis als
        # gueltig durchgehen lassen. Lieber sauber als Fehler behandeln, dann
        # greift dieselbe Retry-/Skip-Logik wie bei jedem anderen Klassifikations-
        # fehler (siehe orchestrator.py: _classify_and_store).
        raise RuntimeError(
            "Claude-Antwort wurde bei max_tokens abgeschnitten - Ergebnis waere "
            "unvollstaendig. Statement wird diesen Zyklus uebersprungen."
        )
    return _parse_response(response)


async def selftest() -> None:
    """Wirft eine Exception mit klarer Ursache, falls Claude nicht erreichbar/konfiguriert ist.

    Wird beim Start aufgerufen, damit ein falscher/fehlender API-Key sofort auffaellt
    statt erst beim ersten echten Statement irgendwann spaeter im Log unterzugehen.
    """
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY ist nicht gesetzt")

    try:
        result = await classify(
            "Testaussage: Ich werde neue Zoelle auf importierte Stahlprodukte verhaengen.",
            _bypass_daily_cap=True,
        )
    except APIStatusError as exc:
        raise RuntimeError(
            f"Claude-API antwortete mit Fehler (Status {exc.status_code}): {exc.message}. "
            "Pruefe ANTHROPIC_API_KEY und CLAUDE_MODEL in .env."
        ) from exc

    if result is FALLBACK_CLASSIFICATION:
        raise RuntimeError(
            "Claude-Selftest lieferte keine gueltige strukturierte Antwort. "
            "Pruefe CLAUDE_MODEL in .env."
        )
