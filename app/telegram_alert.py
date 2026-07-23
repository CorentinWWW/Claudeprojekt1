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
from app.scoring import format_expected_move, high_volatility_text

logger = logging.getLogger(__name__)

SENTIMENT_EMOJI = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}
TELEGRAM_MAX_LENGTH = 4096
DIGEST_MAX_DETAIL_LINES = 15
# Markiert im Alert die Ticker, die die Praezisions-Schwelle (ALERT_MIN_TICKER_CONFIDENCE)
# tatsaechlich erreichen - also die eigentlich handelbaren, hochsicheren Signale, im
# Gegensatz zu evtl. mitgelisteten Kontext-Tickern mit geringerer Konfidenz.
_ACTIONABLE_MARKER = "⭐"


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
    if high_volatility_text(text):
        return "⚡ Hohe Volatilität – Richtung evtl. unsicher (ggf. Straddle)"
    return ""


_TECH_EMOJI = {
    "Strong Buy": "🟢🟢", "Buy": "🟢", "Neutral": "⚪",
    "Sell": "🔴", "Strong Sell": "🔴🔴",
}


def _technical_segment(technical: Optional[dict]) -> str:
    """Kompakte technische Gesamtbewertung (TradingView-Stil) je handelbarem Ticker:
    Rating, MA/Oszillator-Zaehlung, ein paar Kennzahlen (RSI/MACD/ADX) und ob die Technik
    das Signal bestaetigt oder ihm widerspricht. Leer, wenn keine Technik vorliegt."""
    if not technical or not isinstance(technical, dict):
        return ""
    lines = []
    for ticker, summ in technical.items():
        if not isinstance(summ, dict):
            continue
        label = summ.get("label") or "Neutral"
        emoji = _TECH_EMOJI.get(label, "⚪")
        ma = summ.get("ma") or {}
        osc = summ.get("oscillators") or {}
        vals = summ.get("values") or {}
        parts = [
            f"{emoji} <b>{html.escape(str(ticker))}</b> Technik: {html.escape(label)}",
            f"(MA {ma.get('buy', 0)}▲/{ma.get('sell', 0)}▼, "
            f"Osz {osc.get('buy', 0)}▲/{osc.get('sell', 0)}▼)",
        ]
        detail = []
        rsi_v = vals.get("rsi")
        if isinstance(rsi_v, (int, float)):
            detail.append(f"RSI {rsi_v:.0f}")
        macd_h = vals.get("macd_hist")
        if isinstance(macd_h, (int, float)):
            detail.append(f"MACD{'▲' if macd_h > 0 else '▼'}")
        adx_v = vals.get("adx")
        if isinstance(adx_v, (int, float)):
            detail.append(f"ADX {adx_v:.0f}")
        line = "📊 " + " ".join(parts)
        if detail:
            line += " · " + " ".join(detail)
        agrees = summ.get("agrees")
        if agrees is True:
            line += " · ✅ bestätigt Signal"
        elif agrees is False:
            line += " · ⚠️ widerspricht Signal"
        lines.append(line)
    return "\n".join(lines)


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


def _safe_ticker(tc: dict, default: str = "?") -> str:
    """Robuste Ticker-String-Extraktion aus einem ticker_calls-Eintrag: liefert IMMER
    einen String (default, falls das Feld fehlt, None ist, oder - z.B. bei einer alten/
    fremden DB-Zeile - keinen String enthaelt, etwa eine Zahl). '.get(key, default)'
    allein faengt nur eine FEHLENDE Taste ab, kein explizites None und keinen falschen
    Typ - beides wuerde .upper()/.strip()/html.escape() sonst mit einem
    AttributeError/TypeError zum Absturz bringen."""
    val = tc.get("ticker")
    return val if isinstance(val, str) and val else default


def _build_chart_markup(ticker_calls: list[dict]) -> Optional[dict]:
    """Inline-Keyboard mit Chart-Links (#9) fuer die handelbaren Ticker (max. 3),
    sortiert nach Konfidenz. None, wenn deaktiviert, das URL-Template kaputt ist oder
    kein handelbarer Ticker uebrig bleibt."""
    if not ENABLE_CHART_BUTTONS:
        return None
    if "{ticker}" not in CHART_URL_TEMPLATE:
        # str.format() ignoriert ueberzaehlige/fehlende Platzhalter still (kein
        # KeyError) - ohne diese explizite Pruefung wuerde ein CHART_URL_TEMPLATE ohne
        # {ticker} (z.B. eine falsch kopierte feste URL) fuer JEDEN Ticker denselben
        # Button erzeugen, ohne jede Fehlermeldung.
        logger.warning(
            "CHART_URL_TEMPLATE enthaelt keinen {ticker}-Platzhalter - Chart-Buttons "
            "werden uebersprungen: %s", CHART_URL_TEMPLATE,
        )
        return None
    buttons = []
    for tc in _sorted_by_confidence(ticker_calls):
        if not _has_clear_direction_and_confidence(tc):
            continue
        ticker = _safe_ticker(tc, "").strip().upper()
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


def _has_clear_direction_and_confidence(tc: dict) -> bool:
    """True, wenn dieser Ticker eine klare Long/Short-Richtung hat, nicht auf der
    Blockliste steht, UND (falls der Praezisions-Filter aktiv ist) dessen Konfidenz
    erreicht. Deckt sich bewusst mit orchestrator.actionable_tickers's Semantik
    (inklusive: bei deaktiviertem Filter zaehlt jeder Ticker mit klarer Richtung) -
    im Unterschied zu _is_actionable() (Stern-Markierung), die bei deaktiviertem
    Filter IMMER False liefert. Fuer Chart-Buttons gibt es keinen Grund, sie bei
    deaktiviertem Filter komplett abzuschalten - anders als beim Stern (der sonst
    bedeutungslos waere, weil er dann jeden Ticker markieren wuerde) ist ein Chart-
    Link fuer jeden Ticker mit klarer Richtung weiterhin sinnvoll und nicht irrefuehrend."""
    if _safe_ticker(tc, "").upper() in BLOCKLIST_TICKERS:
        return False
    if tc.get("direction") not in ("long", "short"):
        return False
    if ALERT_MIN_TICKER_CONFIDENCE <= 0:
        return True
    return (tc.get("confidence") or 0.0) >= ALERT_MIN_TICKER_CONFIDENCE


def _is_actionable(tc: dict) -> bool:
    """True, wenn dieser Ticker die Praezisions-Schwelle erreicht (konkrete Richtung +
    Konfidenz >= ALERT_MIN_TICKER_CONFIDENCE) und nicht auf der Blockliste steht - dann
    bekommt er im Alert eine STERN-Markierung. Bei deaktiviertem Filter (Schwelle <= 0)
    wird bewusst NIE markiert (sonst haette jeder Ticker die Markierung, was sie
    wertlos machte) - siehe _has_clear_direction_and_confidence() fuer die Chart-
    Button-Eignung, die bei deaktiviertem Filter NICHT auf 'nie' faellt."""
    if ALERT_MIN_TICKER_CONFIDENCE <= 0:
        return False
    if _safe_ticker(tc, "").upper() in BLOCKLIST_TICKERS:
        return False
    return tc.get("direction") in ("long", "short") and (tc.get("confidence") or 0.0) >= ALERT_MIN_TICKER_CONFIDENCE


def _visible_ticker_calls(ticker_calls: list[dict]) -> list[dict]:
    """Entfernt Ticker auf der Blockliste aus der ANZEIGE - unabhaengig davon, ob der
    Praezisions-Filter (ALERT_MIN_TICKER_CONFIDENCE) aktiv ist. BLOCKLIST_TICKERS
    bedeutet 'diese Ticker nie melden', nicht nur 'nie als Alarm-Ausloeser verwenden'.
    Ohne diesen Filter wuerde ein geblockter Ticker trotzdem als normale (nur nicht mit
    Stern markierte) Zeile in jedem Alert auftauchen, der aus einem ANDEREN Grund
    feuert (z.B. ein zweiter, nicht geblockter Ticker, oder - bei deaktiviertem
    Praezisions-Filter - jede marktrelevante Meldung unabhaengig von Tickern)."""
    return [
        tc for tc in (ticker_calls or [])
        if isinstance(tc, dict) and _safe_ticker(tc, "").upper() not in BLOCKLIST_TICKERS
    ]


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


# Harte Obergrenze fuer die Anzahl gerenderter Ticker-Zeilen: das Claude-Tool-Schema
# begrenzt die Laenge von ticker_calls nicht (kein maxItems), und das bestehende
# Laengen-Sicherheitsnetz in _format_message kuerzt nur Ueberschrift/URL, nie die
# Ticker-Zeilen selbst - eine sehr lange Liste koennte die Nachricht sonst ueber
# TELEGRAM_MAX_LENGTH aufblaehen und den GESAMTEN Alert verlieren (Telegram lehnt die
# komplette Nachricht ab). Die Liste ist vorher schon nach Konfidenz absteigend
# sortiert, die wichtigsten/sichersten Ticker bleiben also in jedem Fall erhalten.
_MAX_TICKER_LINES = 12


def _format_ticker_calls_compact(ticker_calls: list[dict]) -> str:
    if not ticker_calls:
        return "–"
    sorted_calls = _sorted_by_confidence(ticker_calls)
    overflow = max(0, len(sorted_calls) - _MAX_TICKER_LINES)
    parts = [
        f"{html.escape(_safe_ticker(tc))}{_direction_arrow(tc.get('direction'))}"
        f"{(tc.get('confidence') or 0.0):.0%}{_ACTIONABLE_MARKER if _is_actionable(tc) else ''}"
        for tc in sorted_calls[:_MAX_TICKER_LINES]
    ]
    if overflow:
        parts.append(f"+{overflow} weitere")
    return ", ".join(parts)


def _format_ticker_lines(
    ticker_calls: list[dict],
    ticker_change: Optional[dict] = None,
    ticker_risk: Optional[dict] = None,
    ticker_hitrate: Optional[dict] = None,
) -> str:
    """Eine Zeile pro Ticker mit ausgeschriebenem Long/Short und eigener Konfidenz,
    sortiert nach Konfidenz absteigend (sicherste Einschaetzung zuerst). Ticker, die die
    Praezisions-Schwelle erreichen, werden mit einem Stern markiert - so ist auf einen
    Blick klar, welcher Ticker der eigentliche (handelbare) Ausloeser ist und welche nur
    Kontext mit geringerer Sicherheit sind. ticker_change (#8), ticker_risk (Stop/Ziel,
    #5) und ticker_hitrate (historische Trefferquote, #9) sind optionale {TICKER: ...}-
    Dicts, die die jeweilige Zeile ergaenzen. Zeigt hoechstens _MAX_TICKER_LINES Zeilen."""
    if not ticker_calls:
        return "📈 –"
    ticker_change = ticker_change or {}
    ticker_risk = ticker_risk or {}
    ticker_hitrate = ticker_hitrate or {}
    sorted_calls = _sorted_by_confidence(ticker_calls)
    overflow = max(0, len(sorted_calls) - _MAX_TICKER_LINES)
    lines = []
    for tc in sorted_calls[:_MAX_TICKER_LINES]:
        ticker = _safe_ticker(tc)
        key = ticker.upper()
        line = (
            f"{_direction_arrow(tc.get('direction'))} {html.escape(ticker)} "
            f"({html.escape(tc.get('direction') or '?')}) – {(tc.get('confidence') or 0.0):.0%}"
        )
        if _is_actionable(tc):
            line += f" {_ACTIONABLE_MARKER}"
        change = ticker_change.get(key)
        if isinstance(change, (int, float)):
            line += f" · heute {change:+.1f}%"
        risk = ticker_risk.get(key)
        if isinstance(risk, dict) and risk.get("stop") is not None and risk.get("target") is not None:
            line += f"\n   🛡 SL {risk['stop']:g} / 🎯 TP {risk['target']:g}"
        hr = ticker_hitrate.get(key)
        if isinstance(hr, dict) and hr.get("n"):
            line += f"\n   📊 bisher {hr['hits']}/{hr['n']} richtig ({(hr['hit_rate']):.0%})"
        lines.append(line)
    if overflow:
        lines.append(f"… +{overflow} weitere Ticker")
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
    ticker_lines = _format_ticker_lines(
        _visible_ticker_calls(classification.ticker_calls),
        extras.get("ticker_change"),
        extras.get("ticker_risk"),
        extras.get("ticker_hitrate"),
    )
    url = _linkable_url(raw.url)
    age = _format_age(raw.published_at)
    age_str = f" · 🕒 {age}" if age else ""
    session_str = _session_segment()

    # Feste, quellenunabhaengige Zusatzzeilen. _thread_segment escaped den DB-Text
    # selbst; alle uebrigen sind fester Text bzw. Zahlen (keine Nutzereingabe).
    trailing = ticker_lines

    # Ueberzeugungs-Score + grobe Positionsgroessen-Einordnung (#1/#4).
    conviction = extras.get("conviction")
    if isinstance(conviction, int):
        tier = extras.get("position_tier") or ""
        tier_str = f" · {tier}" if tier else ""
        trailing += f"\n🎯 Überzeugung {conviction}/100{tier_str}"

    # Erwartete Bewegung + Horizont (#3) - direkt aus der Classification.
    move_str = format_expected_move(
        classification.expected_move_pct, classification.expected_horizon
    )
    if move_str:
        trailing += f"\n📐 erwartete Bewegung {move_str}"

    corroboration = extras.get("corroboration")
    if isinstance(corroboration, int) and corroboration > 1:
        trailing += f"\n✅ bestätigt durch {corroboration} Quellen"

    # Grober, unverbindlicher Positionsanteil (Half-Kelly, #5).
    kelly = extras.get("kelly_fraction")
    if isinstance(kelly, (int, float)) and kelly > 0:
        trailing += f"\n💰 Positionsanteil (Half-Kelly, unverbindlich) ~{kelly * 100:.0f}% des Einsatzkapitals"

    # Richtungswechsel gegenueber dem letzten Alert fuer denselben Ticker (#2).
    flip = extras.get("direction_flip")
    if isinstance(flip, str) and flip:
        trailing += f"\n⟳ Richtungswechsel ggü. letztem Alert (vorher {html.escape(flip)})"

    # Sektor-Cluster (#4): mehrere Werte einer Branche zuletzt alarmiert.
    cluster = extras.get("sector_cluster")
    if isinstance(cluster, dict) and cluster.get("sector") and cluster.get("count"):
        trailing += (
            f"\n🧩 Sektor-Cluster: {html.escape(str(cluster['sector']))} "
            f"({cluster['count']} Meldungen zuletzt)"
        )

    # Kurs laeuft bereits gegen die These (#3).
    divergence = extras.get("divergence")
    if isinstance(divergence, str) and divergence:
        trailing += f"\n⚠️ Kurs läuft bereits gegen die These: {html.escape(divergence)}"

    # Signal ist spaet dran - Kurs heute schon stark in Signalrichtung gelaufen.
    late_move = extras.get("late_move")
    if isinstance(late_move, (int, float)):
        trailing += (
            f"\n⏰ Spät dran – Kurs heute schon {late_move:+.1f}% in Signalrichtung "
            "(Bewegung evtl. großteils gelaufen)"
        )

    # Uebernacht-/Vorboersen-Gap-Antizipation: frueh sagen, dass der Kurs zum naechsten
    # Open gappen duerfte - bevor es vorboerslich schon hochschiesst.
    gap = extras.get("overnight_gap")
    if isinstance(gap, dict) and gap.get("gap_direction") in ("up", "down"):
        phase = html.escape(str(gap.get("phase", "")))
        if gap.get("gap_direction") == "up":
            if gap.get("early"):
                trailing += (
                    f"\n🚀 Mögliche Übernacht-Rallye: Katalysator {phase} – der Markt kann "
                    "kaum noch reagieren. Einstieg jetzt, bevor der Kurs zum nächsten Open "
                    "hochgappt (bevor es vorbörslich schon läuft)."
                )
            else:
                trailing += (
                    f"\n🚀 Vorbörslicher Anstieg möglich ({phase}) – der Gap läuft evtl. "
                    "schon; prüfen, ob noch ein Einstieg lohnt."
                )
        else:
            if gap.get("early"):
                trailing += (
                    f"\n📉 Mögliche Übernacht-Lücke nach unten: Katalysator {phase} – "
                    "Reaktion dürfte als Gap zum nächsten Open kommen."
                )
            else:
                trailing += (
                    f"\n📉 Vorbörslicher Rückgang möglich ({phase}) – die Lücke läuft evtl. "
                    "schon."
                )

    # Gap-Chase-Bewertung: Gegenstueck zur obigen Antizipation - der Gap ist bereits
    # passiert (Markt war zu, jetzt zur Boersenoeffnung extrem hoch/niedrig gegappt).
    # Lohnt sich ein Einstieg noch, und falls ja, wann verkaufen?
    chase = extras.get("gap_chase")
    if isinstance(chase, dict) and isinstance(chase.get("gap_pct"), (int, float)):
        ticker = html.escape(str(chase.get("ticker") or "")).strip()
        ticker_part = f"{ticker}: " if ticker else ""
        gap_val = chase["gap_pct"]
        if chase.get("too_late"):
            trailing += (
                f"\n⏭ {ticker_part}bereits {gap_val:+.1f}% über Nacht/vorbörslich "
                "gegappt – Großteil der erwarteten Bewegung dürfte schon gelaufen sein. "
                "Jetzt noch einsteigen ist riskant (Gap-Fade-Gefahr) – eher abwarten/"
                "auf einen Pullback warten."
            )
        else:
            target = chase.get("target_price")
            target_str = f" · Ausstieg ~{target:g}" if isinstance(target, (int, float)) else ""
            trailing += (
                f"\n🎯 {ticker_part}bereits {gap_val:+.1f}% gegappt, aber noch nicht "
                f"ausgereizt – Einstieg kann sich noch lohnen{target_str}."
            )

    # Technische Gesamtbewertung (TradingView-Stil) je handelbarem Ticker.
    tech_line = _technical_segment(extras.get("technical"))
    if tech_line:
        trailing += f"\n{tech_line}"

    if extras.get("hedged"):
        trailing += "\n🗣 unbestätigt/Gerücht – mit Vorsicht behandeln"

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
    markup = _build_chart_markup(_visible_ticker_calls(classification.ticker_calls))
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


def format_weekly_digest(stats: dict) -> str:
    """Baut die woechentliche Performance-Zusammenfassung (#10) als Telegram-Text:
    Gesamt-Trefferquote/Durchschnittsrendite plus die besten/schlechtesten Ticker nach
    mittlerer Rendite. Alle Werte stammen aus der DB (Ticker sind Kuerzel, trotzdem
    defensiv escaped)."""
    evaluated = stats.get("evaluated") or 0
    hits = stats.get("hits") or 0
    hit_rate = stats.get("hit_rate")
    avg_return = stats.get("avg_return_pct")
    hr_str = f"{hit_rate:.0%}" if isinstance(hit_rate, (int, float)) else "–"
    avg_str = f"{avg_return:+.2f}%" if isinstance(avg_return, (int, float)) else "–"
    lines = [
        "📅 <b>Wochen-Performance</b>",
        f"Ausgewertete Signale: {evaluated} · Treffer: {hits}/{evaluated} ({hr_str})",
        f"Ø Rendite pro Signal: {avg_str}",
    ]

    def _ticker_line(t: dict) -> str:
        ticker = html.escape(str(t.get("ticker", "?")))
        n = t.get("n") or 0
        tr = t.get("hit_rate")
        ar = t.get("avg_return_pct")
        tr_str = f"{tr:.0%}" if isinstance(tr, (int, float)) else "–"
        ar_str = f"{ar:+.2f}%" if isinstance(ar, (int, float)) else "–"
        return f"  {ticker}: {ar_str} Ø · {tr_str} Treffer (n={n})"

    best = stats.get("best_tickers") or []
    worst = stats.get("worst_tickers") or []
    if best:
        lines.append("🟢 <b>Beste</b>")
        lines.extend(_ticker_line(t) for t in best)
    if worst:
        lines.append("🔴 <b>Schwächste</b>")
        lines.extend(_ticker_line(t) for t in worst)

    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_LENGTH:
        text = text[:TELEGRAM_MAX_LENGTH]
    return text


async def send_weekly_digest(stats: dict) -> bool:
    """Verschickt die woechentliche Performance-Zusammenfassung (#10)."""
    return await _send(format_weekly_digest(stats))


async def send_text(text: str) -> bool:
    return await _send(text)


async def send_startup_notice(active_sources: list[str]) -> None:
    sources_str = ", ".join(active_sources) if active_sources else "keine"
    text = (
        "🟢 <b>Market Impact Predictor gestartet</b>\n"
        f"Aktive Quellen: {html.escape(sources_str)}\n"
        "Du bekommst hier ab jetzt Alerts bei marktrelevanten Aussagen."
    )
    sent = await send_text(text)
    if not sent and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        logger.warning(
            "Start-Heartbeat konnte nicht an Telegram gesendet werden - "
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID pruefen."
        )
