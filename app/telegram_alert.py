import asyncio
import html
import logging
from collections import Counter
from typing import Optional

import httpx

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from app.db import Classification, RawStatement

logger = logging.getLogger(__name__)

SENTIMENT_EMOJI = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}
TELEGRAM_MAX_LENGTH = 4096
DIGEST_MAX_DETAIL_LINES = 15


def _direction_arrow(direction: Optional[str]) -> str:
    if direction == "long":
        return "🔺"
    if direction == "short":
        return "🔻"
    return "◽"


def _format_ticker_calls(ticker_calls: list[dict]) -> str:
    if not ticker_calls:
        return "–"
    lines = []
    for tc in ticker_calls:
        ticker = html.escape(tc.get("ticker", "?"))
        direction = html.escape(tc.get("direction") or "?")
        reasoning = html.escape(tc.get("reasoning", ""))
        arrow = _direction_arrow(tc.get("direction"))
        suffix = f" – {reasoning}" if reasoning else ""
        lines.append(f"{arrow} {ticker} ({direction}){suffix}")
    return "\n".join(lines)


def _format_ticker_calls_compact(ticker_calls: list[dict]) -> str:
    if not ticker_calls:
        return "–"
    return ", ".join(
        f"{html.escape(tc.get('ticker', '?'))}{_direction_arrow(tc.get('direction'))}"
        for tc in ticker_calls
    )


def _format_message(raw: RawStatement, classification: Classification) -> str:
    emoji = SENTIMENT_EMOJI.get(classification.sentiment, "⚪")
    sectors = ", ".join(classification.sectors) or "—"
    lines = [
        f"{emoji} <b>Markt-relevante Trump-Aussage</b> "
        f"({html.escape(classification.sentiment)}, "
        f"Konfidenz {classification.confidence:.0%})",
    ]
    if classification.related_topic_id is not None and classification.is_major_escalation:
        lines.append(
            f"⚠️ Eskalation von Statement #{html.escape(str(classification.related_topic_id))}"
        )
    lines += [
        f"Quelle: {html.escape(raw.source)}",
        "",
        html.escape(raw.text[:500]),
        "",
        f"Ticker:\n{_format_ticker_calls(classification.ticker_calls)}",
        f"Sektoren: {html.escape(sectors)}",
        f"Begründung: {html.escape(classification.reasoning)}",
        "<i>Keine Finanzberatung – eigene Anlageentscheidung auf eigenes Risiko.</i>",
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
    footer = "<i>Keine Finanzberatung – eigene Anlageentscheidung auf eigenes Risiko.</i>"

    sorted_items = sorted(items, key=lambda item: item[1].confidence, reverse=True)
    candidate_lines = []
    for raw, classification, _ in sorted_items[:DIGEST_MAX_DETAIL_LINES]:
        emoji = SENTIMENT_EMOJI.get(classification.sentiment, "⚪")
        title = html.escape(raw.text[:120])
        ticker_str = _format_ticker_calls_compact(classification.ticker_calls)
        candidate_lines.append(f"{emoji} {title} (Ticker: {ticker_str})")

    # Zeilenweise statt zeichenweise budgetieren: eine reine Zeichen-Kappung des
    # fertigen Texts (wie zuvor) koennte mitten in einem HTML-Tag oder einer Entity
    # enden - Telegram wuerde dann die GESAMTE Nachricht wegen ungueltigem HTML
    # ablehnen. Stattdessen wird bei Platzmangel immer nur eine ganze Detailzeile
    # weniger angezeigt, nie ein Teilstring einer Zeile.
    included_lines: list[str] = []
    for line in candidate_lines:
        remaining_after = len(items) - (len(included_lines) + 1)
        placeholder = (
            f"\n… und {remaining_after} weitere (Details im Log der Ausfuehrung)"
            if remaining_after > 0
            else ""
        )
        trial_body = "\n".join(included_lines + [line])
        trial_text = f"{header}\n\n{trial_body}{placeholder}\n\n{footer}"
        if len(trial_text) > TELEGRAM_MAX_LENGTH:
            break
        included_lines.append(line)

    remaining = len(items) - len(included_lines)
    body = "\n".join(included_lines)
    if remaining > 0:
        body += f"\n… und {remaining} weitere (Details im Log der Ausfuehrung)"

    text = f"{header}\n\n{body}\n\n{footer}"
    if len(text) > TELEGRAM_MAX_LENGTH:
        # Aeusserster Notfall (z.B. schon Header+Footer allein zu lang): komplett
        # ohne Detailzeilen - immer noch vollstaendiges, gueltiges HTML.
        text = f"{header}\n\n{footer}"
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
