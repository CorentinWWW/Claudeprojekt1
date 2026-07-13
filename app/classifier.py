"""Klassifiziert einen Statement-Text mit Claude: marktrelevant? Sentiment? betroffene Ticker/Sektoren?"""
import json
import logging

from anthropic import Anthropic

from app.config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from app.db import Classification

logger = logging.getLogger(__name__)

_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

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
                    "neutral = kein klarer Effekt oder gemischt."
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
                    "Leer lassen, wenn keine konkrete Firma genannt/gemeint ist."
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

SYSTEM_PROMPT = (
    "Du bist ein Finanzanalyse-Assistent. Du bekommst eine einzelne Aussage/ein Zitat "
    "von Donald Trump (aus Reden, Social-Media-Posts oder Presseberichten). "
    "Schaetze ein, ob und wie diese Aussage Aktienmaerkte beeinflussen koennte. "
    "Sei konservativ: setze is_market_relevant nur auf true, wenn ein plausibler "
    "wirtschaftlicher Zusammenhang besteht (z.B. Zoelle, Handelspolitik, "
    "Zentralbank/Zinsen, konkrete Unternehmen/Branchen, Regulierung, Sanktionen, "
    "Steuerpolitik, Ausgabenprogramme). Reine politische/persoenliche Aussagen ohne "
    "Marktbezug sind is_market_relevant=false. Deine Einschaetzung ist Analyse, "
    "keine Finanzberatung."
)


def classify(text: str) -> Classification:
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY ist nicht gesetzt")

    response = _client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_statement"},
        messages=[
            {
                "role": "user",
                "content": f'Aussage:\n"""\n{text}\n"""',
            }
        ],
    )

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
    return Classification(
        is_market_relevant=False,
        sentiment="neutral",
        confidence=0.0,
        tickers=[],
        sectors=[],
        reasoning="Konnte nicht klassifiziert werden.",
    )
