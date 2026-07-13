import logging

import httpx

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from app.db import Classification, RawStatement

logger = logging.getLogger(__name__)

SENTIMENT_EMOJI = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}


def _format_message(raw: RawStatement, classification: Classification) -> str:
    emoji = SENTIMENT_EMOJI.get(classification.sentiment, "⚪")
    tickers = ", ".join(classification.tickers) or "—"
    sectors = ", ".join(classification.sectors) or "—"
    lines = [
        f"{emoji} *Markt-relevante Trump-Aussage* ({classification.sentiment}, "
        f"Konfidenz {classification.confidence:.0%})",
        f"Quelle: {raw.source}",
        "",
        raw.text[:500],
        "",
        f"Ticker: {tickers}",
        f"Sektoren: {sectors}",
        f"Begruendung: {classification.reasoning}",
    ]
    if raw.url:
        lines.append(f"Link: {raw.url}")
    return "\n".join(lines)


async def send_alert(raw: RawStatement, classification: Classification):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram nicht konfiguriert, ueberspringe Alert.")
        return

    text = _format_message(raw, classification)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
            )
            resp.raise_for_status()
    except Exception:
        logger.warning("Telegram-Alert konnte nicht gesendet werden.", exc_info=True)
