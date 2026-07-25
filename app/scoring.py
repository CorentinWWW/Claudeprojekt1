"""Reine, Claude-FREIE Bewertungs-Hilfen fuer die Alert-Anreicherung.

Alle Funktionen hier sind seiteneffektfrei und deterministisch (kein Netz, keine DB,
kein Claude-Call) - damit billig, schnell und vollstaendig unit-testbar. Sie fassen die
Signale, die ohnehin schon vorliegen (Claude-Konfidenzen, Textmerkmale, Frische,
Quellen-Korroboration), zu einer einzigen, auf einen Blick lesbaren Ueberzeugungs-Zahl
zusammen und leiten daraus eine grobe, ausdruecklich unverbindliche Positionsgroessen-
Einordnung ab. KEINE Anlageberatung - nur eine Verdichtung der vorhandenen Signale.
"""
import math
import re
from typing import Optional

# --- Hedge-/Geruecht-Erkennung (#2) -----------------------------------------------
# Sprachliche Marker fuer UNBESTAETIGTE/spekulative Meldungen ("Berichten zufolge",
# "erwaegt", "koennte", "in Gespraechen"). Solche Meldungen sind handelsrelevant, aber
# unsicherer als eine vollzogene Tatsache - sie senken die Ueberzeugung und bekommen im
# Alert einen Vorsicht-Hinweis. Bewusst KURATIERT (nur eindeutige Hedge-Woerter), damit
# nicht jede zweite Meldung faelschlich als Geruecht gilt. Bare "may"/"Mai" bewusst NICHT
# aufgenommen (zu viele Fehltreffer durch den Monatsnamen/das Allerweltswort).
_HEDGE_PATTERN = re.compile(
    r"\b("
    r"reportedly|allegedly|rumou?rs?|rumou?red|speculat\w*|"
    r"could|might|considering|weighing|mulls?|mulling|in talks|"
    r"is said to|are said to|sources say|potential(?:ly)?|reportedly|"
    r"denies|denied|deny|unconfirmed|"
    # deutschsprachige Pendants
    r"angeblich|berichten zufolge|geruechten zufolge|gerüchten zufolge|"
    r"koennte|könnte|duerfte|dürfte|erwaegt|erwägt|prueft|prüft|"
    r"in gespraechen|in gesprächen|dementiert|unbestaetigt|unbestätigt"
    r")\b",
    re.IGNORECASE,
)


def has_hedge_language(text: Optional[str]) -> bool:
    """True, wenn der Text spekulative/unbestaetigte Formulierungen enthaelt (Geruecht,
    Erwaegung, Konjunktiv). Leerer/fehlender Text -> False."""
    if not text:
        return False
    return _HEDGE_PATTERN.search(text) is not None


# --- Hohe-Volatilitaet-Erkennung (#6, gemeinsame Quelle) --------------------------
# Themen, bei denen die Schwankung oft groesser ist als die klare Richtung (dann ist ein
# direktionaler Trade riskanter). Eine EINZIGE Definition, die sowohl der Volatilitaets-
# Hinweis im Alert (telegram_alert) als auch der Ueberzeugungs-Score (Abschlag) nutzen.
_VOLATILITY_PATTERN = re.compile(
    r"\b("
    r"tariffs?|zoll|zoelle|zölle|sanctions?|sanktion|embargo|shutdown|default|"
    r"war|krieg|invasion|nuclear|militar\w*|airstrike|"
    r"nationaliz\w*|verstaatlich\w*|export ban|import ban|price cap|"
    r"bailout|stimulus|federal reserve|interest rate|rate cut|rate hike|zinsen|leitzins"
    r")\b",
    re.IGNORECASE,
)


def high_volatility_text(text: Optional[str]) -> bool:
    """True, wenn der Text auf ein Hoch-Volatilitaets-Thema hindeutet. Leerer/fehlender
    Text -> False."""
    if not text:
        return False
    return _VOLATILITY_PATTERN.search(text) is not None


# --- Ueberzeugungs-Score (#1) -----------------------------------------------------
def _freshness_adjust(freshness_minutes: Optional[float]) -> float:
    """Frische-Zuschlag/-Abschlag: brandneue Meldungen sind wertvoller (noch nicht
    eingepreist), alte oft schon verarbeitet. None (kein Zeitstempel) -> 0 (neutral)."""
    if freshness_minutes is None:
        return 0.0
    if freshness_minutes < 0:
        freshness_minutes = 0.0
    if freshness_minutes <= 10:
        return 8.0
    if freshness_minutes <= 30:
        return 4.0
    if freshness_minutes <= 120:
        return 0.0
    if freshness_minutes <= 1440:  # bis 1 Tag
        return -6.0
    return -12.0


def conviction_score(
    overall_confidence: float,
    strongest_ticker_confidence: float,
    freshness_minutes: Optional[float] = None,
    corroboration_sources: int = 1,
    hedged: bool = False,
    high_volatility: bool = False,
) -> int:
    """Verdichtet die vorhandenen Signale zu einer Ueberzeugungs-Zahl 0-100.

    Kern ist die Pro-Ticker-Konfidenz des sichersten handelbaren Tickers (das ist der
    eigentliche 'diese Aktie geht hoch/runter'-Grund), ergaenzt um die Gesamt-Konfidenz.
    Zu-/Abschlaege fuer Frische, unabhaengige Bestaetigung durch mehrere Quellen,
    spekulative Sprache und hohe Volatilitaet (Richtung unsicherer). Bewusst transparent
    und additiv, damit im Alert nachvollziehbar bleibt, warum eine Meldung wie stark ist.
    """
    overall = max(0.0, min(1.0, float(overall_confidence or 0.0)))
    strongest = max(0.0, min(1.0, float(strongest_ticker_confidence or 0.0)))
    base = 100.0 * (0.35 * overall + 0.65 * strongest)

    score = base
    score += _freshness_adjust(freshness_minutes)
    # Jede ZUSAETZLICHE unabhaengige Quelle (ueber die erste hinaus) bestaetigt die
    # Meldung -> +3, gedeckelt bei +9 (3 Extra-Quellen).
    score += 3.0 * max(0, min(3, (corroboration_sources or 1) - 1))
    if hedged:
        score -= 12.0
    if high_volatility:
        score -= 5.0

    return int(round(max(0.0, min(100.0, score))))


# --- Positionsgroessen-Einordnung (#4) --------------------------------------------
# AUSDRUECKLICH unverbindliche, grobe Einordnung aus dem Ueberzeugungs-Score - keine
# Anlageberatung, kein konkreter Geldbetrag, nur eine relative "wie stark ist dieses
# Signal im Vergleich"-Kategorie.
def position_tier(score: int) -> str:
    """Grobe, unverbindliche Ueberzeugungs-Kategorie aus dem conviction_score."""
    if score >= 88:
        return "🟢 hohe Überzeugung"
    if score >= 75:
        return "🟡 Standard"
    return "🟠 Sondierung"


# --- Kelly-lite Positionsanteil (#5) ----------------------------------------------
def kelly_fraction(
    hit_rate: Optional[float],
    avg_win_pct: Optional[float],
    avg_loss_pct: Optional[float],
    kelly_multiplier: float = 0.5,
    cap: float = 0.25,
) -> Optional[float]:
    """Grober, UNVERBINDLICHER Bankroll-Anteil nach dem Kelly-Kriterium aus der
    historischen Trefferquote und dem mittleren Gewinn/Verlust. Bewusst konservativ:
    Half-Kelly (kelly_multiplier=0.5) und hart gedeckelt (cap, Standard 25%). Gibt einen
    Anteil in [0, cap] zurueck - oder None, wenn die Eingaben unbrauchbar sind (keine
    Historie, nicht-positiver Gewinn/Verlust). KEINE Anlageberatung.

    f* = p - (1-p)/b   mit b = avg_win/avg_loss (Odds); negatives f* -> 0 (kein Edge)."""
    if hit_rate is None or avg_win_pct is None or avg_loss_pct is None:
        return None
    if not (0.0 <= hit_rate <= 1.0):
        return None
    if avg_win_pct <= 0 or avg_loss_pct <= 0:
        return None
    b = avg_win_pct / avg_loss_pct
    edge = hit_rate - (1.0 - hit_rate) / b
    if edge <= 0:
        return 0.0
    return min(cap, edge * kelly_multiplier)


# --- Historische-Performance-Feedback (Ticker lernt aus eigenen vergangenen Alerts) -----
def historical_performance_adjust(
    hit_rate: Optional[float], n: int, min_samples: int, weight: float
) -> float:
    """Ueberzeugungs-Score-Zu-/Abschlag aus der HISTORISCHEN Trefferquote eines Tickers -
    macht die eigene bisherige Erfolgsbilanz fuer GENAU diesen Ticker zu einem Signal fuer
    neue Alerts, statt jede neue Meldung unabhaengig davon gleich zu behandeln. 50%
    Trefferquote (Coinflip) wirkt neutral (0.0); linear skaliert bis +weight bei 100%
    bzw. -weight bei 0%. Gibt 0.0 (kein Effekt) zurueck, wenn keine Trefferquote vorliegt,
    zu wenige Samples (< min_samples) - verhindert, dass 1-2 Zufallstreffer/-verluste den
    Score verzerren - oder weight <= 0 (Feature aus)."""
    if hit_rate is None or n < min_samples or weight <= 0:
        return 0.0
    return (hit_rate - 0.5) * 2.0 * weight


# --- Erwartete Bewegung (#3) ------------------------------------------------------
def format_expected_move(expected_move_pct, expected_horizon) -> str:
    """Kompakte Anzeige der von Claude geschaetzten erwarteten Kursbewegung + Horizont,
    z.B. '~4% · Tage'. Leerer String, wenn keine (brauchbare) Schaetzung vorliegt."""
    if not isinstance(expected_move_pct, (int, float)):
        return ""
    pct = abs(float(expected_move_pct))
    if pct <= 0:
        return ""
    horizon = str(expected_horizon).strip() if expected_horizon else ""
    return f"~{pct:.0f}%" + (f" · {horizon}" if horizon else "")


# --- Uebernacht-/Vorboersen-Gap-Antizipation --------------------------------------
# Zweck (Nutzerwunsch): fruehzeitig sagen, dass eine Aktie steigen (oder fallen) wird,
# BEVOR der Katalysator kurz vor/nach Boersenschluss ausgehypt wird und der Kurs dann
# VORBOERSLICH schon extrem gegappt ist. Kern-Idee: kommt ein klar gerichteter, frischer
# Katalysator in einem Fenster, in dem der Markt ihn NICHT MEHR VOLL EINPREISEN kann
# (nachboerslich, ueber Nacht, uebers Wochenende - oder nur noch wenige Minuten vor
# Schluss), landet die Reaktion oft als Gap im naechsten Open. Genau dann soll der Alert
# sagen: "jetzt rein, bevor es zum naechsten Open hochschiesst" statt erst, wenn der
# Gap vorboerslich schon gelaufen ist.

_GAP_PHASE = {
    "after": "nachbörslich",
    "closed": "über Nacht",
    "weekend": "übers Wochenende",
    "near_close": "kurz vor Börsenschluss",
    "pre": "vorbörslich",
}


def predict_gap(
    direction: Optional[str],
    session: Optional[str],
    minutes_to_close: Optional[int],
    expected_move_pct: Optional[float] = None,
    near_close_minutes: int = 45,
    min_expected_move_pct: float = 0.0,
) -> Optional[dict]:
    """Antizipiert einen Uebernacht-/Vorboersen-Gap fuer einen klar gerichteten Alert.

    Gibt None zurueck, wenn kein Gap-Fenster vorliegt (z.B. mitten in der regulaeren
    Session, wenn der Markt die Meldung noch ueber Stunden einpreisen kann - dann ist es
    eine normale Intraday-Bewegung, kein Uebernacht-Gap). Sonst ein Dict:
        {
          "gap_direction": "up" | "down",   # up = Kurs duerfte hoch gappen
          "phase": "<menschlich>",          # z.B. "nachbörslich"
          "early": True/False,              # True = noch VOR dem Gap (Einstieg moeglich);
                                            # False = vorboerslich, Gap laeuft evtl. schon
        }
    Rein zeit-/richtungsbasiert (kein Netz/Claude). min_expected_move_pct filtert
    optional Mini-Katalysatoren heraus (0 = aus)."""
    if direction == "long":
        gap_direction = "up"
    elif direction == "short":
        gap_direction = "down"
    else:
        return None

    if session in (None, "unknown"):
        return None

    # Optionaler Mindest-Erwartungswert (falls Claude eine Schaetzung geliefert hat).
    if min_expected_move_pct > 0 and isinstance(expected_move_pct, (int, float)):
        if abs(float(expected_move_pct)) < min_expected_move_pct:
            return None

    if session in ("after", "closed", "weekend"):
        return {"gap_direction": gap_direction, "phase": _GAP_PHASE[session], "early": True}

    if session == "open":
        # Nur noch kurz bis Schluss: kaum Zeit, heute einzupreisen -> Gap-Kandidat.
        if isinstance(minutes_to_close, int) and 0 <= minutes_to_close <= near_close_minutes:
            return {"gap_direction": gap_direction, "phase": _GAP_PHASE["near_close"],
                    "early": True}
        return None

    if session == "pre":
        # Vorboerslich: das Gap laeuft moeglicherweise gerade schon - noch melden, aber
        # als "evtl. schon in Bewegung" markieren (early=False).
        return {"gap_direction": gap_direction, "phase": _GAP_PHASE["pre"], "early": False}

    return None


# --- Gap-Chase-Bewertung (Gegenstueck zu predict_gap: der Gap ist bereits passiert) ----
# predict_gap() warnt VOR einem Gap, waehrend der Markt noch zu ist. Dieses Gegenstueck
# greift NACH der Eroeffnung: ist ein Ticker bereits ueber Nacht/vorboerslich stark in
# Signalrichtung gegappt (Markt war zu, jetzt zur Boersenoeffnung extrem hoch/niedrig),
# lohnt sich ein Einstieg dann ueberhaupt noch, oder ist der Groteil der erwarteten
# Bewegung schon gelaufen (Chase-/Gap-Fade-Risiko)?
def evaluate_gap_chase(
    direction: Optional[str],
    gap_pct: Optional[float],
    expected_move_pct: Optional[float] = None,
    too_late_ratio: float = 0.8,
    too_late_abs_pct: float = 6.0,
) -> Optional[dict]:
    """Bewertet einen bereits GESCHEHENEN Ueber-Nacht-/Vorboersen-Gap kurz nach
    Boersenoeffnung: lohnt sich ein Einstieg noch, oder ist der Groteil der erwarteten
    Bewegung schon gelaufen?

    gap_pct = (heutiger Eroeffnungskurs - gestriger Schluss) / gestriger Schluss * 100
    (siehe prices.previous_close), VORZEICHENBEHAFTET (nicht richtungsbereinigt).

    Gibt None zurueck, wenn keine Bewertung moeglich/sinnvoll ist: fehlende Richtung/
    Gap, oder der Gap ging GEGEN die eingeschaetzte Richtung (das ist die bestehende
    Kurs-Divergenz-Warnung, kein Chase-Fall). Sonst ein Dict:
        {
          "gap_pct": <richtungsbereinigter Gap in %, positiv = mit der These>,
          "too_late": True/False,
          "used_fraction": <Anteil der erwarteten Bewegung, der schon gelaufen ist,
                            oder None ohne Claude-Schaetzung>,
          "remaining_pct": <geschaetzte verbleibende Bewegung in %, oder None>,
        }

    Mit Erwartungswert (expected_move_pct > 0, aus Claudes Schaetzung): "zu spaet",
    wenn der Gap bereits >= too_late_ratio (Standard 80%) der erwarteten Gesamtbewegung
    ausgemacht hat. Ohne Erwartungswert: "zu spaet", wenn der Gap allein schon
    >= too_late_abs_pct (Standard 6%) betraegt - eine grobe, konservative Ersatzschwelle."""
    if direction not in ("long", "short"):
        return None
    if not isinstance(gap_pct, (int, float)) or not math.isfinite(gap_pct):
        return None
    with_thesis = gap_pct if direction == "long" else -gap_pct
    if with_thesis <= 0:
        # Gegen die These gegappt - kein Chase-Fall (das deckt die bestehende
        # Divergenz-Warnung ab), sondern schlicht kein Grund zur Eile.
        return None

    if isinstance(expected_move_pct, (int, float)) and expected_move_pct > 0:
        used_fraction = with_thesis / expected_move_pct
        too_late = used_fraction >= too_late_ratio
        remaining_pct = max(0.0, expected_move_pct - with_thesis)
        return {
            "gap_pct": with_thesis, "too_late": too_late,
            "used_fraction": used_fraction, "remaining_pct": remaining_pct,
            # Fortschritt RELATIV zur 'zu spaet'-Schwelle (1.0 = Schwelle exakt erreicht,
            # >1.0 = schon drueber) - fuer die Balken-Visualisierung im Alert (#Grafik).
            "cutoff_fraction": used_fraction / too_late_ratio if too_late_ratio > 0 else None,
        }

    too_late = with_thesis >= too_late_abs_pct
    return {
        "gap_pct": with_thesis, "too_late": too_late, "used_fraction": None, "remaining_pct": None,
        "cutoff_fraction": with_thesis / too_late_abs_pct if too_late_abs_pct > 0 else None,
    }


# --- Fortschrittsbalken (reine Text-/Unicode-Visualisierung, kein Bild/Dependency) ------
def format_progress_bar(fraction: Optional[float], width: int = 10) -> str:
    """Text-Fortschrittsbalken fuer die Gap-Chase-Cutoff-Visualisierung im Alert
    ("bei welcher Luecke lohnt sich ein Kauf nicht mehr") - bewusst als reiner
    Unicode-Text statt eines generierten Bildes: kein zusaetzlicher Dependency
    (matplotlib/Pillow wuerden die Installationszeit der 48x/Tag frisch
    aufgesetzten GitHub-Actions-Laeufe spuerbar verlaengern) und rendert in JEDEM
    Telegram-Client sofort, ohne Bild-Upload.

    fraction=1.0 -> Balken exakt voll (= 'zu spaet'-Schwelle erreicht). Werte > 1.0
    (Schwelle schon ueberschritten) werden bei einem vollen Balken gedeckelt;
    None/nicht-endliche Werte -> leerer Balken (0%)."""
    if not isinstance(fraction, (int, float)) or not math.isfinite(fraction):
        fraction = 0.0
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {fraction * 100:.0f}% bis 'zu spät'"


# --- Handlungsempfehlung (#Empfehlung) ---------------------------------------------
def trade_recommendation(
    score: int,
    direction: Optional[str],
    technical_contradicts_strongly: bool = False,
    gap_too_late: bool = False,
    hedged: bool = False,
    divergence: bool = False,
    market_open: bool = True,
) -> dict:
    """Verdichtet die bereits vorhandenen Signale (Ueberzeugungs-Score inkl. aller
    Score-Anpassungen, technische Zweitmeinung, Gap-Chase-Timing, Geruecht-/
    Divergenz-Warnung) zu EINER klaren, kurzen Handlungsempfehlung - macht explizit,
    was man sonst aus mehreren einzelnen Alert-Zeilen selbst zusammenreimen muesste.
    Rein additiv aus vorhandenen Signalen, KEIN zusaetzlicher Claude-Call. Ausdruecklich
    KEINE Anlageberatung.

    Zwei harte Gegenanzeigen (technical_contradicts_strongly, gap_too_late) druecken die
    Empfehlung mindestens auf 'wait'; kommt dazu noch ein niedriger Score, auf 'avoid'.
    Sonst entscheidet der Score allein (>= 85 -> starkes 'buy', 60-84 -> normales 'buy',
    < 60 -> 'wait').

    market_open=False (vor-/nachboerslich, Wochenende, Feiertag): die 'buy'-Formulierung
    wechselt von 'Jetzt kaufen' auf 'Zum naechsten Handelsstart kaufen' - eine ECHTE
    Order kann ausserhalb der Handelszeiten ohnehin nicht ausgefuehrt werden, "jetzt"
    waere dann irrefuehrend. Das Paper-Depot eroeffnet die Position trotzdem sofort (zum
    best-effort aktuellen Kurs) - dieser Parameter aendert nur den TEXT, nicht ob/wann
    eine Position eroeffnet wird."""
    if direction not in ("long", "short"):
        return {"action": "wait", "label": "🟠 Kein klares Signal", "reasons": []}

    verb = "kaufen" if direction == "long" else "shorten"
    reasons = []
    if technical_contradicts_strongly:
        reasons.append("Technik widerspricht klar")
    if gap_too_late:
        reasons.append("Bewegung bereits größtenteils gelaufen")
    if hedged:
        reasons.append("unbestätigt/Gerücht")
    if divergence:
        reasons.append("Kurs läuft bereits gegen die These")
    hard_block = technical_contradicts_strongly or gap_too_late

    if hard_block and score < 50:
        return {"action": "avoid", "label": f"🔴 Eher NICHT {verb}", "reasons": reasons}
    if hard_block or score < 60:
        return {
            "action": "wait", "label": "🟠 Abwarten / nur kleine Position",
            "reasons": reasons or ["Überzeugung noch moderat (< 60)"],
        }
    now_str = "Jetzt" if market_open else "Zum nächsten Handelsstart"
    if not market_open:
        reasons.append("Markt aktuell geschlossen")
    if score >= 85:
        return {"action": "buy", "label": f"🟢 {now_str} {verb}", "reasons": reasons or ["hohe Überzeugung"]}
    size_str = "Standard-Größe" if market_open else "Standard-Größe, zum nächsten Open"
    return {
        "action": "buy", "label": f"🟡 {verb.capitalize()}, {size_str}",
        "reasons": reasons or ["solide Überzeugung"],
    }


# --- Ruhezeiten-Fenster (#7, reine Zeitfenster-Logik) -----------------------------
def parse_hour_window(spec: Optional[str]) -> Optional[tuple[int, int]]:
    """Parst eine Ruhezeit-Angabe 'START-ENDE' (ganze Stunden 0-23, z.B. '23-7') zu
    (start, end). None bei leerer/ungueltiger Angabe (dann keine Ruhezeit aktiv)."""
    if not spec or "-" not in spec:
        return None
    a, _, b = spec.partition("-")
    try:
        start, end = int(a.strip()), int(b.strip())
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return None
    return (start, end)


def hour_in_window(hour: int, window: Optional[tuple[int, int]]) -> bool:
    """True, wenn die (ganze) Stunde im Fenster liegt - inkl. Mitternachts-Umschlag
    (z.B. 23-7 deckt 23,0,1,...,6 ab). window=None -> immer False (kein Fenster)."""
    if window is None:
        return False
    start, end = window
    if start < end:
        return start <= hour < end
    # Umschlag ueber Mitternacht
    return hour >= start or hour < end
