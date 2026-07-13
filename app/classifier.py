"""Klassifiziert einen Statement-Text mit Claude: marktrelevant? Sentiment? betroffene Ticker/Sektoren?"""
import datetime
import logging

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
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Boersenticker konkret betroffener Unternehmen, z.B. ['TSLA', 'AAPL']. "
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
        },
        "required": [
            "is_market_relevant",
            "sentiment",
            "confidence",
            "tickers",
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
        "Sektor nennen und tickers leer lassen. Deine Einschaetzung ist Analyse, "
        "keine Finanzberatung."
    )


FALLBACK_CLASSIFICATION = Classification(
    is_market_relevant=False,
    sentiment="neutral",
    confidence=0.0,
    tickers=[],
    sectors=[],
    reasoning="Konnte nicht klassifiziert werden (keine gueltige Antwort erhalten).",
)


def _parse_response(response) -> Classification:
    for block in response.content:
        if block.type == "tool_use" and block.name == "classify_statement":
            data = block.input
            return Classification(
                is_market_relevant=bool(data.get("is_market_relevant", False)),
                sentiment=data.get("sentiment", "neutral"),
                confidence=float(data.get("confidence", 0.0)),
                tickers=data.get("tickers", []) or [],
                sectors=data.get("sectors", []) or [],
                reasoning=data.get("reasoning", ""),
            )
    logger.warning("Keine tool_use Antwort von Claude erhalten, fallback auf neutral")
    return FALLBACK_CLASSIFICATION


async def classify(text: str) -> Classification:
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY ist nicht gesetzt")

    response = await _client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=_system_prompt(),
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_statement"},
        messages=[
            {
                "role": "user",
                "content": f'Aussage:\n"""\n{text}\n"""',
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
