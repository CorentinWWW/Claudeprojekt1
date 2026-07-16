import asyncio
import html
import logging
import re
import time
from collections import Counter
from typing import Optional

import httpx

from app.config import (
    ALERT_MIN_TICKER_CONFIDENCE,
    BLOCKLIST_TICKERS,
    CHART_URL_TEMPLATE,
    ENABLE_CHART_BUTTONS,
    ENABLE_MARKET_SESSION_INFO,
    ENABLE_VOLATILITY_FLAG,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from app.db import Classification, RawStatement
from app.market_hours import session_label, us_market_session

logger = logging.getLogger(__name__)

SENTIMENT_EMOJI = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}
TELEGRAM_MAX_LENGTH = 4096
DIGEST_MAX_DETAIL_LINES = 15
# Markiert im Alert die Ticker, die die Praezisions-Schwelle (ALERT_MIN_TICKER_CONFIDENCE)
# tatsaechlich erreichen - also die eigentlich handelbaren, hochsicheren Signale, im
# Gegensatz zu evtl. mitgelisteten Kontext-Tickern mit geringerer Konfidenz.
_ACTIONABLE_MARKER = "⭐"

# High-Volatility-Signalwoerter (#6): Themen, bei denen die Schwankung oft groesser ist
# als die klare Richtung - dann ist ein direktionaler Trade riskanter als z.B. ein
# Straddle. Bewusst breiter als der Prioritaets-Filter (harte geopolitische/geldpolitische
# Ereignisse zusaetzlich).
_VOLATILITY_PATTERN = re.compile(
    r"\b("
    r"tariffs?|zoll|zoelle|zölle|sanctions?|sanktion|embargo|shutdown|default|"
    r"war|krieg|invasion|nuclear|militar\w*|airstrike|"
    r"nationaliz\w*|verstaatlich\w*|export ban|import ban|price cap|"
    r"bailout|stimulus|federal reserve|interest rate|rate cut|rate hike|zinsen|leitzins"
    r")\b",
    re.IGNORECASE,
)


def _direction_arrow(direction: Optional[str]) -> str:
    if direction == "long":
        return "🔺"
    if direction == "short":
        return "🔻"
    return "◽"


def _session_segment() -> str:
    """Kompaktes Boersen-Session-Segment fuer den Nachrichtenkopf (#7) - leer, wenn
    deaktiviert oder keine Zeitzonendaten vorhanden."""
    if not ENABLE_MARKET_SESSION_INFO:
        return ""
    label = session_label(us_market_session())
    return f" · {label}" if label else ""


def _volatility_flag(text: Optional[str]) -> str:
    """High-Vol-Hinweiszeile (#6) oder leerer String."""
    if not ENABLE_VOLATILITY_FLAG or not text:
        return ""
    if _VOLATILITY_PATTERN.search(text):
        return "⚡ Hohe Volatilität – Richtung evtl. unsicher (ggf. Straddle)"
    return ""


def _thread_segment(thread: Optional[list]) -> str:
    """Kompakte Verlaufs-Zeitleiste bei einer Eskalation (#5): wie viele Meldungen zum
    Thema es gibt und wann/womit es begann. Der Basistext kommt aus der DB und wird
    daher HTML-escaped."""
    if not thread or len(thread) < 2:
        return ""
    root = thread[0]
    root_age = _format_age(root.get("ingested_at"))
    age_part = f" ({root_age})" if root_age else ""
    snippet = html.escape((root.get("text") or "").strip()[:60])
    return f"🧵 Teil einer Entwicklung ({len(thread)} Meldungen) – Beginn{age_part}: {snippet}"


def _build_chart_markup(ticker_calls: list[dict]) -> Optional[dict]:
    """Inline-Keyboard mit Chart-Links (#9) fuer die handelbaren Ticker (max. 3),
    sortiert nach Konfidenz. None, wenn deaktiviert oder kein handelbarer Ticker."""
    if not ENABLE_CHART_BUTTONS:
        return None
    buttons = []
    for tc in _sorted_by_confidence(ticker_calls):
        if not _is_actionable(tc):
            continue
        ticker = (tc.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        try:
            url = CHART_URL_TEMPLATE.format(ticker=ticker)
        except (KeyError, IndexError, ValueError):
            return None  # kaputtes Template -> lieber gar keine Buttons als ein Crash
        if not url.startswith(("http://", "https://")):
            continue
        buttons.append({"text": f"📈 {ticker}", "url": url})
        if len(buttons) >= 3:
            break
    return {"inline_keyboard": [buttons]} if buttons else None


def _is_actionable(tc: dict) -> bool:
    """True, wenn dieser Ticker die Praezisions-Schwelle erreicht (konkrete Richtung +
    Konfidenz >= ALERT_MIN_TICKER_CONFIDENCE) und nicht auf der Blockliste steht - dann
    bekommt er im Alert eine Markierung und einen Chart-Button. Muss dieselben Ticker
    meinen wie orchestrator.actionable_tickers (die Alarm-Entscheidung): sonst koennte
    ein geblockter Ticker zwar nie den Alarm ausloesen, aber trotzdem als vermeintliches
    Signal markiert werden, wenn ein ANDERER Ticker den Alert getriggert hat.
    Bei deaktiviertem Filter (Schwelle <= 0) wird nichts markiert (sonst haette jeder
    Ticker die Markierung, was sie wertlos machte)."""
    if ALERT_MIN_TICKER_CONFIDENCE <= 0:
        return False
    if (tc.get("ticker") or "").upper() in BLOCKLIST_TICKERS:
        return False
    return tc.get("direction") in ("long", "short") and (tc.get("confidence") or 0.0) >= ALERT_MIN_TICKER_CONFIDENCE


def _format_age(published_at) -> str:
    """Kompakte Altersangabe ('gerade eben' / 'vor 3 Min' / 'vor 2 Std' / 'vor 4 Tg')
    aus der echten Veroeffentlichungszeit - fuer eine Handelsentscheidung entscheidend,
    ob eine Meldung frisch oder laengst eingepreist ist. Leerer String, wenn kein
    (brauchbarer) Zeitstempel vorliegt (z.B. alte DB-Zeilen)."""
    if not isinstance(published_at, (int, float)) or published_at <= 0:
        return ""
    delta = time.time() - published_at
    if delta < 0:  # kleine Uhr-Abweichung zwischen Quelle und uns
        delta = 0
    minutes = int(delta // 60)
    if minutes < 1:
        return "gerade eben"
    if minutes < 60:
        return f"vor {minutes} Min"
    hours = minutes // 60
    if hours < 24:
        return f"vor {hours} Std"
    return f"vor {hours // 24} Tg"


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
        f"{(tc.get('confidence') or 0.0):.0%}{_ACTIONABLE_MARKER if _is_actionable(tc) else ''}"
        for tc in _sorted_by_confidence(ticker_calls)
    )


def _format_ticker_lines(ticker_calls: list[dict], ticker_change: Optional[dict] = None) -> str:
    """Eine Zeile pro Ticker mit ausgeschriebenem Long/Short und eigener Konfidenz,
    sortiert nach Konfidenz absteigend (sicherste Einschaetzung zuerst). Ticker, die die
    Praezisions-Schwelle erreichen, werden mit einem Stern markiert - so ist auf einen
    Blick klar, welcher Ticker der eigentliche (handelbare) Ausloeser ist und welche nur
    Kontext mit geringerer Sicherheit sind. ticker_change (optional, {TICKER: prozent})
    ergaenzt die heutige Kursbewegung (#8)."""
    if not ticker_calls:
        return "📈 –"
    ticker_change = ticker_change or {}
    lines = []
    for tc in _sorted_by_confidence(ticker_calls):
        ticker = tc.get("ticker") or "?"
        line = (
            f"{_direction_arrow(tc.get('direction'))} {html.escape(ticker)} "
            f"({html.escape(tc.get('direction') or '?')}) – {(tc.get('confidence') or 0.0):.0%}"
        )
        if _is_actionable(tc):
            line += f" {_ACTIONABLE_MARKER}"
        change = ticker_change.get(ticker.upper())
        if isinstance(change, (int, float)):
            line += f" · heute {change:+.1f}%"
        lines.append(line)
    return "\n".join(lines)


def _short_headline(classification: Classification, raw: RawStatement, max_length: int) -> str:
    text = (classification.reasoning or raw.text or "").strip()
    if len(text) <= max_length:
        return text
    cut = text[:max_length].rsplit(" ", 1)[0]
    return (cut or text[:max_length]) + "…"


def _linkable_url(url: Optional[str]) -> Optional[str]:
    """Attribut-escapte URL fuer ein <a href> - oder None, wenn nicht verlinkbar.

    Nur http(s): ein exotisches Schema (z.B. javascript: aus einer manipulierten
    Quelle) soll gar nicht erst im Markup landen; ausserdem lehnt Telegram Nachrichten
    mit unbekannten URL-Protokollen KOMPLETT ab - der Alert waere dann verloren.
    Absurd lange URLs (> 1000 Zeichen; echte Artikel-URLs liegen weit darunter)
    werden verworfen statt verlinkt - sonst muesste das Laengen-Sicherheitsnetz in
    _format_message die Ueberschrift opfern, nur um die URL unterzubringen."""
    if not url or not url.startswith(("http://", "https://")) or len(url) > 1000:
        return None
    return html.escape(url, quote=True)


def _format_message(
    raw: RawStatement, classification: Classification, extras: Optional[dict] = None
) -> str:
    extras = extras or {}
    emoji = SENTIMENT_EMOJI.get(classification.sentiment, "⚪")
    escalation_prefix = (
        "⚠️ "
        if classification.related_topic_id is not None and classification.is_major_escalation
        else ""
    )
    ticker_lines = _format_ticker_lines(classification.ticker_calls, extras.get("ticker_change"))
    url = _linkable_url(raw.url)
    age = _format_age(raw.published_at)
    age_str = f" · 🕒 {age}" if age else ""
    session_str = _session_segment()

    # Feste, quellenunabhaengige Zusatzzeilen (Volatilitaets-Hinweis, Themen-Zeitleiste).
    # _thread_segment escaped den DB-Text selbst; _volatility_flag ist fester Text.
    trailing = ticker_lines
    vol_line = _volatility_flag(raw.text)
    if vol_line:
        trailing += f"\n{vol_line}"
    thread_line = _thread_segment(extras.get("thread"))
    if thread_line:
        trailing += f"\n{thread_line}"

    def assemble(escaped_headline: str) -> str:
        # Anchor wird immer als Ganzes um die (ggf. gekuerzte) Ueberschrift gelegt,
        # nie mitgekuerzt - ein zerrissenes <a>-Tag wuerde Telegram die GESAMTE
        # Nachricht ablehnen lassen. age_str/session_str sind feste, quellenunabhaengige
        # Texte (keine Nutzereingabe) und muessen daher nicht escaped werden.
        inner = f'<a href="{url}">{escaped_headline}</a>' if url else escaped_headline
        return f"{emoji} {classification.confidence:.0%}{age_str}{session_str} — {inner}\n{trailing}"

    # Auf Nutzerwunsch: die Ueberschrift wird NICHT um ihrer selbst willen abgekuerzt
    # (voller Begruendungstext). _short_headline() truncatet nur, wenn der Text den
    # verbleibenden Platz in der Nachricht tatsaechlich sprengen wuerde - ein reines
    # Sicherheitsnetz fuer einen pathologisch langen Begruendungstext, das im
    # Normalfall (System-Prompt verlangt 1-2 Saetze) nie greift, weil max_length dann
    # weit ueber jeder realistischen Textlaenge liegt. +1 fuer die ggf. angehaengte
    # Ellipse; das Escaping-Aufblaehen ("<" -> "&lt;") faengt der Notfall-Schnitt.
    fixed_overhead = len(assemble("")) + len(escalation_prefix)
    max_headline_len = max(50, TELEGRAM_MAX_LENGTH - fixed_overhead - 1)
    headline = html.escape(escalation_prefix + _short_headline(classification, raw, max_headline_len))
    text = assemble(headline)

    if len(text) > TELEGRAM_MAX_LENGTH:
        # Absoluter Notfall (z.B. Begruendungstext voller "<"/"&", die durchs
        # Escaping stark aufblaehen): den bereits escapten Ueberschrift-Text hart
        # kuerzen und den Anchor neu darum bauen. Im Extremfall entsteht dabei ein
        # abgeschnittenes HTML-Entity-Fragment (z.B. "&am" statt "&amp;") - das zeigt
        # Telegram als harmlosen literalen Text, die Tag-Struktur bleibt intakt.
        overflow = len(text) - TELEGRAM_MAX_LENGTH
        headline = headline[:-overflow] if overflow < len(headline) else ""
        text = assemble(headline)
    if len(text) > TELEGRAM_MAX_LENGTH:
        # Selbst mit leerer Ueberschrift zu lang (pathologisch lange URL): Link weg.
        url = None
        text = assemble("")
    return text


async def _send(text: str, retries: int = 2, reply_markup: Optional[dict] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram nicht konfiguriert, ueberspringe Nachricht.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        # Ueberschriften sind jetzt auf den Quellartikel verlinkt - ohne das hier
        # wuerde Telegram unter jeder Nachricht eine grosse Link-Vorschau-Karte
        # anzeigen und die bewusst kompakten Alerts wieder aufblaehen.
        "disable_web_page_preview": True,
    }
    if reply_markup:
        # Inline-Buttons (z.B. Chart-Links) - Telegram erwartet reply_markup als Objekt
        # (httpx json= serialisiert das verschachtelte dict korrekt mit).
        payload["reply_markup"] = reply_markup

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


async def send_alert(
    raw: RawStatement, classification: Classification, extras: Optional[dict] = None
) -> bool:
    text = _format_message(raw, classification, extras=extras)
    markup = _build_chart_markup(classification.ticker_calls)
    return await _send(text, reply_markup=markup)


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
        url = _linkable_url(raw.url)
        if url:
            # Anchor pro ganzer Zeile - die Budget-Schleife unten verwirft nur ganze
            # Zeilen, ein <a>-Tag kann also nie zerrissen werden.
            headline = f'<a href="{url}">{headline}</a>'
        ticker_str = _format_ticker_calls_compact(classification.ticker_calls)
        age = _format_age(raw.published_at)
        age_str = f" · 🕒 {age}" if age else ""
        candidate_lines.append(f"{emoji} {classification.confidence:.0%}{age_str} {headline} — {ticker_str}")

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
