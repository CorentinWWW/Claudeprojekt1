"""Anbieter-unabhaengige KI-Schicht: eine Kette KOSTENLOSER, OpenAI-kompatibler
Anbieter mit automatischem Ausweichen - Claude (Anthropic) ist nur noch EIN optionaler
Anbieter unter mehreren.

Warum: jeder Claude-Call kostet Geld. Ist das Guthaben leer, stand bisher der gesamte
Monitor still - der Start-Selbsttest scheitert, der Monitoring-Loop startet gar nicht
erst. Mehrere Anbieter haben dauerhaft kostenlose Kontingente hinter einer
OpenAI-kompatiblen Schnittstelle (POST {base_url}/chat/completions, Bearer-Token) -
EIN generischer HTTP-Client (httpx, ohnehin Abhaengigkeit) deckt sie alle ab, auch
ein selbst gehostetes Ollama.

Gratis-Tarife haben enge Limits (Anfragen pro Minute UND pro Tag, teils Tokens pro
Tag). Deshalb:
- Pacing: Mindestabstand zwischen zwei Anfragen je Anbieter, statt erst in einen
  429-Sturm zu laufen.
- Failover: 429/Tageslimit/Serverfehler/kaputter Key -> dieser Anbieter pausiert
  (Cooldown, wachsend bei Wiederholung), der naechste in der Kette uebernimmt. Ein
  Erfolg setzt den Anbieter sofort zurueck.
- Status fuer /api/health - OHNE Keys: der Endpunkt ist bewusst oeffentlich, und
  manche Anbieter wiederholen den Key in ihren Fehlermeldungen (live bei Alpha Vantage
  erlebt). Fehlertexte werden deshalb vor dem Speichern um den Key bereinigt.

Konfiguration (alles in .env, siehe .env.example):
- <PREFIX>_API_KEY aktiviert einen Anbieter (z.B. GROQ_API_KEY, GEMINI_API_KEY).
- <PREFIX>_MODEL / <PREFIX>_BASE_URL / <PREFIX>_MIN_INTERVAL_SECONDS ueberschreiben
  die Voreinstellung.
- LLM_PROVIDERS legt die Reihenfolge fest (z.B. "groq,gemini,anthropic"); leer =
  DEFAULT_ORDER, gefiltert auf die konfigurierten Anbieter.
"""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Preset:
    env_prefix: str
    base_url: str
    default_model: str
    # Mindestabstand zwischen zwei Anfragen (Sekunden) - bewusst etwas unter dem
    # dokumentierten Minutenlimit des Gratis-Tarifs, damit Uhrenversatz/parallel
    # laufende Aufrufe nicht sofort ein 429 ausloesen.
    min_interval_seconds: float
    signup_url: str
    key_required: bool = True
    timeout_seconds: float = 45.0
    # Zusaetzliche Request-Felder, nur wenn der Modellname passt (z.B. Denk-Aufwand
    # bei Reasoning-Modellen niedrig halten: spart Tokens vom Tageskontingent und
    # verhindert, dass das Denken das max_tokens-Budget aufbraucht).
    extra_body_by_model_substring: dict = field(default_factory=dict)


# Stand der Gratis-Tarife: recherchiert 2026-09-29. Diese Tarife aendern sich laufend
# (allein 2026: GitHub Models eingestellt, Cerebras ohne Dauer-Gratistarif, Groq hat
# Llama aus dem Gratisplan genommen, Gemini 2.5 fuer Neukunden geschlossen). Ein
# veraltetes Modell kostet hier nichts ausser einem Log-Eintrag: 404/"decommissioned"
# pausiert nur dieses Kettenglied, der naechste Anbieter uebernimmt. Modelle lassen
# sich per <PREFIX>_MODEL in der .env ueberschreiben.
PRESETS: dict[str, Preset] = {
    # ~500 Anfragen/Tag, ~15/Minute (Google veroeffentlicht die Limits nicht mehr fix,
    # AI Studio zeigt sie pro Projekt). Traegt die Tageslast des Bots allein.
    "gemini": Preset(
        env_prefix="GEMINI",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-3.5-flash-lite",
        min_interval_seconds=5.0,
        signup_url="https://aistudio.google.com/apikey",
        extra_body_by_model_substring={"gemini-3": {"reasoning_effort": "low"}},
    ),
    # Pro MODELL eigenes Kontingent: 30/Minute, 1000/Tag, 8K Tokens/Minute, 200K
    # Tokens/Tag - deshalb stehen zwei Groq-Modelle getrennt in DEFAULT_ORDER.
    "groq": Preset(
        env_prefix="GROQ",
        base_url="https://api.groq.com/openai/v1",
        default_model="openai/gpt-oss-120b",
        min_interval_seconds=2.5,
        signup_url="https://console.groq.com/keys",
        extra_body_by_model_substring={"gpt-oss": {"reasoning_effort": "low"}},
    ),
    # Free-Plan: 10 $ Guthaben/Monat - bei diesem Volumen ~1,50 $/Monat Verbrauch.
    "mistral": Preset(
        env_prefix="MISTRAL",
        base_url="https://api.mistral.ai/v1",
        default_model="mistral-small-latest",
        min_interval_seconds=1.2,
        signup_url="https://console.mistral.ai/api-keys",
    ),
    # 10.000 "Neurons"/Tag, harter Stopp im Free-Plan. Die URL enthaelt die
    # Account-ID (CLOUDFLARE_ACCOUNT_ID), sonst gilt der Anbieter als nicht konfiguriert.
    "cloudflare": Preset(
        env_prefix="CLOUDFLARE",
        base_url="https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        default_model="@cf/google/gemma-4-26b-a4b-it",
        min_interval_seconds=1.0,
        signup_url="https://dash.cloudflare.com/profile/api-tokens",
    ),
    # 20/Minute, nur 50/Tag (1000/Tag nach einmalig 10 $ Guthaben). Die Liste der
    # ":free"-Modelle wechselt fast woechentlich - daher nur als spaeter Ausweg.
    "openrouter": Preset(
        env_prefix="OPENROUTER",
        base_url="https://openrouter.ai/api/v1",
        default_model="google/gemma-4-31b-it:free",
        min_interval_seconds=3.5,
        signup_url="https://openrouter.ai/keys",
    ),
    # Beliebiger weiterer OpenAI-kompatibler Endpunkt, z.B. selbst gehostetes Ollama
    # (CUSTOM_LLM_BASE_URL=http://<host>:11434/v1). Kein Key noetig, dafuer muessen
    # BASE_URL und MODEL gesetzt sein. Laengerer Timeout: CPU-Inferenz ist langsam.
    "custom": Preset(
        env_prefix="CUSTOM_LLM",
        base_url="",
        default_model="",
        min_interval_seconds=0.0,
        signup_url="",
        key_required=False,
        timeout_seconds=180.0,
    ),
}

# Reihenfolge, wenn LLM_PROVIDERS nicht gesetzt ist: kostenlose Anbieter zuerst,
# Anthropic (kostenpflichtig) nur als letzter Ausweg. Ein Eintrag ist entweder ein
# Anbieter ("groq", Modell aus GROQ_MODEL bzw. Voreinstellung) oder "anbieter:modell"
# - so kann derselbe Anbieter mit einem zweiten Modell ein zweites, eigenes
# Tageskontingent beisteuern (bei Groq gelten die Limits pro Modell).
DEFAULT_ORDER = [
    "gemini",
    "groq",
    "groq:openai/gpt-oss-20b",
    "mistral",
    "cloudflare",
    "openrouter",
    "custom",
    "anthropic",
]
ANTHROPIC = "anthropic"

# Fehlerarten - bestimmen Cooldown und ob ein Fehler "permanent" ist.
RATE_LIMIT = "rate_limit"          # Minutenlimit - erholt sich von selbst
QUOTA = "quota"                    # Tageslimit/Guthaben leer - erst spaeter wieder
AUTH = "auth"                      # Key ungueltig/gesperrt - repariert sich nicht
NOT_FOUND = "not_found"            # Modell existiert nicht (mehr)
BAD_REQUEST = "bad_request"        # Anfrage abgelehnt (z.B. Feature nicht unterstuetzt)
SERVER = "server"                  # 5xx beim Anbieter
NETWORK = "network"                # Timeout/Verbindungsfehler
INVALID_OUTPUT = "invalid_output"  # Antwort da, aber kein brauchbares JSON

PERMANENT_KINDS = {AUTH, NOT_FOUND}

# (Basis-Cooldown, Obergrenze) in Sekunden; verdoppelt sich je weiterem Fehlschlag
# in Folge. Tageslimit/Key-Fehler bewusst lang: sonst wuerde jeder Poll-Zyklus
# denselben aussichtslosen Request erneut schicken.
_COOLDOWNS = {
    RATE_LIMIT: (30.0, 900.0),
    QUOTA: (1800.0, 6 * 3600.0),
    AUTH: (3600.0, 6 * 3600.0),
    NOT_FOUND: (3600.0, 6 * 3600.0),
    BAD_REQUEST: (300.0, 3600.0),
    SERVER: (30.0, 600.0),
    NETWORK: (30.0, 600.0),
    INVALID_OUTPUT: (120.0, 1800.0),
}
# Einzelne unbrauchbare Antworten sind bei kleinen Modellen normal (Zufall) - erst ab
# so vielen in Folge wird der Anbieter pausiert.
_INVALID_OUTPUT_TOLERANCE = 3


class ProviderError(Exception):
    """Ein einzelner Anbieter ist bei EINER Anfrage gescheitert."""

    def __init__(self, kind: str, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


class LLMError(RuntimeError):
    """Kein Anbieter der Kette konnte liefern."""


class LLMUnavailable(LLMError):
    """Voruebergehend: alle Anbieter pausieren (Limits) oder sind gestoert. Erholt
    sich von selbst - das betroffene Statement wird diesen Zyklus uebersprungen."""


class LLMConfigError(LLMError):
    """Permanent: kein Anbieter konfiguriert, oder alle scheitern an Key/Modell.
    Repariert sich nicht von selbst - .env pruefen."""


@dataclass
class ProviderSpec:
    name: str
    base_url: str
    api_key: str
    model: str
    min_interval_seconds: float
    timeout_seconds: float
    extra_body: dict


@dataclass
class ProviderState:
    next_slot: float = 0.0          # time.monotonic() des naechsten erlaubten Starts
    cooldown_until: float = 0.0     # time.time()
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    last_error_kind: Optional[str] = None
    last_error_at: Optional[float] = None
    last_success_at: Optional[float] = None
    successes: int = 0
    failures: int = 0


_states: dict[str, ProviderState] = {}
_warned_unknown: set = set()


def reset_state() -> None:
    """Fuer Tests / manuellen Reset."""
    _states.clear()
    _warned_unknown.clear()


def state(name: str) -> ProviderState:
    st = _states.get(name)
    if st is None:
        st = ProviderState()
        _states[name] = st
    return st


def _env(name: str, default: str = "") -> str:
    # Zur Laufzeit gelesen (nicht beim Import): config.py hat .env bereits per
    # load_dotenv() in os.environ geladen; Tests koennen os.environ direkt setzen.
    return (os.getenv(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r ist keine Zahl - nutze Standard %s.", name, raw, default)
        return default


def parse_entry(entry: str) -> tuple[Optional[str], Optional[str]]:
    """Kettenglied -> (anbieter, modell-override). 'groq' -> ('groq', None),
    'groq:openai/gpt-oss-20b' -> ('groq', 'openai/gpt-oss-20b'), 'anthropic' ->
    ('anthropic', None), Unbekanntes -> (None, None). Nur der ERSTE Doppelpunkt
    trennt: OpenRouter-Modelle enden selbst auf ':free'."""
    entry = entry.strip()
    if entry.lower() == ANTHROPIC:
        return ANTHROPIC, None
    if entry.lower() in PRESETS:
        return entry.lower(), None
    prefix, sep, rest = entry.partition(":")
    if sep and prefix.lower() in PRESETS and rest.strip():
        return prefix.lower(), rest.strip()
    return None, None


def provider_spec(entry: str, model_override: Optional[str] = None) -> Optional[ProviderSpec]:
    """Aufgeloeste Konfiguration eines OpenAI-kompatiblen Kettenglieds, oder None, wenn
    es nicht (vollstaendig) konfiguriert ist."""
    name, entry_model = parse_entry(entry)
    preset = PRESETS.get(name) if name else None
    if preset is None:
        return None
    p = preset.env_prefix
    api_key = _env(f"{p}_API_KEY")
    base_url = _env(f"{p}_BASE_URL", preset.base_url)
    if "{account_id}" in base_url:
        account_id = _env(f"{p}_ACCOUNT_ID")
        if not account_id:
            return None
        base_url = base_url.replace("{account_id}", account_id)
    model = model_override or entry_model or _env(f"{p}_MODEL", preset.default_model)
    if preset.key_required and not api_key:
        return None
    if not base_url or not model:
        return None
    extra: dict = {}
    for substring, body in preset.extra_body_by_model_substring.items():
        if substring in model:
            extra.update(body)
    return ProviderSpec(
        name=name,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        min_interval_seconds=_env_float(f"{p}_MIN_INTERVAL_SECONDS", preset.min_interval_seconds),
        timeout_seconds=_env_float(f"{p}_TIMEOUT_SECONDS", preset.timeout_seconds),
        extra_body=extra,
    )


def configured_openai_providers() -> list[str]:
    return [name for name in PRESETS if provider_spec(name) is not None]


def chain_order(anthropic_available: bool) -> list[str]:
    """Die tatsaechlich nutzbare Kette in Reihenfolge (nur konfigurierte Glieder)."""
    raw = [p.strip() for p in _env("LLM_PROVIDERS").split(",") if p.strip()]
    order = raw or DEFAULT_ORDER
    result: list[str] = []
    seen_models: set = set()
    for entry in order:
        name, _model = parse_entry(entry)
        key = entry.lower() if name == ANTHROPIC or _model is None else f"{name}:{_model}"
        if key in result:
            continue
        if name == ANTHROPIC:
            if anthropic_available:
                result.append(key)
        elif name is not None:
            spec = provider_spec(key)
            # Dasselbe Modell zweimal (z.B. GROQ_MODEL=openai/gpt-oss-20b UND der
            # Eintrag "groq:openai/gpt-oss-20b") teilt sich EIN Kontingent - doppelt
            # in der Kette waere nur ein zweiter, sinnloser Versuch.
            if spec is not None and (name, spec.model) not in seen_models:
                seen_models.add((name, spec.model))
                result.append(key)
        elif entry not in _warned_unknown:
            _warned_unknown.add(entry)
            logger.warning(
                "LLM_PROVIDERS enthaelt unbekannten Anbieter %r - ignoriert. Bekannt: %s",
                entry, ", ".join(list(PRESETS) + [ANTHROPIC]),
            )
    return result


def resolve_model_spec(spec: str) -> tuple[str, str]:
    """Fuer die Grenzfall-Zweitmeinung (CLAUDE_ESCALATION_MODEL):
    'groq:openai/gpt-oss-120b' -> ('groq:openai/gpt-oss-120b', 'openai/gpt-oss-120b').
    Ohne bekanntes Anbieter-Praefix (z.B. 'claude-sonnet-5') -> Anthropic - so bleiben
    bestehende .env-Werte wie CLAUDE_ESCALATION_MODEL=claude-sonnet-5 gueltig."""
    spec = spec.strip()
    name, model = parse_entry(spec)
    if name and name != ANTHROPIC and model:
        return f"{name}:{model}", model
    return ANTHROPIC, spec


# --- Verfuegbarkeit / Cooldown ----------------------------------------------------------
def is_available(name: str, now: Optional[float] = None) -> bool:
    return (now if now is not None else time.time()) >= state(name).cooldown_until


def seconds_until_available(names: list[str]) -> Optional[float]:
    """Wartezeit bis der erste der Anbieter wieder frei ist (0 = sofort), None bei
    leerer Liste."""
    if not names:
        return None
    now = time.time()
    return max(0.0, min(state(n).cooldown_until for n in names) - now)


def all_permanently_broken(names: list[str]) -> bool:
    return bool(names) and all(state(n).last_error_kind in PERMANENT_KINDS for n in names)


def _redact(text: str, secret: Optional[str]) -> str:
    text = str(text or "")
    if secret and len(secret) >= 6:
        text = text.replace(secret, "***")
    # Zusaetzlich alles, was nach einem Bearer-/API-Key aussieht (Anbieter maskieren
    # teils nur halb, z.B. "sk-abc...xyz").
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", text)
    text = re.sub(r"\b(sk|gsk|csk|AIza)[A-Za-z0-9_\-]{8,}", "***", text)
    return text[:300]


def mark_success(name: str) -> None:
    st = state(name)
    st.consecutive_failures = 0
    st.cooldown_until = 0.0
    st.last_success_at = time.time()
    st.successes += 1


def mark_failure(name: str, err: ProviderError, secret: Optional[str] = None) -> float:
    """Merkt den Fehler und pausiert den Anbieter. Gibt den Cooldown (s) zurueck."""
    st = state(name)
    st.consecutive_failures += 1
    st.failures += 1
    st.last_error_kind = err.kind
    st.last_error = _redact(err.message, secret)
    st.last_error_at = time.time()

    base, cap = _COOLDOWNS.get(err.kind, (60.0, 900.0))
    if err.kind == INVALID_OUTPUT and st.consecutive_failures < _INVALID_OUTPUT_TOLERANCE:
        cooldown = 0.0
    elif err.retry_after is not None and err.retry_after > 0:
        # Ein vom Anbieter genannter Zeitpunkt ist praeziser als jede Schaetzung.
        cooldown = min(float(err.retry_after), cap)
    else:
        steps = st.consecutive_failures - 1
        if err.kind == INVALID_OUTPUT:
            steps = st.consecutive_failures - _INVALID_OUTPUT_TOLERANCE
        cooldown = min(base * (2 ** max(0, steps)), cap)
    st.cooldown_until = time.time() + cooldown

    log = logger.critical if err.kind in PERMANENT_KINDS else logger.warning
    log(
        "KI-Anbieter %s: %s (%s) - pausiert fuer %.0fs, naechster Anbieter uebernimmt.",
        name, st.last_error, err.kind, cooldown,
    )
    return cooldown


async def _pace(name: str, min_interval: float) -> None:
    """Reserviert den naechsten Startzeitpunkt fuer diesen Anbieter und wartet bis
    dahin. Ohne Lock: zwischen Lesen und Schreiben von next_slot liegt kein await,
    in asyncio also atomar - und ein Lock waere an eine einzelne Event-Loop gebunden."""
    if min_interval <= 0:
        return
    st = state(name)
    now = time.monotonic()
    start = max(now, st.next_slot)
    st.next_slot = start + min_interval
    if start > now:
        await asyncio.sleep(start - now)


# --- HTTP / Antwort-Auswertung ------------------------------------------------------
_PER_DAY = re.compile(r"per[\s_-]?day|perday|daily|\bTPD\b|\bRPD\b|free-models-per-day", re.IGNORECASE)
_GONE_MODEL = re.compile(r"decommission|no longer (?:supported|available)|model_not_found|does not exist", re.IGNORECASE)
_NO_CREDIT = re.compile(r"credit|insufficient|billing|payment|usage limit", re.IGNORECASE)


def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    # Gemini nennt die Wartezeit nur im Body: "retryDelay": "41s"
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', resp.text or "")
    if m:
        return float(m.group(1))
    return None


def classify_http_error(status: int, body: str) -> str:
    body = body or ""
    if status == 429:
        return QUOTA if _PER_DAY.search(body) else RATE_LIMIT
    if status == 402:
        return QUOTA
    if status == 401:
        return AUTH
    if status == 403:
        return QUOTA if _NO_CREDIT.search(body) else AUTH
    if status == 404:
        return NOT_FOUND
    if status in (400, 422):
        return NOT_FOUND if _GONE_MODEL.search(body) else BAD_REQUEST
    if status in (408, 409) or status >= 500:
        return SERVER
    return BAD_REQUEST


def extract_json_object(content) -> Optional[dict]:
    """Holt das JSON-Objekt aus einer Modellantwort - auch wenn das Modell es in
    ```json-Bloecke packt oder einen Satz davor schreibt (kleine Modelle tun das
    trotz Anweisung). None, wenn nichts Parsebares drinsteht."""
    if isinstance(content, list):  # manche Anbieter liefern Content-Teile
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def _post(url: str, headers: dict, body: dict, timeout: float, client) -> httpx.Response:
    if client is not None:
        return await client.post(url, headers=headers, json=body, timeout=timeout)
    async with httpx.AsyncClient(timeout=timeout) as c:
        return await c.post(url, headers=headers, json=body)


async def chat_json(
    name: str,
    system: str,
    user: str,
    model_override: Optional[str] = None,
    max_tokens: int = 3000,
    client=None,
) -> tuple[dict, str]:
    """EINE Anfrage an EINEN OpenAI-kompatiblen Anbieter, JSON-Modus. Gibt
    (json_objekt, tatsaechliches_modell) zurueck oder wirft ProviderError. Cooldown/
    Failover entscheidet der Aufrufer (siehe app/classifier.py)."""
    spec = provider_spec(name, model_override)
    if spec is None:
        raise ProviderError(AUTH, f"{name} ist nicht konfiguriert (API-Key/URL/Modell fehlt)")

    await _pace(name, spec.min_interval_seconds)

    url = f"{spec.base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if spec.api_key:
        headers["Authorization"] = f"Bearer {spec.api_key}"
    body = {
        "model": spec.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        **spec.extra_body,
    }

    try:
        resp = await _post(url, headers, body, spec.timeout_seconds, client)
        if resp.status_code == 400 and not _GONE_MODEL.search(resp.text or ""):
            # Manche Modelle kennen keinen JSON-Modus oder keinen reasoning_effort
            # (v.a. wechselnde Gratis-Modelle). Einmal mit dem nackten Minimum - die
            # JSON-Anweisung steht ohnehin im Prompt, extract_json_object faengt
            # Markdown-Verpackung ab. Ist das Modell selbst weg, bringt das nichts.
            body = {k: v for k, v in body.items() if k in ("model", "messages", "temperature", "max_tokens")}
            resp = await _post(url, headers, body, spec.timeout_seconds, client)
    except httpx.TimeoutException as exc:
        raise ProviderError(NETWORK, f"Timeout nach {spec.timeout_seconds:.0f}s: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(NETWORK, f"Verbindungsfehler: {exc}") from exc

    if resp.status_code >= 400:
        kind = classify_http_error(resp.status_code, resp.text)
        raise ProviderError(
            kind,
            f"HTTP {resp.status_code}: {_redact(resp.text, spec.api_key)[:240]}",
            retry_after=_retry_after_seconds(resp),
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderError(INVALID_OUTPUT, "Antwort ist kein JSON") from exc
    if isinstance(data, dict) and data.get("error") and not data.get("choices"):
        # Manche Anbieter (OpenRouter) melden Fehler mit Status 200 im Body.
        err = data["error"]
        code = err.get("code") if isinstance(err, dict) else None
        msg = err.get("message") if isinstance(err, dict) else str(err)
        status = code if isinstance(code, int) else 500
        raise ProviderError(classify_http_error(status, str(msg)), f"Fehler im Body: {_redact(str(msg), spec.api_key)}")

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list):
        raise ProviderError(INVALID_OUTPUT, "Antwort ohne 'choices'")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    if choice.get("finish_reason") == "length":
        raise ProviderError(INVALID_OUTPUT, "Antwort bei max_tokens abgeschnitten - JSON unvollstaendig")
    message = choice.get("message") or {}
    parsed = extract_json_object(message.get("content"))
    if parsed is None:
        raise ProviderError(INVALID_OUTPUT, "kein gueltiges JSON-Objekt in der Antwort")
    return parsed, spec.model


# --- Status fuer /api/health ------------------------------------------------------
def provider_status(anthropic_available: bool, anthropic_model: str = "") -> list[dict]:
    """Oeffentlich sichtbarer Zustand der Kette - Namen, Modelle, Pausen, letzte
    Fehler (bereinigt). NIEMALS Keys."""
    now = time.time()
    out = []
    for name in chain_order(anthropic_available):
        st = state(name)
        if name == ANTHROPIC:
            model = anthropic_model
        else:
            spec = provider_spec(name)
            model = spec.model if spec else ""
        remaining = max(0.0, st.cooldown_until - now)
        out.append({
            "name": name,
            "model": model,
            "available": remaining == 0,
            "cooldown_remaining_seconds": round(remaining) if remaining else 0,
            "successes": st.successes,
            "failures": st.failures,
            "last_success_at": st.last_success_at,
            "last_error": st.last_error,
            "last_error_kind": st.last_error_kind,
            "last_error_at": st.last_error_at,
        })
    return out
