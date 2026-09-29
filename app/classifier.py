"""Klassifiziert einen Statement-Text per KI: marktrelevant? Sentiment? betroffene
Ticker mit Long/Short-Einschaetzung? Ist es dasselbe Thema wie eine heute schon
gemeldete Meldung, und falls ja, eskaliert es genug fuer einen erneuten Alert?

Welche KI antwortet, entscheidet die Anbieter-Kette aus app/llm.py: kostenlose,
OpenAI-kompatible Anbieter (Gemini, Groq, Mistral, ...) zuerst, Claude nur noch
optional. Faellt einer aus (Limit, Stoerung, kaputter Key), uebernimmt der naechste.
"""
import asyncio
import dataclasses
import datetime
import logging
import math
import re
from typing import Optional

import anthropic
from anthropic import APIStatusError, AsyncAnthropic

from app import llm
from app.config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MAX_RETRIES,
    CLAUDE_MODEL,
    CLAUDE_TIMEOUT_SECONDS,
    LLM_MAX_WAIT_SECONDS,
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
            "expected_move_pct": {
                "type": ["number", "null"],
                "description": (
                    "Grobe Schaetzung der ERWARTETEN Kursbewegung des am staerksten "
                    "betroffenen Tickers in Prozent, als BETRAG ohne Vorzeichen (die "
                    "Richtung steckt bereits in ticker_calls.direction) - also z.B. 3 fuer "
                    "'ca. 3% Bewegung erwartet'. Nur eine ungefaehre Groessenordnung, kein "
                    "Kursziel. null, wenn keine sinnvolle Schaetzung moeglich ist."
                ),
            },
            "expected_horizon": {
                "type": ["string", "null"],
                "description": (
                    "Ueber welchen Zeitraum sich die erwartete Bewegung voraussichtlich "
                    "entfaltet: 'Stunden', 'Tage' oder 'Wochen'. null, wenn unklar."
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


# Fuer die OpenAI-kompatiblen Gratis-Anbieter (JSON-Modus statt Anthropic-Tool-Use):
# bewusst KOMPAKT statt des vollen CLASSIFY_TOOL-Schemas (~1500 Tokens). Groqs
# Gratis-Tarif erlaubt nur 200K Tokens/Tag pro Modell - jeder gesparte Token pro Call
# ist direkt mehr Klassifikationen pro Tag. Die Bedeutung der Felder erklaert der
# System-Prompt ohnehin schon. Ein Test prueft, dass hier jedes Schema-Feld vorkommt.
JSON_OUTPUT_INSTRUCTIONS = (
    "\n\nANTWORTFORMAT: Antworte AUSSCHLIESSLICH mit EINEM gueltigen JSON-Objekt - "
    "kein Markdown, keine Erklaerung davor oder danach. Felder:\n"
    '{"is_market_relevant": true|false, '
    '"sentiment": "positive"|"negative"|"neutral", '
    '"confidence": 0.0-1.0, '
    '"ticker_calls": [{"ticker": "US-Kuerzel wie NVDA", "direction": "long"|"short", '
    '"confidence": 0.0-1.0, "reasoning": "ein kurzer Satz"}], '
    '"sectors": ["z.B. Halbleiter"], '
    '"reasoning": "1-2 Saetze auf Deutsch", '
    '"related_topic_id": ID eines bereits gemeldeten Themas oder null, '
    '"is_major_escalation": true|false, '
    '"expected_move_pct": erwartete Bewegung des staerksten Tickers in Prozent '
    "(Betrag ohne Vorzeichen) oder null, "
    '"expected_horizon": "Stunden"|"Tage"|"Wochen"|null}\n'
    "ticker_calls bleibt [] wenn kein konkretes Unternehmen sicher betroffen ist."
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


_DIRECTION_SYNONYMS = {"buy": "long", "bullish": "long", "sell": "short", "bearish": "short"}
_SENTIMENT_SYNONYMS = {"bullish": "positive", "bearish": "negative", "mixed": "neutral"}


def _normalize_direction(value):
    """'Short'/' LONG '/'sell' -> 'short'/'long'/'short'. Nachgelagert zaehlt nur exakt
    'long'/'short' (actionable_tickers) - ein Gratis-Modell, das 'Short' schreibt, wuerde
    sonst stillschweigend verworfen. Fehlt der Wert: 'long' wie bisher."""
    if not value:
        return "long"
    v = str(value).strip().lower()
    return _DIRECTION_SYNONYMS.get(v, v)


def _as_bool(value) -> bool:
    """bool("false") ist True - kleinere Modelle liefern Wahrheitswerte gern als Text."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "ja", "1")
    return bool(value)


def _as_int_or_none(value) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
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
            "direction": _normalize_direction(tc.get("direction")),
            "confidence": _clamped_confidence(tc.get("confidence")),
            "reasoning": tc.get("reasoning", ""),
        }
        existing = by_ticker.get(ticker)
        if existing is None or entry["confidence"] > existing["confidence"]:
            by_ticker[ticker] = entry
    return list(by_ticker.values())


def _parse_expected_move(value) -> Optional[float]:
    """Parst die geschaetzte erwartete Bewegung zu einem nicht-negativen Prozent-Betrag
    oder None. Robust gegen null, Strings, Vorzeichen und absurde/nicht-endliche Werte
    (die Richtung steckt in direction, daher wird der Betrag genommen und bei >100%
    gedeckelt - ein Modell koennte sonst z.B. 9999 liefern)."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return min(100.0, abs(parsed))


def _normalize_sentiment(value) -> str:
    v = str(value or "").strip().lower()
    v = _SENTIMENT_SYNONYMS.get(v, v)
    return v if v in ("positive", "negative", "neutral") else "neutral"


def _normalize_sectors(value) -> list:
    # Ein einzelner String statt Liste wuerde spaeter Zeichen fuer Zeichen iteriert.
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(s) for s in value if s]
    return []


def _classification_from_tool_input(data: dict) -> Classification:
    ticker_calls = _clean_ticker_calls(data.get("ticker_calls") or [])
    confidence = _clamped_confidence(data.get("confidence"))
    horizon = data.get("expected_horizon")
    return Classification(
        is_market_relevant=_as_bool(data.get("is_market_relevant", False)),
        sentiment=_normalize_sentiment(data.get("sentiment")),
        confidence=confidence,
        ticker_calls=ticker_calls,
        sectors=_normalize_sectors(data.get("sectors")),
        reasoning=data.get("reasoning") or "",
        related_topic_id=_as_int_or_none(data.get("related_topic_id")),
        is_major_escalation=_as_bool(data.get("is_major_escalation", False)),
        expected_move_pct=_parse_expected_move(data.get("expected_move_pct")),
        expected_horizon=horizon.strip() if isinstance(horizon, str) and horizon.strip() else None,
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


NO_PROVIDER_MESSAGE = (
    "Kein KI-Anbieter konfiguriert - Klassifikation kann nicht laufen. Kostenlos z.B. "
    "GEMINI_API_KEY (https://aistudio.google.com/apikey) und/oder GROQ_API_KEY "
    "(https://console.groq.com/keys) in .env eintragen, siehe .env.example."
)


def _chain() -> list[str]:
    return llm.chain_order(anthropic_available=_client is not None)


def _secret_for(entry: str) -> Optional[str]:
    if entry == llm.ANTHROPIC:
        return ANTHROPIC_API_KEY
    spec = llm.provider_spec(entry)
    return spec.api_key if spec else None


def _target_configured(entry: str) -> bool:
    if entry == llm.ANTHROPIC:
        return _client is not None
    return llm.provider_spec(entry) is not None


def model_available(model_spec: str) -> bool:
    """Ist das Modell (z.B. CLAUDE_ESCALATION_MODEL) konfiguriert und gerade nicht
    pausiert? Fuer die Grenzfall-Zweitmeinung: ohne Anthropic-Guthaben/-Key soll sie
    still entfallen, statt pro Grenzfall einen Tages-Slot und einen Fehler zu kosten."""
    entry, _model = llm.resolve_model_spec(model_spec)
    return _target_configured(entry) and llm.is_available(entry)


def _anthropic_error_kind(exc: BaseException) -> str:
    if isinstance(exc, anthropic.RateLimitError):
        return llm.RATE_LIMIT
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return llm.AUTH
    if isinstance(exc, anthropic.NotFoundError):
        return llm.NOT_FOUND
    if isinstance(exc, anthropic.BadRequestError):
        # Leeres Guthaben/erreichtes Ausgabenlimit kommt bei Anthropic als 400 - live
        # erlebt: "You have reached your specified API usage limits."
        if re.search(r"credit balance|usage limit|billing", str(exc), re.IGNORECASE):
            return llm.QUOTA
        return llm.BAD_REQUEST
    if isinstance(exc, anthropic.APIStatusError):
        return llm.SERVER
    if isinstance(exc, anthropic.APIConnectionError):
        return llm.NETWORK
    if isinstance(exc, RuntimeError) and "max_tokens" in str(exc):
        return llm.INVALID_OUTPUT
    return llm.SERVER


def _user_message(text: str, context_block: str) -> str:
    return f'Aussage:\n"""\n{text}\n"""{context_block}'


async def _call_anthropic(text: str, context_block: str, model: Optional[str]) -> Classification:
    response = await _client.messages.create(
        # model kann fuer die Zweitmeinung bei Grenzfaellen (orchestrator: Borderline-
        # Eskalation) durch ein staerkeres Modell ueberschrieben werden.
        model=model or CLAUDE_MODEL,
        max_tokens=2048,
        system=_system_prompt(),
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_statement"},
        messages=[{"role": "user", "content": _user_message(text, context_block)}],
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


async def _call_openai(entry: str, text: str, context_block: str,
                       model_override: Optional[str] = None) -> Classification:
    data, model = await llm.chat_json(
        entry,
        system=_system_prompt() + JSON_OUTPUT_INSTRUCTIONS,
        user=_user_message(text, context_block),
        model_override=model_override,
    )
    if "is_market_relevant" not in data:
        raise llm.ProviderError(llm.INVALID_OUTPUT, "JSON ohne Pflichtfeld is_market_relevant")
    try:
        result = _classification_from_tool_input(data)
    except (TypeError, ValueError, AttributeError) as exc:
        raise llm.ProviderError(llm.INVALID_OUTPUT, f"JSON passt nicht zum Schema: {exc}") from exc
    name, _ = llm.parse_entry(entry)
    return dataclasses.replace(result, model_used=f"{name}:{model}")


async def _classify_single(entry: str, model: str, text: str, context_block: str) -> Classification:
    """Genau EIN Anbieter/Modell, ohne Kette - fuer die Grenzfall-Zweitmeinung."""
    if entry == llm.ANTHROPIC:
        try:
            result = await _call_anthropic(text, context_block, model)
        except Exception as exc:
            llm.mark_failure(
                entry, llm.ProviderError(_anthropic_error_kind(exc), f"{type(exc).__name__}: {exc}"),
                ANTHROPIC_API_KEY,
            )
            raise
        if result is FALLBACK_CLASSIFICATION:
            return result
        llm.mark_success(entry)
        return dataclasses.replace(result, model_used=model)
    try:
        result = await _call_openai(entry, text, context_block)
    except llm.ProviderError as err:
        llm.mark_failure(entry, err, _secret_for(entry))
        raise llm.LLMUnavailable(f"{entry}: {err.message}") from err
    llm.mark_success(entry)
    return result


async def _classify_chain(chain: list[str], text: str, context_block: str) -> Classification:
    """Probiert die Anbieter der Reihe nach; der erste, der liefert, gewinnt."""
    # Nur Claude konfiguriert (alter Betriebsmodus): Fehler unveraendert als Original-
    # Exception weiterreichen - der Orchestrator unterscheidet daran permanente
    # (401/403/404) von voruebergehenden Fehlern.
    only_anthropic = chain == [llm.ANTHROPIC]
    errors: dict[str, llm.ProviderError] = {}
    for round_no in range(2):
        for entry in chain:
            if not llm.is_available(entry):
                continue
            if entry == llm.ANTHROPIC:
                try:
                    result = await _call_anthropic(text, context_block, None)
                except Exception as exc:
                    err = llm.ProviderError(_anthropic_error_kind(exc), f"{type(exc).__name__}: {exc}")
                    llm.mark_failure(entry, err, ANTHROPIC_API_KEY)
                    if only_anthropic:
                        raise
                    errors[entry] = err
                    continue
                if result is FALLBACK_CLASSIFICATION:
                    if only_anthropic:
                        return result
                    err = llm.ProviderError(llm.INVALID_OUTPUT, "keine gueltige tool_use-Antwort")
                    llm.mark_failure(entry, err)
                    errors[entry] = err
                    continue
                llm.mark_success(entry)
                return dataclasses.replace(result, model_used=CLAUDE_MODEL)
            try:
                result = await _call_openai(entry, text, context_block)
            except llm.ProviderError as err:
                llm.mark_failure(entry, err, _secret_for(entry))
                errors[entry] = err
                continue
            llm.mark_success(entry)
            return result

        # Niemand hat geliefert. Ist ein Anbieter nur kurz pausiert (typisch: das
        # Minutenlimit), lohnt sich EIN kurzes Warten mehr als das Statement fallen
        # zu lassen. Tageslimits/kaputte Keys (lange Pause) -> sofort aufgeben.
        wait = llm.seconds_until_available(chain)
        if round_no == 0 and wait is not None and wait <= LLM_MAX_WAIT_SECONDS:
            if wait > 0:
                logger.info("Alle KI-Anbieter kurz pausiert - warte %.0fs auf den naechsten freien.", wait)
                await asyncio.sleep(wait)
            continue
        break

    summary = "; ".join(f"{entry}: {err.kind}" for entry, err in errors.items()) or (
        "alle Anbieter pausieren gerade (Limits/Stoerung), siehe /api/health -> llm_providers"
    )
    if errors and all(err.kind == llm.INVALID_OUTPUT for err in errors.values()):
        logger.warning("Kein Anbieter lieferte gueltiges JSON (%s) - fallback auf neutral.", summary)
        return FALLBACK_CLASSIFICATION
    if llm.all_permanently_broken(chain):
        raise llm.LLMConfigError(f"Alle KI-Anbieter scheitern dauerhaft (Key/Modell pruefen): {summary}")
    raise llm.LLMUnavailable(f"Kein KI-Anbieter konnte liefern: {summary}")


async def classify(
    text: str,
    recent_context: Optional[list[dict]] = None,
    _bypass_daily_cap: bool = False,
    priority: bool = False,
    model: Optional[str] = None,
) -> Classification:
    # Erst pruefen, OB ueberhaupt ein Anbieter in Frage kommt - sonst wuerde ein
    # aussichtsloser Aufruf trotzdem einen Slot des Tages-Kostendeckels verbrauchen.
    if model:
        target_entry, target_model = llm.resolve_model_spec(model)
        if not _target_configured(target_entry):
            raise llm.LLMUnavailable(f"Modell {model!r}: Anbieter nicht konfiguriert")
        if not llm.is_available(target_entry):
            raise llm.LLMUnavailable(f"Modell {model!r}: Anbieter pausiert gerade")
        chain = None
    else:
        chain = _chain()
        if not chain:
            raise llm.LLMConfigError(NO_PROVIDER_MESSAGE)

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
    if chain is None:
        return await _classify_single(target_entry, target_model, text, context_block)
    return await _classify_chain(chain, text, context_block)


class SelftestTransientError(RuntimeError):
    """Selbsttest gescheitert, aber nur voruebergehend (alle Anbieter gerade im
    Limit/gestoert). Der Monitoring-Loop soll trotzdem starten - Gratis-Minutenlimits
    erholen sich von selbst, ein nicht gestarteter Loop dagegen nie (bis zum naechsten
    Neustart war der Bot sonst komplett tot)."""


async def selftest() -> None:
    """Wirft eine Exception mit klarer Ursache, falls keine KI erreichbar/konfiguriert ist.

    Wird beim Start aufgerufen, damit ein falscher/fehlender API-Key sofort auffaellt
    statt erst beim ersten echten Statement irgendwann spaeter im Log unterzugehen.
    """
    if not _chain():
        raise RuntimeError(NO_PROVIDER_MESSAGE)

    try:
        result = await classify(
            "Testaussage: Ich werde neue Zoelle auf importierte Stahlprodukte verhaengen.",
            _bypass_daily_cap=True,
        )
    except APIStatusError as exc:
        # Nur im reinen Claude-Betrieb erreicht die Original-Exception den Aufrufer.
        raise RuntimeError(
            f"Claude-API antwortete mit Fehler (Status {exc.status_code}): {exc.message}. "
            "Pruefe ANTHROPIC_API_KEY und CLAUDE_MODEL in .env - oder trage einen "
            "kostenlosen Anbieter ein (GEMINI_API_KEY/GROQ_API_KEY, siehe .env.example)."
        ) from exc
    except llm.LLMConfigError as exc:
        raise RuntimeError(str(exc)) from exc
    except llm.LLMUnavailable as exc:
        raise SelftestTransientError(str(exc)) from exc

    if result is FALLBACK_CLASSIFICATION:
        raise RuntimeError(
            "KI-Selftest lieferte keine gueltige strukturierte Antwort. Modell-"
            "Einstellungen in .env pruefen (CLAUDE_MODEL bzw. <ANBIETER>_MODEL)."
        )
    logger.info("KI-Selftest erfolgreich mit %s.", result.model_used or CLAUDE_MODEL)
