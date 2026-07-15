import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    # Ein gesetzter, aber leerer/nur-Whitespace-Wert (z.B. eine CI-Variable, die zu
    # einem leeren String aufgeloest wird) soll wie "nicht gesetzt" behandelt werden -
    # sonst wuerde z.B. ENABLE_NEWS="" eine Quelle stillschweigend deaktivieren, obwohl
    # der Default eigentlich True waere.
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _str(name: str, default: str = "") -> str:
    # .strip() faengt versehentlich mitkopierte Zeilenumbrueche/Leerzeichen ab
    # (z.B. beim Einfuegen eines Tokens aus Telegram/der Console in GitHub Secrets) -
    # ohne das wuerden ungueltige Zeichen erst spaeter als kryptischer URL-Fehler auftauchen.
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    # Analog zu _bool: ein gesetzter, aber leerer/nur-Whitespace-Wert soll wie "nicht
    # gesetzt" behandelt werden, statt int("") mit einem ValueError den kompletten
    # Prozess schon beim Modul-Import abstuerzen zu lassen.
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val.strip())


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return float(val.strip())


def _strlist(name: str, upper: bool = False) -> list[str]:
    """Komma-separierte Liste aus einer Env-Variable (leere Elemente/Whitespace werden
    verworfen). Fuer Watchlists o.ae. Leere/ungesetzte Variable -> leere Liste."""
    raw = os.getenv(name, "")
    items = [p.strip() for p in raw.split(",") if p.strip()]
    return [p.upper() for p in items] if upper else items


ANTHROPIC_API_KEY = _str("ANTHROPIC_API_KEY")
# Haiku statt Sonnet als Default: die Klassifikation ist eine strukturierte,
# schema-gefuehrte Aufgabe (Tool-Use mit festem JSON-Schema) - dafuer reicht Haiku in
# der Praxis gut aus, kostet aber nur einen Bruchteil pro Call. Bei Bedarf in .env auf
# z.B. "claude-sonnet-5" fuer potenziell bessere Einschaetzungsqualitaet umstellen.
CLAUDE_MODEL = _str("CLAUDE_MODEL", "claude-haiku-4-5")
CLAUDE_MAX_RETRIES = _int("CLAUDE_MAX_RETRIES", 3)
CLAUDE_TIMEOUT_SECONDS = _float("CLAUDE_TIMEOUT_SECONDS", 30)

TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID")
# Kurze Nachricht beim Start schicken, damit sofort sichtbar ist ob Telegram korrekt verbunden ist
TELEGRAM_STARTUP_NOTICE = _bool("TELEGRAM_STARTUP_NOTICE", True)

POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 60)

ALERT_CONFIDENCE_THRESHOLD = _float("ALERT_CONFIDENCE_THRESHOLD", 0.5)
# Praezisions-Filter fuer die Telegram-Alerts: es wird NUR noch alarmiert, wenn die
# Meldung mindestens EINEN konkreten Boersenticker mit klarer Long/Short-Richtung UND
# einer Pro-Ticker-Konfidenz >= diesem Wert enthaelt. Zweck: der Nutzer will nur die
# sichersten, direkt handelbaren Signale ("eine bestimmte Aktie geht hoch/runter") und
# keine allgemeinen marktrelevanten Meldungen ohne konkrete, hochsichere Aktie. Hoeher
# = weniger, aber sicherere Nachrichten; niedriger = mehr Nachrichten. 0.90 = sehr
# streng (nur die absolut sichersten Signale, teils tagelang keine); 0.85 = streng;
# 0.80 = moderat. Auf 0 setzen, um wieder JEDE marktrelevante Meldung oberhalb von
# ALERT_CONFIDENCE_THRESHOLD zu alarmieren (altes Verhalten).
ALERT_MIN_TICKER_CONFIDENCE = _float("ALERT_MIN_TICKER_CONFIDENCE", 0.90)

# --- Persoenlicher Filter (Watchlist/Blocklist) ---
# Wenn WATCHLIST_TICKERS und/oder WATCHLIST_SECTORS gesetzt sind, wird nur noch
# alarmiert, wenn ein handelbarer Ticker in der Watchlist ist ODER ein betroffener
# Sektor auf WATCHLIST_SECTORS passt (Teilstring, case-insensitive). Leer = kein
# Filter (alles erlaubt). BLOCKLIST_TICKERS entfernt einzelne Ticker generell aus der
# Alarm-Bewertung (z.B. Werte, die du ohnehin nicht handelst).
WATCHLIST_TICKERS = _strlist("WATCHLIST_TICKERS", upper=True)
WATCHLIST_SECTORS = [s.lower() for s in _strlist("WATCHLIST_SECTORS")]
BLOCKLIST_TICKERS = _strlist("BLOCKLIST_TICKERS", upper=True)

# --- Alert-Anreicherung ---
# US-Boersen-Session (offen/vor-/nachboerslich/zu) im Alert anzeigen - hilft
# einzuschaetzen, ob ein Signal gerade ueberhaupt handelbar ist.
ENABLE_MARKET_SESSION_INFO = _bool("ENABLE_MARKET_SESSION_INFO", True)
# High-Volatility-Flag (Zoelle/Sanktionen/Krieg/Fed etc.): markiert Meldungen, bei denen
# die Schwankung oft groesser ist als die klare Richtung (ggf. Straddle statt Direktional).
ENABLE_VOLATILITY_FLAG = _bool("ENABLE_VOLATILITY_FLAG", True)
# Inline-Buttons mit Chart-Links (TradingView) pro handelbarem Ticker unter dem Alert.
ENABLE_CHART_BUTTONS = _bool("ENABLE_CHART_BUTTONS", True)
# Basis-URL fuer die Chart-Buttons; {ticker} wird ersetzt.
CHART_URL_TEMPLATE = _str("CHART_URL_TEMPLATE", "https://www.tradingview.com/chart/?symbol={ticker}")

# --- Zweitmeinung fuer Grenzfaelle (#4) ---
# Meldungen, deren staerkste Ticker-Konfidenz knapp um die Alarm-Schwelle liegt
# (+/- ESCALATION_BAND), werden zur Absicherung ein zweites Mal mit einem staerkeren
# Modell klassifiziert. Kostet nur fuer diese Grenzfaelle einen Extra-Call (zaehlt gegen
# das Tages-Limit). Standardmaessig AUS, da es zusaetzliche Kosten verursacht.
ENABLE_BORDERLINE_ESCALATION = _bool("ENABLE_BORDERLINE_ESCALATION", False)
CLAUDE_ESCALATION_MODEL = _str("CLAUDE_ESCALATION_MODEL", "claude-sonnet-5")
ESCALATION_BAND = _float("ESCALATION_BAND", 0.1)

# --- Preis-Feedback / Backtesting (#2/#3/#8) ---
# Nach jedem Alert den Kurs der handelbaren Ticker erfassen und nach einem Horizont
# erneut messen, um die echte Trefferquote zu ermitteln (Dashboard-Kalibrierung) und
# die heutige Bewegung im Alert anzuzeigen. Best-effort ueber eine kostenlose Quelle
# (Stooq), ohne API-Key. Standardmaessig AUS: haengt von ausgehender Netz-Erreichbarkeit
# ab und macht pro Alert zusaetzliche HTTP-Calls - erst einschalten, wenn gewuenscht.
ENABLE_PRICE_TRACKING = _bool("ENABLE_PRICE_TRACKING", False)
PRICE_OUTCOME_HORIZON_MINUTES = _int("PRICE_OUTCOME_HORIZON_MINUTES", 60)
# Ab wie vielen gleichzeitig alarmwuerdigen Statements in EINEM Poll-Zyklus zu einer
# gebuendelten Sammel-Nachricht gewechselt wird statt einer Einzelnachricht pro Statement
# (verhindert eine Alert-Flut bei einem ploetzlichen Nachrichtenschub).
ALERT_DIGEST_THRESHOLD = _int("ALERT_DIGEST_THRESHOLD", 3)
# Statements, die GLEICHZEITIG (innerhalb derselben Semaphore-Runde) klassifiziert
# werden, sehen sich gegenseitig nicht im Themen-Kontext (recent_context waechst
# erst, NACHDEM eine Klassifikation fertig ist - siehe orchestrator.py:
# _classify_and_store). Bei Werten > 1 koennen zwei fast zeitgleiche Meldungen zum
# selben Thema (z.B. von zwei verschiedenen Nachrichtenquellen) beide unabhaengig
# als "neu" durchgehen und beide einen Alert ausloesen. Default bewusst auf 1
# (seriell) gesetzt, um dieses Duplikat-Risiko auszuschliessen - auf Kosten von
# etwas laengerer Verarbeitungszeit bei einem ploetzlichen Nachrichtenschub.
MAX_CONCURRENT_CLASSIFICATIONS = _int("MAX_CONCURRENT_CLASSIFICATIONS", 1)

# Harter Kostendeckel: mehr als so viele Claude-Klassifikations-Calls finden an einem
# Tag (UTC) nicht mehr statt, egal wie viele neue Statements eintreffen - schuetzt vor
# einem einzelnen Nachrichtenschub, der sonst unbegrenzt Kosten verursachen wuerde
# (jeder NEUE, noch nicht bekannte Statement-Text kostet einen Call, auch wenn er sich
# danach als Themen-Duplikat herausstellt - die Zweistufige-Duplikaterkennung spart
# also Alerts, aber nicht diesen Call selbst). Bei ~1000-1500 Input- und 200-400
# Output-Tokens pro Call kostet der Default von 100 Calls/Tag bei Haiku-4.5-Preisen
# (Stand: $1 / $5 pro 1 Mio. Token) grob geschaetzt max. ca. 0.15-0.25 EUR/Tag,
# unabhaengig vom tatsaechlichen Nachrichtenaufkommen. Persistiert in SQLite, gilt also
# auch ueber einzelne GitHub-Actions-Laeufe hinweg (siehe app/db.py:
# get_classification_calls_today/record_classification_call).
MAX_CLASSIFICATIONS_PER_DAY = _int("MAX_CLASSIFICATIONS_PER_DAY", 100)

# ZUSAETZLICHE Reserve oberhalb von MAX_CLASSIFICATIONS_PER_DAY, die AUSSCHLIESSLICH
# fuer als besonders wichtig eingestufte Meldungen (siehe orchestrator.py:
# is_high_priority - direkte Trump-Posts, harte Wirtschaftsthemen wie Zoelle/Sanktionen/
# Zinsen) verwendet werden darf. Zweck: an einem Tag mit einem Nachrichtenschub soll das
# normale Limit nicht dazu fuehren, dass eine WIRKLICH wichtige Meldung stillschweigend
# uebersprungen wird, nur weil vorher viel Unwichtiges den Zaehler gefuellt hat. Das
# absolute Tages-Maximum bleibt hart gedeckelt bei MAX + PRIORITY (Default 100 + 30 =
# 130) - die Kosten koennen also nicht davonlaufen. Auf 0 setzen, um die Reserve zu
# deaktivieren (dann gilt wieder ein einziges hartes Limit fuer alle).
PRIORITY_CLASSIFICATIONS_PER_DAY = _int("PRIORITY_CLASSIFICATIONS_PER_DAY", 30)

# Statements, deren Text zu >= diesem Wert (0-1, difflib-Aehnlichkeit) einem kuerzlich
# gesehenen Statement gleicht, gelten als Duplikat (z.B. dieselbe Meldung bei
# GDELT und RSS, oder von vielen Portalen wortgleich syndiziert) und werden nicht
# erneut klassifiziert/alarmiert. Dies ist die schnelle, reine Text-Ebene (Tier 1).
DEDUP_SIMILARITY_THRESHOLD = _float("DEDUP_SIMILARITY_THRESHOLD", 0.82)
DEDUP_WINDOW_SECONDS = _int("DEDUP_WINDOW_SECONDS", 86400)  # 24h ("heute")

# Themen-Ebene (Tier 2, semantisch via Claude): wie viele Stunden zurueck bereits
# alarmierte Statements als Kontext mitgegeben werden, damit Claude erkennen kann,
# ob eine neue Meldung im Kern zu einem heute schon gemeldeten Thema gehoert -
# und nur bei einer echten Eskalation trotzdem erneut alarmiert wird.
TOPIC_CONTEXT_WINDOW_HOURS = _int("TOPIC_CONTEXT_WINDOW_HOURS", 24)
TOPIC_CONTEXT_MAX_ITEMS = _int("TOPIC_CONTEXT_MAX_ITEMS", 20)

ENABLE_NEWS = _bool("ENABLE_NEWS", True)
ENABLE_TRUTH_SOCIAL = _bool("ENABLE_TRUTH_SOCIAL", True)
ENABLE_LIVE_AUDIO = _bool("ENABLE_LIVE_AUDIO", False)

TRUTH_SOCIAL_HANDLE = _str("TRUTH_SOCIAL_HANDLE", "realDonaldTrump")
# Optional: eigenes Bearer-Token (z.B. aus einer eingeloggten Browser-Session),
# falls die oeffentlichen Endpunkte ohne Auth nicht mehr funktionieren.
TRUTH_SOCIAL_BEARER_TOKEN = _str("TRUTH_SOCIAL_BEARER_TOKEN")
# Fallback: falls der direkte (unauthentifizierte) API-Call fehlschlaegt, mit einem
# echten headless Chromium die Profilseite laden und die Netzwerk-Antworten der
# Seite selbst mitschneiden (robuster gegen Bot-Blocking als ein nackter HTTP-Call,
# aber deutlich teurer an CPU/RAM). Braucht "pip install playwright" + Chromium.
TRUTH_SOCIAL_BROWSER_FALLBACK = _bool("TRUTH_SOCIAL_BROWSER_FALLBACK", True)
# Mindestabstand zwischen zwei Browser-Fallback-Versuchen, damit ein dauerhaft
# blockierter direkter API-Call nicht bei jedem Poll-Zyklus einen vollen Chromium
# startet.
TRUTH_SOCIAL_BROWSER_FALLBACK_MIN_INTERVAL = _int(
    "TRUTH_SOCIAL_BROWSER_FALLBACK_MIN_INTERVAL", 300
)

LIVE_AUDIO_STREAM_URLS = [
    u.strip() for u in os.getenv("LIVE_AUDIO_STREAM_URLS", "").split(",") if u.strip()
]
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")

DASHBOARD_PORT = _int("DASHBOARD_PORT", 8000)
# Falls gesetzt, verlangen alle /api/*-Endpunkte einen passenden "X-API-Key"-Header.
# Ohne das waere z.B. /api/test (kostet einen echten Claude-Call + kann einen echten
# Telegram-Alert ausloesen) fuer JEDEN erreichbar, der die IP:Port kennt - insbesondere
# relevant, weil die README-Anleitung fuer die Oracle-Cloud-Variante explizit dazu
# anleitet, Port 8000 fuer 0.0.0.0/0 zu oeffnen.
DASHBOARD_API_KEY = _str("DASHBOARD_API_KEY")

DB_PATH = os.getenv("DB_PATH", "trump_monitor.db")


def validate() -> tuple[list[str], list[str]]:
    """Prueft die Konfiguration. Gibt (fatal_errors, warnings) zurueck.

    Fatal-Errors bedeuten: die Klassifikations-Pipeline kann nicht laufen.
    Warnings bedeuten: das System laeuft, aber mit eingeschraenkter Funktion.
    """
    errors = []
    warnings = []

    if not ANTHROPIC_API_KEY:
        errors.append(
            "ANTHROPIC_API_KEY ist nicht gesetzt - Klassifikation kann nicht laufen. "
            "In .env eintragen (siehe .env.example)."
        )

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        warnings.append(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID nicht gesetzt - es werden keine "
            "Telegram-Alerts verschickt, Statements werden nur in der DB erfasst."
        )

    if not ENABLE_NEWS and not ENABLE_TRUTH_SOCIAL and not ENABLE_LIVE_AUDIO:
        errors.append("Alle Quellen sind deaktiviert (ENABLE_NEWS/TRUTH_SOCIAL/LIVE_AUDIO=false).")

    if ENABLE_LIVE_AUDIO and not LIVE_AUDIO_STREAM_URLS:
        warnings.append(
            "ENABLE_LIVE_AUDIO=true aber LIVE_AUDIO_STREAM_URLS ist leer - "
            "Live-Audio-Quelle liefert dadurch nie Ergebnisse."
        )

    if not DASHBOARD_API_KEY:
        warnings.append(
            "DASHBOARD_API_KEY ist nicht gesetzt - alle /api/*-Endpunkte (inkl. /api/test, "
            "das echte Claude-Calls + Telegram-Alerts ausloesen kann) sind ungeschuetzt "
            "erreichbar. Falls das Dashboard oeffentlich erreichbar ist (z.B. Oracle-Cloud-"
            "Anleitung mit offenem Port 8000), dringend einen Wert setzen."
        )

    return errors, warnings
