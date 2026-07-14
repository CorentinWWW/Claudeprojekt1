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


ANTHROPIC_API_KEY = _str("ANTHROPIC_API_KEY")
# Haiku statt Sonnet als Default: die Klassifikation ist eine strukturierte,
# schema-gefuehrte Aufgabe (Tool-Use mit festem JSON-Schema) - dafuer reicht Haiku in
# der Praxis gut aus, kostet aber nur einen Bruchteil pro Call. Bei Bedarf in .env auf
# z.B. "claude-sonnet-5" fuer potenziell bessere Einschaetzungsqualitaet umstellen.
CLAUDE_MODEL = _str("CLAUDE_MODEL", "claude-haiku-4-5")
CLAUDE_MAX_RETRIES = int(os.getenv("CLAUDE_MAX_RETRIES", "3"))
CLAUDE_TIMEOUT_SECONDS = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "30"))

TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID")
# Kurze Nachricht beim Start schicken, damit sofort sichtbar ist ob Telegram korrekt verbunden ist
TELEGRAM_STARTUP_NOTICE = _bool("TELEGRAM_STARTUP_NOTICE", True)

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

ALERT_CONFIDENCE_THRESHOLD = float(os.getenv("ALERT_CONFIDENCE_THRESHOLD", "0.5"))
# Ab wie vielen gleichzeitig alarmwuerdigen Statements in EINEM Poll-Zyklus zu einer
# gebuendelten Sammel-Nachricht gewechselt wird statt einer Einzelnachricht pro Statement
# (verhindert eine Alert-Flut bei einem ploetzlichen Nachrichtenschub).
ALERT_DIGEST_THRESHOLD = int(os.getenv("ALERT_DIGEST_THRESHOLD", "3"))
# Statements, die GLEICHZEITIG (innerhalb derselben Semaphore-Runde) klassifiziert
# werden, sehen sich gegenseitig nicht im Themen-Kontext (recent_context waechst
# erst, NACHDEM eine Klassifikation fertig ist - siehe orchestrator.py:
# _classify_and_store). Bei Werten > 1 koennen zwei fast zeitgleiche Meldungen zum
# selben Thema (z.B. von zwei verschiedenen Nachrichtenquellen) beide unabhaengig
# als "neu" durchgehen und beide einen Alert ausloesen. Default bewusst auf 1
# (seriell) gesetzt, um dieses Duplikat-Risiko auszuschliessen - auf Kosten von
# etwas laengerer Verarbeitungszeit bei einem ploetzlichen Nachrichtenschub.
MAX_CONCURRENT_CLASSIFICATIONS = int(os.getenv("MAX_CONCURRENT_CLASSIFICATIONS", "1"))

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
MAX_CLASSIFICATIONS_PER_DAY = int(os.getenv("MAX_CLASSIFICATIONS_PER_DAY", "100"))

# Statements, deren Text zu >= diesem Wert (0-1, difflib-Aehnlichkeit) einem kuerzlich
# gesehenen Statement gleicht, gelten als Duplikat (z.B. dieselbe Meldung bei
# GDELT und RSS, oder von vielen Portalen wortgleich syndiziert) und werden nicht
# erneut klassifiziert/alarmiert. Dies ist die schnelle, reine Text-Ebene (Tier 1).
DEDUP_SIMILARITY_THRESHOLD = float(os.getenv("DEDUP_SIMILARITY_THRESHOLD", "0.82"))
DEDUP_WINDOW_SECONDS = int(os.getenv("DEDUP_WINDOW_SECONDS", "86400"))  # 24h ("heute")

# Themen-Ebene (Tier 2, semantisch via Claude): wie viele Stunden zurueck bereits
# alarmierte Statements als Kontext mitgegeben werden, damit Claude erkennen kann,
# ob eine neue Meldung im Kern zu einem heute schon gemeldeten Thema gehoert -
# und nur bei einer echten Eskalation trotzdem erneut alarmiert wird.
TOPIC_CONTEXT_WINDOW_HOURS = int(os.getenv("TOPIC_CONTEXT_WINDOW_HOURS", "24"))
TOPIC_CONTEXT_MAX_ITEMS = int(os.getenv("TOPIC_CONTEXT_MAX_ITEMS", "20"))

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
TRUTH_SOCIAL_BROWSER_FALLBACK_MIN_INTERVAL = int(
    os.getenv("TRUTH_SOCIAL_BROWSER_FALLBACK_MIN_INTERVAL", "300")
)

LIVE_AUDIO_STREAM_URLS = [
    u.strip() for u in os.getenv("LIVE_AUDIO_STREAM_URLS", "").split(",") if u.strip()
]
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")

DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))
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
