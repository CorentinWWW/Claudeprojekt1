import asyncio
import html
import logging
from collections import Counter

import httpx

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from app.db import Classification, RawStatement

logger = logging.getLogger(__name__)

SENTIMENT_EMOJI = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}
TELEGRAM_MAX_LENGTH = 4096
DIGEST_MAX_DETAIL_LINES = 15


def _format_message(raw: RawStatement, classification: Classification) -> str:
    emoji = SENTIMENT_EMOJI.get(classification.sentiment, "⚪")
    tickers = ", ".join(classification.tickers) or "—"
    sectors = ", ".join(classification.sectors) or "—"
    lines = [
        f"{emoji} <b>Markt-relevante Trump-Aussage</b> "
        f"({html.escape(classification.sentiment)}, "
        f"Konfidenz {classification.confidence:.0%})",
        f"Quelle: {html.escape(raw.source)}",
        "",
        html.escape(raw.text[:500]),
        "",
        f"Ticker: {html.escape(tickers)}",
        f"Sektoren: {html.escape(sectors)}",
        f"Begründung: {html.escape(classification.reasoning)}",
    ]
    if raw.url:
        lines.append(f'<a href="{html.escape(raw.url)}">Link zur Quelle</a>')
    return "\n".join(lines)


async def _send(text: str, retries: int = 2) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram nicht konfiguriert, ueberspringe Nachricht.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    attempt = 0
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 2)
                    attempt += 1
                    if attempt > retries:
                        logger.warning("Telegram-Rate-Limit dauerhaft, gebe auf.")
                        return False
                    logger.info("Telegram-Rate-Limit, warte %ss", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                logger.warning(
                    "Telegram lehnte Nachricht ab (Status %s): %s",
                    resp.status_code,
                    resp.text[:300],
                )
                return False
            except Exception:
                attempt += 1
                if attempt > retries:
                    logger.warning("Telegram-Alert konnte nicht gesendet werden.", exc_info=True)
                    return False
                await asyncio.sleep(2 * attempt)


async def send_alert(raw: RawStatement, classification: Classification) -> bool:
    return await _send(_format_message(raw, classification))


def _format_digest(items: list[tuple[RawStatement, Classification, int]]) -> str:
    sentiment_counts = Counter(c.sentiment for _, c, _ in items)
    counts_str = " ".join(
        f"{SENTIMENT_EMOJI.get(s, '⚪')} {n}" for s, n in sentiment_counts.items()
    )
    header = (
        f"📊 <b>{len(items)} marktrelevante Trump-Meldungen in diesem Zyklus</b>\n{counts_str}"
    )

    sorted_items = sorted(items, key=lambda item: item[1].confidence, reverse=True)
    lines = []
    for raw, classification, _ in sorted_items[:DIGEST_MAX_DETAIL_LINES]:
        emoji = SENTIMENT_EMOJI.get(classification.sentiment, "⚪")
        tickers = ", ".join(classification.tickers) or "–"
        title = html.escape(raw.text[:120])
        lines.append(f"{emoji} {title} (Ticker: {html.escape(tickers)})")

    remaining = len(items) - len(lines)
    body = "\n".join(lines)
    if remaining > 0:
        body += f"\n… und {remaining} weitere (Details im Log der Ausfuehrung)"

    text = f"{header}\n\n{body}"
    if len(text) > TELEGRAM_MAX_LENGTH:
        text = text[: TELEGRAM_MAX_LENGTH - 20] + "\n…(gekürzt)"
    return text


async def send_digest_alert(items: list[tuple[RawStatement, Classification, int]]) -> bool:
    """Buendelt mehrere gleichzeitig alarmwuerdige Statements (z.B. bei einem
    ploetzlichen Nachrichtenschub) in einer einzigen Telegram-Nachricht statt
    einer Flut von Einzelnachrichten."""
    if not items:
        return False
    return await _send(_format_digest(items))


async def send_text(text: str) -> bool:
    return await _send(text)


async def send_startup_notice(active_sources: list[str]) -> None:
    sources_str = ", ".join(active_sources) if active_sources else "keine"
    text = (
        "🟢 <b>Trump Market Impact Monitor gestartet</b>\n"
        f"Aktive Quellen: {html.escape(sources_str)}\n"
        "Du bekommst hier ab jetzt Alerts bei marktrelevanten Aussagen."
    )
    sent = await send_text(text)
    if not sent and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        logger.warning(
            "Start-Heartbeat konnte nicht an Telegram gesendet werden - "
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID pruefen."
        )
