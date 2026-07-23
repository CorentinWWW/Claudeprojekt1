"""US-Boersen-Handelszeiten (US-Ostkueste, inkl. Sommer-/Winterzeit) - fuer die
Einordnung im Alert, ob ein Signal gerade ueberhaupt handelbar ist.

Regulaerer Handel 9:30-16:00 ET, vorboerslich 4:00-9:30, nachboerslich 16:00-20:00,
Wochenende geschlossen. Feiertage werden bewusst NICHT beruecksichtigt (kleiner
Rand-Effekt, spart eine gepflegte Feiertagsliste/Datenquelle) - deshalb ist die Angabe
als grobe Orientierung gedacht, nicht als handelsrechtlich exakte Aussage.
"""
import datetime
import logging

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - nur falls die Zeitzonendaten fehlen
    _ET = None
    logger.warning(
        "Zeitzone America/New_York nicht verfuegbar (tzdata fehlt?) - "
        "Boersen-Session wird nicht angezeigt."
    )

_PRE_START = 4 * 60          # 04:00 ET
_REGULAR_START = 9 * 60 + 30  # 09:30 ET
_REGULAR_END = 16 * 60        # 16:00 ET
_AFTER_END = 20 * 60          # 20:00 ET

_SESSION_LABEL = {
    "open": "🟢 Börse offen",
    "pre": "🌅 vorbörslich",
    "after": "🌆 nachbörslich",
    "closed": "🌙 Börse zu",
    "weekend": "🌙 Wochenende",
}


def us_market_session(ts: float | None = None) -> str:
    """Grobe US-Aktien-Session zum Zeitpunkt ts (Epoch, Standard: jetzt):
    'open' | 'pre' | 'after' | 'closed' | 'weekend' | 'unknown' (wenn keine
    Zeitzonendaten verfuegbar sind)."""
    if _ET is None:
        return "unknown"
    now = (
        datetime.datetime.fromtimestamp(ts, _ET)
        if ts is not None
        else datetime.datetime.now(_ET)
    )
    if now.weekday() >= 5:  # 5 = Samstag, 6 = Sonntag
        return "weekend"
    minutes = now.hour * 60 + now.minute
    if _PRE_START <= minutes < _REGULAR_START:
        return "pre"
    if _REGULAR_START <= minutes < _REGULAR_END:
        return "open"
    if _REGULAR_END <= minutes < _AFTER_END:
        return "after"
    return "closed"


def session_label(session: str) -> str:
    """Kurzes, emoji-versehenes Label fuer die Anzeige - leerer String bei
    'unknown' (dann wird im Alert einfach nichts angezeigt)."""
    return _SESSION_LABEL.get(session, "")


def minutes_until_close(ts: float | None = None) -> int | None:
    """Minuten bis zum reguläeren Handelsschluss (16:00 ET) - fuer die Uebernacht-Gap-
    Antizipation (kommt ein Katalysator kurz vor Schluss, kann der Markt ihn heute kaum
    noch einpreisen). Nur an Wochentagen VOR dem Schluss sinnvoll; sonst None:
    - None am Wochenende, nach dem regulaeren Schluss (nachboerslich/zu) und ohne
      Zeitzonendaten.
    - Waehrend vorboerslich/regulaer: positive Minutenzahl bis 16:00 ET."""
    if _ET is None:
        return None
    now = (
        datetime.datetime.fromtimestamp(ts, _ET)
        if ts is not None
        else datetime.datetime.now(_ET)
    )
    if now.weekday() >= 5:
        return None
    minutes = now.hour * 60 + now.minute
    if minutes >= _REGULAR_END:
        return None
    return _REGULAR_END - minutes


def minutes_since_open(ts: float | None = None) -> int | None:
    """Minuten seit dem regulaeren Handelsbeginn (9:30 ET) - fuer die Gap-Chase-
    Bewertung (kurz nach Eroeffnung pruefen, ob ein ueber Nacht/vorboerslich gepushter
    Kurs sich noch lohnt oder schon "verbraucht" ist). Nur waehrend der laufenden
    regulaeren Session sinnvoll; sonst None (Wochenende, vor-/nachboerslich/zu, keine
    Zeitzonendaten). Waehrend der regulaeren Session: nicht-negative Minutenzahl seit
    9:30 ET (0 direkt bei Eroeffnung)."""
    if _ET is None:
        return None
    now = (
        datetime.datetime.fromtimestamp(ts, _ET)
        if ts is not None
        else datetime.datetime.now(_ET)
    )
    if now.weekday() >= 5:
        return None
    minutes = now.hour * 60 + now.minute
    if minutes < _REGULAR_START or minutes >= _REGULAR_END:
        return None
    return minutes - _REGULAR_START


def current_market_date(ts: float | None = None) -> str | None:
    """Heutiges Kalenderdatum in US-Ostkueste-Zeit als ISO-String ('YYYY-MM-DD') - fuer
    den Abgleich mit den Datumsangaben in Stooqs Tageshistorie (siehe
    prices.previous_close: welche Zeile ist "gestern", welche ggf. schon "heute"?).
    None ohne Zeitzonendaten."""
    if _ET is None:
        return None
    now = (
        datetime.datetime.fromtimestamp(ts, _ET)
        if ts is not None
        else datetime.datetime.now(_ET)
    )
    return now.date().isoformat()
