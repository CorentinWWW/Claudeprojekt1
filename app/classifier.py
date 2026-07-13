"""Klassifiziert einen Statement-Text mit Claude: marktrelevant? Sentiment? betroffene
Ticker mit Long/Short-Einschaetzung? Ist es dasselbe Thema wie eine heute schon
gemeldete Meldung, und falls ja, eskaliert es genug fuer einen erneuten Alert?
"""
import datetime
import logging
from typing import Optional

from anthropic import APIStatusError, AsyncAnthropic

from app.config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MAX_RETRIES,
    CLAUDE_MODEL,
    CLAUDE_TIMEOUT_SECONDS,
)
from app.db import Classification

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
        "Bewerte eine Aussage von Donald Trump im Hinblick auf ihre Relevanz und "
        "Auswirkung fuer Aktienmaerkte."
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
                        "reasoning": {
                            "type": "string",
                            "description": "Ein kurzer Satz, warum long/short fuer diesen Ticker.",
                        },
                    },
                    "required": ["ticker", "direction", "reasoning"],
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
        "Du bekommst eine einzelne Aussage/ein Zitat von Donald Trump (aus Reden, "
        "Social-Media-Posts oder Presseberichten, teils nur als Nachrichten-Ueberschrift "
        "vorliegend statt als woertliches Zitat). "
        "Schaetze ein, ob und wie diese Aussage Aktienmaerkte beeinflussen koennte. "
        "Sei konservativ: setze is_market_relevant nur auf true, wenn ein plausibler "
        "wirtschaftlicher Zusammenhang besteht (z.B. Zoelle, Handelspolitik, "
        "Zentralbank/Zinsen, konkrete Unternehmen/Branchen, Regulierung, Sanktionen, "
        "Steuerpolitik, Ausgabenprogramme, Aussenpolitik mit Marktrelevanz). Reine "
        "politische/persoenliche Aussagen ohne Marktbezug sind is_market_relevant=false. "
        "Nenne nur Ticker, bei denen du wirklich sicher bist - im Zweifel lieber nur den "
        "Sektor nennen und ticker_calls leer lassen. Bei jedem Ticker gib eine "
        "long/short-Einschaetzung ab: ueberlege konkret, ob diese Aussage fuer GENAU "
        "dieses Unternehmen eher steigende (long) oder fallende (short) Kurse erwarten "
        "laesst - das kann pro Ticker unterschiedlich sein (Gewinner vs. Verlierer "
        "derselben Massnahme, z.B. Zoelle die einer Branche nuetzen und einer anderen "
        "schaden). Falls eine Liste bereits heute gemeldeter Themen mitgegeben wird, "
        "prüfe ob die neue Aussage im Kern dazugehoert und ob sie eine derart deutliche "
        "Verschaerfung darstellt, dass ein erneuter Alert gerechtfertigt ist (siehe "
        "related_topic_id/is_major_escalation). Deine Einschaetzung ist Analyse "
        "auf Basis der vorliegenden Aussage, keine Finanzberatung - der Nutzer trifft "
        "eigene Anlageentscheidungen auf eigenes Risiko."
    )


def _build_context_block(recent_context: Optional[list[dict]]) -> str:
    if not recent_context:
        return ""
    lines = [
        f"[{item['id']}] {item['text'][:150]} (sentiment: {item.get('sentiment') or 'unbekannt'})"
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


def _parse_response(response) -> Classification:
    for block in response.content:
        if block.type == "tool_use" and block.name == "classify_statement":
            data = block.input
            raw_ticker_calls = data.get("ticker_calls", []) or []
            ticker_calls = [
                {
                    "ticker": tc.get("ticker", ""),
                    "direction": tc.get("direction", "long"),
                    "reasoning": tc.get("reasoning", ""),
                }
                for tc in raw_ticker_calls
                if tc.get("ticker")
            ]
            return Classification(
                is_market_relevant=bool(data.get("is_market_relevant", False)),
                sentiment=data.get("sentiment", "neutral"),
                confidence=float(data.get("confidence", 0.0)),
                ticker_calls=ticker_calls,
                sectors=data.get("sectors", []) or [],
                reasoning=data.get("reasoning", ""),
                related_topic_id=data.get("related_topic_id"),
                is_major_escalation=bool(data.get("is_major_escalation", False)),
            )
    logger.warning("Keine tool_use Antwort von Claude erhalten, fallback auf neutral")
    return FALLBACK_CLASSIFICATION


async def classify(text: str, recent_context: Optional[list[dict]] = None) -> Classification:
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY ist nicht gesetzt")

    context_block = _build_context_block(recent_context)
    response = await _client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1536,
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
            "Testaussage: Ich werde neue Zoelle auf importierte Stahlprodukte verhaengen."
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
