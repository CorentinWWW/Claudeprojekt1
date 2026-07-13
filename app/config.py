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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

ENABLE_NEWS = _bool("ENABLE_NEWS", True)
ENABLE_TRUTH_SOCIAL = _bool("ENABLE_TRUTH_SOCIAL", True)
ENABLE_LIVE_AUDIO = _bool("ENABLE_LIVE_AUDIO", False)

TRUTH_SOCIAL_HANDLE = os.getenv("TRUTH_SOCIAL_HANDLE", "realDonaldTrump")
# Optional: eigenes Bearer-Token (z.B. aus einer eingeloggten Browser-Session),
# falls die oeffentlichen Endpunkte ohne Auth nicht mehr funktionieren.
TRUTH_SOCIAL_BEARER_TOKEN = os.getenv("TRUTH_SOCIAL_BEARER_TOKEN", "")

LIVE_AUDIO_STREAM_URLS = [
    u.strip() for u in os.getenv("LIVE_AUDIO_STREAM_URLS", "").split(",") if u.strip()
]
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")

DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

DB_PATH = os.getenv("DB_PATH", "trump_monitor.db")
