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


def _sorted_by_confidence(ticker_calls: list[dict]) -> list[dict]:
    # Auf Nutzerwunsch: die Einschaetzung, bei der Claude am sichersten ist, steht
    # ganz oben. Fehlende/None-Konfidenz (z.B. alte DB-Zeilen vor Einfuehrung dieses
    # Felds) wird dabei als 0.0 behandelt statt einen Vergleichsfehler auszuloesen.
    return sorted(ticker_calls, key=lambda tc: tc.get("confidence") or 0.0, reverse=True)


def _format_ticker_calls_compact(ticker_calls: list[dict]) -> str:
    if not ticker_calls:
        return "–"
    return ", ".join(
        f"{html.escape(tc.get('ticker', '?'))}{_direction_arrow(tc.get('direction'))}"
        f"{(tc.get('confidence') or 0.0):.0%}"
        for tc in _sorted_by_confidence(ticker_calls)
    )


def _format_ticker_lines(ticker_calls: list[dict]) -> str:
    """Eine Zeile pro Ticker mit ausgeschriebenem Long/Short und eigener Konfidenz,
    sortiert nach Konfidenz absteigend (sicherste Einschaetzung zuerst)."""
    if not ticker_calls:
        return "📈 –"
    lines = [
        f"{_direction_arrow(tc.get('direction'))} {html.escape(tc.get('ticker') or '?')} "
        f"({html.escape(tc.get('direction') or '?')}) – {(tc.get('confidence') or 0.0):.0%}"
        for tc in _sorted_by_confidence(ticker_calls)
    ]
    return "\n".join(lines)


def _short_headline(classification: Classification, raw: RawStatement, max_length: int) -> str:
    text = (classification.reasoning or raw.text or "").strip()
    if len(text) <= max_length:
        return text
    cut = text[:max_length].rsplit(" ", 1)[0]
    return (cut or text[:max_length]) + "…"


def _format_message(raw: RawStatement, classification: Classification) -> str:
    emoji = SENTIMENT_EMOJI.get(classification.sentiment, "⚪")
    escalation_prefix = (
        "⚠️ "
        if classification.related_topic_id is not None and classification.is_major_escalation
        else ""
    )
    ticker_lines = _format_ticker_lines(classification.ticker_calls)
    # Auf Nutzerwunsch: die Ueberschrift wird NICHT um ihrer selbst willen abgekuerzt
    # (voller Begruendungstext). _short_headline() truncatet nur, wenn der Text den
    # verbleibenden Platz in der Nachricht tatsaechlich sprengen wuerde - ein reines
    # Sicherheitsnetz fuer einen pathologisch langen Begruendungstext, das im
    # Normalfall (System-Prompt verlangt 1-2 Saetze) nie greift, weil max_length dann
    # weit ueber jeder realistischen Textlaenge liegt.
    # Vorab-Budget (Normalfall): +1 fuer die von _short_headline() ggf. angehaengte
    # Ellipse. Reicht bei ganz normalem Text locker, verhindert aber NICHT, dass
    # html.escape() den Text nachtraeglich nochmal deutlich verlaengert, falls er
    # ungewoehnlich viele "<"/"&"/etc. enthaelt (jedes Zeichen kann bis zu 5 Zeichen
    # werden - "<" -> "&lt;") - dafuer gibt es den harten Notfall-Schnitt danach.
    fixed_overhead = len(f"{emoji} {classification.confidence:.0%} — \n{ticker_lines}") + len(escalation_prefix)
    max_headline_len = max(50, TELEGRAM_MAX_LENGTH - fixed_overhead - 1)
    headline = html.escape(escalation_prefix + _short_headline(classification, raw, max_headline_len))
    text = f"{emoji} {classification.confidence:.0%} — {headline}\n{ticker_lines}"

    if len(text) > TELEGRAM_MAX_LENGTH:
        # Absoluter Notfall (z.B. Begruendungstext voller "<"/"&", die durchs
        # Escaping stark aufblaehen): bereits escapten Ueberschrift-String hart auf
        # den tatsaechlich verbleibenden Platz kuerzen. Im Extremfall entsteht dabei
        # ein abgeschnittenes HTML-Entity-Fragment (z.B. "&am" statt "&amp;") - das
        # zeigt Telegram als harmlosen literalen Text, bricht aber (anders als ein
        # abgeschnittenes Tag) niemals die HTML-Struktur der Nachricht, da hier keine
        # Tags im Spiel sind.
        overflow = len(text) - TELEGRAM_MAX_LENGTH
        headline = headline[:-overflow] if overflow < len(headline) else ""
        text = f"{emoji} {classification.confidence:.0%} — {headline}\n{ticker_lines}"
    return text


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


_DIGEST_HEADLINE_MAX_LENGTH = 70


def _format_digest(items: list[tuple[RawStatement, Classification, int]]) -> str:
    sentiment_counts = Counter(c.sentiment for _, c, _ in items)
    counts_str = " ".join(
        f"{SENTIMENT_EMOJI.get(s, '⚪')} {n}" for s, n in sentiment_counts.items()
    )
    header = f"📊 <b>{len(items)} Meldungen</b> {counts_str}"

    sorted_items = sorted(items, key=lambda item: item[1].confidence, reverse=True)
    candidate_lines = []
    for raw, classification, _ in sorted_items[:DIGEST_MAX_DETAIL_LINES]:
        emoji = SENTIMENT_EMOJI.get(classification.sentiment, "⚪")
        headline = html.escape(_short_headline(classification, raw, max_length=_DIGEST_HEADLINE_MAX_LENGTH))
        ticker_str = _format_ticker_calls_compact(classification.ticker_calls)
        candidate_lines.append(f"{emoji} {classification.confidence:.0%} {headline} — {ticker_str}")

    # Zeilenweise statt zeichenweise budgetieren: eine reine Zeichen-Kappung des
    # fertigen Texts koennte mitten in einem HTML-Tag oder einer Entity enden -
    # Telegram wuerde dann die GESAMTE Nachricht wegen ungueltigem HTML ablehnen.
    # Stattdessen wird bei Platzmangel immer nur eine ganze Detailzeile weniger
    # angezeigt, nie ein Teilstring einer Zeile.
    included_lines: list[str] = []
    for line in candidate_lines:
        remaining_after = len(items) - (len(included_lines) + 1)
        placeholder = f"\n+{remaining_after} weitere" if remaining_after > 0 else ""
        trial_body = "\n".join(included_lines + [line])
        trial_text = f"{header}\n\n{trial_body}{placeholder}"
        if len(trial_text) > TELEGRAM_MAX_LENGTH:
            break
        included_lines.append(line)

    remaining = len(items) - len(included_lines)
    body = "\n".join(included_lines)
    if remaining > 0:
        body += f"\n+{remaining} weitere"

    text = f"{header}\n\n{body}"
    if len(text) > TELEGRAM_MAX_LENGTH:
        # Aeusserster Notfall (z.B. schon der Header allein zu lang): komplett
        # ohne Detailzeilen - immer noch vollstaendiges, gueltiges HTML.
        text = header
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
