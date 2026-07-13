import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
CLAUDE_MAX_RETRIES = int(os.getenv("CLAUDE_MAX_RETRIES", "3"))
CLAUDE_TIMEOUT_SECONDS = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "30"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Kurze Nachricht beim Start schicken, damit sofort sichtbar ist ob Telegram korrekt verbunden ist
TELEGRAM_STARTUP_NOTICE = _bool("TELEGRAM_STARTUP_NOTICE", True)

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

ALERT_CONFIDENCE_THRESHOLD = float(os.getenv("ALERT_CONFIDENCE_THRESHOLD", "0.5"))
MAX_CONCURRENT_CLASSIFICATIONS = int(os.getenv("MAX_CONCURRENT_CLASSIFICATIONS", "3"))

# Statements, deren Text zu >= diesem Wert (0-1, difflib-Aehnlichkeit) einem kuerzlich
# gesehenen Statement gleicht, gelten als Duplikat (z.B. dieselbe Meldung bei
# GDELT und RSS) und werden nicht erneut klassifiziert/alarmiert.
DEDUP_SIMILARITY_THRESHOLD = float(os.getenv("DEDUP_SIMILARITY_THRESHOLD", "0.82"))
DEDUP_WINDOW_SECONDS = int(os.getenv("DEDUP_WINDOW_SECONDS", "21600"))  # 6h

ENABLE_NEWS = _bool("ENABLE_NEWS", True)
ENABLE_TRUTH_SOCIAL = _bool("ENABLE_TRUTH_SOCIAL", True)
ENABLE_LIVE_AUDIO = _bool("ENABLE_LIVE_AUDIO", False)

TRUTH_SOCIAL_HANDLE = os.getenv("TRUTH_SOCIAL_HANDLE", "realDonaldTrump")
# Optional: eigenes Bearer-Token (z.B. aus einer eingeloggten Browser-Session),
# falls die oeffentlichen Endpunkte ohne Auth nicht mehr funktionieren.
TRUTH_SOCIAL_BEARER_TOKEN = os.getenv("TRUTH_SOCIAL_BEARER_TOKEN", "")
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

    return errors, warnings
