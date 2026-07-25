"""Paper-Trading: ein rein VIRTUELLES Depot (Startkapital PAPER_STARTING_CAPITAL, kein
echtes Geld, kein Broker). Auf jeden tatsaechlich verschickten Alert hin wird fuer die
handelbaren Ticker eine virtuelle Position eroeffnet (Einstiegskurs gemerkt), laufend zum
aktuellen Kurs bewertet und per Telegram gemeldet, "auf wie viel es steht". Stop-Loss/
Take-Profit schliessen automatisch; ein Gegensignal (Long -> Short o.u.) dreht die
Position. Zweck: die Signalguete mit echten Kursen mitverfolgen, ohne echtes Risiko.
Ausdruecklich KEINE Anlageberatung und KEINE Orderausfuehrung.

Aufteilung: die REINEN Rechen-/Formatier-Bausteine (Sizing, PnL, Stop/Ziel-Treffer,
Telegram-Texte) sind ohne Netz/DB testbar; die duennen async-Funktionen darunter
verbinden sie mit Kursabfrage (app/prices.py) und DB (app/db.py).
"""
import asyncio
import html
import logging
import time
from typing import Optional

from app import prices
from app.config import (
    PAPER_CAPITAL_PRESERVATION,
    PAPER_LOSS_STREAK_SIZE_FACTOR,
    PAPER_LOSS_STREAK_THRESHOLD,
    PAPER_MAX_POSITIONS,
    PAPER_MIN_STAKE,
    PAPER_STARTING_CAPITAL,
    PAPER_STATUS_INTERVAL_MINUTES,
)
from app.db import (
    close_paper_position,
    count_open_paper_positions,
    get_consecutive_paper_losses,
    get_meta,
    get_open_paper_position_for_ticker,
    get_open_paper_positions,
    get_open_paper_stake_sum,
    get_paper_closed_stats,
    get_paper_realized_pnl,
    insert_paper_position,
    set_meta,
)
from app.market_hours import session_label, us_market_session

logger = logging.getLogger(__name__)

_STATUS_META_KEY = "paper_status_last_sent"


# --- Reine Bausteine (ohne Netz/DB) ------------------------------------------------

def position_fraction(score: Optional[int], loss_streak: int = 0) -> float:
    """Anteil des Depotwerts, den der Bot je Position einsetzt - abgeleitet aus dem
    Ueberzeugungs-Score (0-100). Das ist die "Taktik, die er selber macht": ueberzeugtere
    Signale bekommen mehr Kapital, schwaechere weniger. Bewusst gedeckelt, damit nie das
    ganze Depot auf einer Position steht. None (Score aus) -> mittlerer Anteil.

    Kapitalerhalt-Modus (#Kapitalerhalt): laeuft eine Verlustserie (loss_streak >=
    PAPER_LOSS_STREAK_THRESHOLD geschlossene Verlust-Trades IN FOLGE), wird der Anteil
    zusaetzlich um PAPER_LOSS_STREAK_SIZE_FACTOR verkleinert - Risk-off nach einer
    Pechstraehne, statt unveraendert weiterzumachen."""
    if score is None:
        base = 0.16
    elif score >= 85:
        base = 0.30
    elif score >= 70:
        base = 0.22
    elif score >= 50:
        base = 0.16
    else:
        base = 0.12
    if PAPER_CAPITAL_PRESERVATION and loss_streak >= PAPER_LOSS_STREAK_THRESHOLD:
        base *= PAPER_LOSS_STREAK_SIZE_FACTOR
    return base


def compute_position(
    account_value: float,
    free_cash: float,
    score: Optional[int],
    entry_price: Optional[float],
    direction: Optional[str],
    day_high: Optional[float] = None,
    day_low: Optional[float] = None,
    loss_streak: int = 0,
) -> Optional[dict]:
    """Bestimmt Einsatz, Stueckzahl und Stop/Ziel fuer eine neue virtuelle Position.
    account_value = aktueller Depotwert (Basis fuers Sizing), free_cash = freier
    Barbestand (Obergrenze - man kann nicht mehr binden, als frei ist). Gibt None zurueck,
    wenn kein gueltiger Kurs/Richtung vorliegt oder der Einsatz unter PAPER_MIN_STAKE
    faellt (kein sinnloser Dust-Trade). loss_streak siehe position_fraction
    (Kapitalerhalt-Modus)."""
    if not isinstance(entry_price, (int, float)) or entry_price <= 0:
        return None
    if direction not in ("long", "short"):
        return None
    stake = min(free_cash, account_value * position_fraction(score, loss_streak))
    if stake < PAPER_MIN_STAKE:
        return None
    qty = stake / entry_price
    levels = prices.suggest_risk_levels(entry_price, direction, day_high, day_low) or {}
    return {
        "stake": round(stake, 2),
        "qty": qty,
        "stop": levels.get("stop"),
        "target": levels.get("target"),
    }


def unrealized_pnl(direction: str, entry_price: float, qty: float, current_price: float) -> float:
    """Bisher nicht realisierter Gewinn/Verlust (EUR) einer offenen Position beim
    aktuellen Kurs. Long profitiert von steigendem, Short von fallendem Kurs."""
    if direction == "short":
        return (entry_price - current_price) * qty
    return (current_price - entry_price) * qty


def return_pct(direction: str, entry_price: float, current_price: float) -> float:
    """Rendite der Position in Prozent (richtungsbereinigt: fuer Short ist ein fallender
    Kurs positiv). Bezugsgroesse ist der Einstiegskurs."""
    if entry_price <= 0:
        return 0.0
    raw = (current_price - entry_price) / entry_price * 100.0
    return -raw if direction == "short" else raw


def stop_or_target_hit(
    direction: str, current_price: float, stop: Optional[float], target: Optional[float]
) -> Optional[str]:
    """Prueft, ob der aktuelle Kurs Stop-Loss oder Take-Profit erreicht hat. Gibt 'stop',
    'target' oder None zurueck. Stop wird bei Gleichstand vor Ziel geprueft (Risiko
    zuerst)."""
    if direction == "long":
        if stop is not None and current_price <= stop:
            return "stop"
        if target is not None and current_price >= target:
            return "target"
    elif direction == "short":
        if stop is not None and current_price >= stop:
            return "stop"
        if target is not None and current_price <= target:
            return "target"
    return None


_CLOSE_REASON_LABEL = {
    "stop": "Stop-Loss",
    "target": "Ziel erreicht",
    "reversal": "Gegensignal",
    "manual": "manuell",
}


def _arrow(direction: str) -> str:
    return "🔺" if direction == "long" else "🔻" if direction == "short" else "◽"


def _eur(value: float) -> str:
    return f"{value:+.2f}€"


def _t(pos: dict) -> str:
    """HTML-sichere Ticker-Anzeige (Ticker sind Kuerzel, trotzdem defensiv escaped)."""
    return html.escape(str(pos.get("ticker", "?")))


def format_open_message(
    pos: dict, account_value_after: float, free_cash_after: float, session: Optional[str] = None
) -> str:
    """session (siehe app.market_hours.us_market_session/session_label): macht transparent,
    in welcher Boersen-Phase der als 'Kaufpreis' gemerkte Kurs abgefragt wurde - der Bot
    merkt sich immer nur den TATSAECHLICHEN aktuellen Kurs zum Alarm-Zeitpunkt (nie einen
    idealisierten/nachtraeglich korrigierten Wert). Ist das ausserhalb der reguraeren
    Session (z.B. vorboerslich), ist das best-effort ueber Stooq - der angezeigte Kurs
    kann dann der zuletzt gehandelte (moeglicherweise etwas verzoegerte) Kurs sein."""
    direction = pos["direction"]
    session_str = f" ({session_label(session)})" if session and session_label(session) else ""
    lines = [
        f"🟢 <b>Paper-Position eröffnet: {_t(pos)} {html.escape(direction)}</b> {_arrow(direction)}",
        f"Einstieg {pos['entry_price']:g}{session_str} · {pos['qty']:.4g} Stück · Einsatz {pos['stake']:.2f}€",
    ]
    if pos.get("stop") is not None and pos.get("target") is not None:
        lines.append(f"🛡 SL {pos['stop']:g} / 🎯 TP {pos['target']:g}")
    lines.append(f"💼 Depotwert {account_value_after:.2f}€ · frei {free_cash_after:.2f}€")
    return "\n".join(lines)


def format_close_message(
    pos: dict, exit_price: float, pnl: float, ret_pct: float, reason: str,
    account_value_after: float,
) -> str:
    direction = pos["direction"]
    reason_label = _CLOSE_REASON_LABEL.get(reason, reason)
    emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
    return (
        f"{emoji} <b>Paper-Position geschlossen: {_t(pos)} {html.escape(direction)}</b> "
        f"({html.escape(reason_label)})\n"
        f"Einstieg {pos['entry_price']:g} → Ausstieg {exit_price:g} ({ret_pct:+.1f}%)\n"
        f"Ergebnis: {_eur(pnl)} · Depot jetzt {account_value_after:.2f}€"
    )


def format_status_message(summary: dict) -> str:
    """Depot-Uebersicht ("auf wie viel steht alles"): Gesamtwert, freier Bestand und je
    offene Position der aktuelle Kurs samt (noch nicht realisiertem) Gewinn/Verlust."""
    start = summary["starting_capital"]
    equity = summary["equity"]
    total_ret = (equity - start) / start * 100.0 if start else 0.0
    lines = [
        f"📄 <b>Paper-Depot</b> (Start {start:.0f}€)",
        f"💼 Wert {equity:.2f}€ ({total_ret:+.1f}%) · frei {summary['free_cash']:.2f}€",
    ]
    positions = summary["positions"]
    if positions:
        lines.append(f"Offene Positionen ({len(positions)}):")
        for p in positions:
            cur = p.get("current_price")
            if cur is None:
                lines.append(
                    f"{_arrow(p['direction'])} {html.escape(str(p['ticker']))} "
                    f"{html.escape(p['direction'])} @ {p['entry_price']:g} → (Kurs n/a)"
                )
                continue
            lines.append(
                f"{_arrow(p['direction'])} {html.escape(str(p['ticker']))} "
                f"{html.escape(p['direction'])} @ {p['entry_price']:g} → {cur:g} "
                f"({p['return_pct']:+.1f}%, {_eur(p['unrealized_pnl'])})"
            )
    else:
        lines.append("Keine offenen Positionen.")
    closed = summary.get("closed", 0)
    if closed:
        wins = summary.get("wins", 0)
        lines.append(
            f"📊 realisiert {_eur(summary['realized_pnl'])} · "
            f"{wins}/{closed} Trades im Plus"
        )
    if summary.get("capital_preservation_active"):
        lines.append(
            f"🛡 Kapitalerhalt-Modus aktiv ({summary['loss_streak']} Verluste in Folge) "
            "– Positionsgröße reduziert"
        )
    return "\n".join(lines)


# --- Kontostand ---------------------------------------------------------------------

def account_snapshot() -> dict:
    """Barbestand-Sicht OHNE aktuelle Kurse (fuers Sizing): account_value = Startkapital +
    realisierte Ergebnisse (Cash-Basis), free_cash = davon abzueglich des in offenen
    Positionen gebundenen Einsatzes."""
    realized = get_paper_realized_pnl()
    open_stake = get_open_paper_stake_sum()
    account_value = PAPER_STARTING_CAPITAL + realized
    return {
        "account_value": account_value,
        "free_cash": account_value - open_stake,
        "realized_pnl": realized,
        "open_stake": open_stake,
    }


# --- Async: Eroeffnen / Verwalten / Melden -----------------------------------------

async def _close(pos: dict, exit_price: float, reason: str, notify: bool) -> float:
    """Schliesst eine Position best-effort und meldet sie (falls notify). Gibt den
    realisierten PnL zurueck."""
    pnl = unrealized_pnl(pos["direction"], pos["entry_price"], pos["qty"], exit_price)
    ret = return_pct(pos["direction"], pos["entry_price"], exit_price)
    close_paper_position(pos["id"], exit_price, time.time(), pnl, reason)
    if notify:
        snap = account_snapshot()
        from app.telegram_alert import send_text
        await send_text(format_close_message(pos, exit_price, pnl, ret, reason, snap["account_value"]))
    return pnl


async def open_positions_for_alert(classification, statement_id, score: Optional[int]) -> None:
    """Eroeffnet fuer die handelbaren Ticker eines gerade verschickten Alerts virtuelle
    Positionen. Laeuft schon eine Position auf demselben Ticker in GEGEN-Richtung, wird
    sie zuerst geschlossen (Signal-Umkehr); in GLEICHER Richtung wird nicht doppelt
    eroeffnet. Best-effort: ohne erreichbaren Kurs entfaellt das Eroeffnen still."""
    # Late import, um einen Import-Zyklus (orchestrator -> paper_trading -> orchestrator)
    # zu vermeiden - actionable_tickers lebt im orchestrator.
    from app.orchestrator import actionable_tickers
    from app.telegram_alert import send_text

    loss_streak = get_consecutive_paper_losses() if PAPER_CAPITAL_PRESERVATION else 0
    # Einmal pro Alert-Batch bestimmt (nicht je Ticker) - die Session aendert sich nicht
    # innerhalb weniger Sekunden. Rein informativ fuer die Telegram-Meldung (#Kaufpreis-
    # Transparenz): macht sichtbar, ob der gemerkte Kurs aus der regulaeren Session oder
    # best-effort ausserhalb (vor-/nachboerslich) stammt.
    session = us_market_session()

    for tc in actionable_tickers(classification):
        ticker = (tc.get("ticker") or "").upper()
        direction = tc.get("direction")
        if direction not in ("long", "short") or not ticker:
            continue

        quote = await prices.get_quote(ticker)
        if not quote or not quote.get("price"):
            logger.debug("[paper] kein Kurs fuer %s - Position wird nicht eroeffnet.", ticker)
            continue
        price = quote["price"]

        existing = get_open_paper_position_for_ticker(ticker)
        if existing is not None:
            if existing["direction"] == direction:
                continue  # gleiche Richtung laeuft schon - nicht aufstocken
            # Gegensignal: alte Position glattstellen, dann neu in Gegenrichtung eroeffnen.
            await _close(existing, price, "reversal", notify=True)

        if count_open_paper_positions() >= PAPER_MAX_POSITIONS:
            logger.info("[paper] Positionslimit (%d) erreicht - kein neuer Trade fuer %s.",
                        PAPER_MAX_POSITIONS, ticker)
            continue

        snap = account_snapshot()
        plan = compute_position(
            snap["account_value"], snap["free_cash"], score, price, direction,
            quote.get("high"), quote.get("low"), loss_streak,
        )
        if plan is None:
            logger.info("[paper] kein Einsatz mehr frei fuer %s (frei %.2f€).",
                        ticker, snap["free_cash"])
            continue

        insert_paper_position(
            statement_id, ticker, direction, price, time.time(),
            plan["qty"], plan["stake"], plan["stop"], plan["target"],
        )
        after = account_snapshot()
        pos = {"ticker": ticker, "direction": direction, "entry_price": price, **plan}
        await send_text(
            format_open_message(pos, after["account_value"], after["free_cash"], session=session)
        )


async def manage_open_positions() -> None:
    """Bewertet alle offenen Positionen zum aktuellen Kurs, schliesst automatisch die,
    die Stop-Loss oder Take-Profit erreicht haben (mit Sofort-Meldung), und schickt -
    hoechstens alle PAPER_STATUS_INTERVAL_MINUTES - einen Depot-Status. Best-effort: ist
    der Kursdienst nicht erreichbar, passiert nichts (keine Fehlmeldung)."""
    positions = get_open_paper_positions()
    if not positions:
        return

    quotes = await asyncio.gather(*(prices.get_quote(p["ticker"]) for p in positions))

    still_open: list[dict] = []
    for pos, quote in zip(positions, quotes):
        price = quote.get("price") if quote else None
        if price is None:
            still_open.append({**pos, "current_price": None})
            continue
        hit = stop_or_target_hit(pos["direction"], price, pos.get("stop"), pos.get("target"))
        if hit:
            await _close(pos, price, hit, notify=True)
            continue
        still_open.append({
            **pos,
            "current_price": price,
            "unrealized_pnl": unrealized_pnl(pos["direction"], pos["entry_price"], pos["qty"], price),
            "return_pct": return_pct(pos["direction"], pos["entry_price"], price),
        })

    await _maybe_send_status(still_open)


def _status_is_due(now: float) -> bool:
    if PAPER_STATUS_INTERVAL_MINUTES <= 0:
        return True
    last = get_meta(_STATUS_META_KEY)
    if last is None:
        return True
    try:
        last_ts = float(last)
    except (TypeError, ValueError):
        return True
    return (now - last_ts) >= PAPER_STATUS_INTERVAL_MINUTES * 60


async def _maybe_send_status(open_positions: list[dict]) -> None:
    """Schickt den Depot-Status, sofern das Intervall seit dem letzten Status abgelaufen
    ist. Eroeffnungen/Schliessungen melden sich unabhaengig davon sofort selbst."""
    if not open_positions:
        return
    now = time.time()
    if not _status_is_due(now):
        return
    from app.telegram_alert import send_text
    closed = get_paper_closed_stats()
    snap = account_snapshot()
    unreal = sum(p.get("unrealized_pnl") or 0.0 for p in open_positions)
    loss_streak = get_consecutive_paper_losses() if PAPER_CAPITAL_PRESERVATION else 0
    summary = {
        "starting_capital": PAPER_STARTING_CAPITAL,
        "free_cash": snap["free_cash"],
        "equity": snap["account_value"] + unreal,
        "realized_pnl": snap["realized_pnl"],
        "positions": open_positions,
        "closed": closed["closed"],
        "wins": closed["wins"],
        "loss_streak": loss_streak,
        "capital_preservation_active": (
            PAPER_CAPITAL_PRESERVATION and loss_streak >= PAPER_LOSS_STREAK_THRESHOLD
        ),
    }
    sent = await send_text(format_status_message(summary))
    if sent:
        set_meta(_STATUS_META_KEY, str(now))
