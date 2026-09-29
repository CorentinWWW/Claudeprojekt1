"""Tests fuer die Anbieter-Kette (app/llm.py + app/classifier.py): kostenlose,
OpenAI-kompatible KI-Anbieter statt nur Claude, mit automatischem Ausweichen.

Hintergrund: ohne Claude-Guthaben stand der komplette Monitor still (Start-Selbsttest
scheitert -> Loop startet nie). Gratis-Tarife haben enge Minuten- UND Tageslimits -
entscheidend ist deshalb, dass ein Limit/Ausfall EINES Anbieters sauber zum naechsten
fuehrt, ohne Statements zu verlieren oder Keys preiszugeben. Kein echtes Netz: der
HTTP-Aufruf (llm._post) wird durch eine Attrappe ersetzt.
"""
import asyncio
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []

_KEY_VARS = [
    "GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
    "CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CUSTOM_LLM_BASE_URL", "CUSTOM_LLM_MODEL",
    "LLM_PROVIDERS", "GROQ_MODEL", "GEMINI_MODEL",
]

GOOD_JSON = (
    '{"is_market_relevant": true, "sentiment": "negative", "confidence": 0.8, '
    '"ticker_calls": [{"ticker": "X", "direction": "short", "confidence": 0.85, "reasoning": "r"}], '
    '"sectors": ["Stahl"], "reasoning": "Zoelle.", "related_topic_id": null, '
    '"is_major_escalation": false, "expected_move_pct": 3, "expected_horizon": "Tage"}'
)


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _fresh(env: dict):
    """Frische DB + Module, nur die angegebenen Anbieter-Keys gesetzt, kein Pacing."""
    for var in _KEY_VARS:
        os.environ.pop(var, None)
    for prefix in ("GEMINI", "GROQ", "MISTRAL", "OPENROUTER", "CLOUDFLARE", "CUSTOM_LLM"):
        os.environ[f"{prefix}_MIN_INTERVAL_SECONDS"] = "0"
    os.environ.update(env)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.llm as llm
    llm.reset_state()
    import app.classifier as clf
    importlib.reload(clf)
    clf._client = None  # Claude nur, wo ein Test es ausdruecklich einhaengt
    return llm, clf, db


def _resp(status, *, json_body=None, text=None, headers=None, url="https://x/chat/completions"):
    req = httpx.Request("POST", url)
    if json_body is not None:
        return httpx.Response(status, json=json_body, headers=headers or {}, request=req)
    return httpx.Response(status, text=text or "", headers=headers or {}, request=req)


def _ok(content):
    return _resp(200, json_body={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})


class FakeHttp:
    """Ersetzt llm._post. behaviors: Liste von (Teilstring in URL oder Modell, Funktion),
    die erste passende Funktion liefert die Response."""

    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.calls = []

    async def __call__(self, url, headers, body, timeout, client):
        self.calls.append({"url": url, "headers": headers, "body": dict(body)})
        key = f"{url} {body.get('model')}"
        for needle, fn in self.behaviors:
            if needle in key:
                return fn(body)
        raise AssertionError(f"unerwarteter Aufruf: {key}")


def _run(coro):
    return asyncio.run(coro)


# --- Reine Hilfsfunktionen ------------------------------------------------------------
def test_json_extraction():
    llm, _clf, _db = _fresh({})
    check("JSON: pures Objekt", llm.extract_json_object('{"a": 1}') == {"a": 1})
    check("JSON: in ```json-Block verpackt",
          llm.extract_json_object('```json\n{"a": 2}\n```') == {"a": 2})
    check("JSON: Satz davor und danach",
          llm.extract_json_object('Hier das Ergebnis: {"a": 3} Ende.') == {"a": 3})
    check("JSON: Content als Liste von Teilen",
          llm.extract_json_object([{"type": "text", "text": '{"a": 4}'}]) == {"a": 4})
    check("JSON: Muell -> None", llm.extract_json_object("kein json hier") is None)
    check("JSON: Array statt Objekt -> None", llm.extract_json_object("[1, 2]") is None)
    check("JSON: leer -> None", llm.extract_json_object("") is None)


def test_http_error_classification():
    llm, _clf, _db = _fresh({})
    c = llm.classify_http_error
    check("429 Minutenlimit -> rate_limit",
          c(429, '{"error": "Rate limit reached ... requests per minute"}') == llm.RATE_LIMIT)
    check("429 Groq Tokens/Tag -> quota",
          c(429, "Rate limit reached for model on tokens per day (TPD): Limit 200000") == llm.QUOTA)
    check("429 Gemini PerDay-Quota -> quota",
          c(429, '"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"') == llm.QUOTA)
    check("429 OpenRouter free-models-per-day -> quota",
          c(429, "Rate limit exceeded: free-models-per-day") == llm.QUOTA)
    check("402 -> quota", c(402, "") == llm.QUOTA)
    check("401 -> auth", c(401, "") == llm.AUTH)
    check("403 ohne Guthaben-Hinweis -> auth", c(403, "forbidden") == llm.AUTH)
    check("404 -> not_found", c(404, "") == llm.NOT_FOUND)
    check("400 'decommissioned' -> not_found (Modell weg, nicht Anfrage kaputt)",
          c(400, "The model `x` has been decommissioned") == llm.NOT_FOUND)
    check("400 sonst -> bad_request", c(400, "invalid param") == llm.BAD_REQUEST)
    check("503 -> server", c(503, "") == llm.SERVER)


def test_chain_order():
    llm, _clf, _db = _fresh({})
    check("ohne Keys: leere Kette", llm.chain_order(anthropic_available=False) == [])
    check("nur Claude: Kette = [anthropic] (alter Betriebsmodus)",
          llm.chain_order(anthropic_available=True) == ["anthropic"])

    llm, _clf, _db = _fresh({"GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "GEMINI_API_KEY": "AIzaBBBBBBBBBB"})
    order = llm.chain_order(anthropic_available=True)
    check("Default: Gemini zuerst, dann beide Groq-Modelle (eigene Kontingente), Claude zuletzt",
          order == ["gemini", "groq", "groq:openai/gpt-oss-20b", "anthropic"])

    llm, _clf, _db = _fresh({
        "GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "GROQ_MODEL": "openai/gpt-oss-20b",
    })
    check("gleiches Modell doppelt (GROQ_MODEL = zweiter Eintrag) -> nur einmal in der Kette",
          llm.chain_order(anthropic_available=False) == ["groq"])

    llm, _clf, _db = _fresh({
        "GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "GEMINI_API_KEY": "AIzaBBBBBBBBBB",
        "LLM_PROVIDERS": "groq, nichtda, gemini",
    })
    check("LLM_PROVIDERS bestimmt die Reihenfolge, Unbekanntes wird ignoriert",
          llm.chain_order(anthropic_available=False) == ["groq", "gemini"])

    llm, _clf, _db = _fresh({"CLOUDFLARE_API_KEY": "cf_token_1234567"})
    check("Cloudflare ohne CLOUDFLARE_ACCOUNT_ID gilt als nicht konfiguriert",
          llm.chain_order(anthropic_available=False) == [])
    os.environ["CLOUDFLARE_ACCOUNT_ID"] = "acc123"
    spec = llm.provider_spec("cloudflare")
    check("Cloudflare: Account-ID landet in der URL",
          spec is not None and "/accounts/acc123/ai/v1" in spec.base_url)


def test_model_spec_resolution():
    llm, _clf, _db = _fresh({})
    check("claude-sonnet-5 (alter .env-Wert) -> Anthropic",
          llm.resolve_model_spec("claude-sonnet-5") == ("anthropic", "claude-sonnet-5"))
    check("groq:openai/gpt-oss-120b -> Groq mit diesem Modell",
          llm.resolve_model_spec("groq:openai/gpt-oss-120b")
          == ("groq:openai/gpt-oss-120b", "openai/gpt-oss-120b"))
    check("OpenRouter-Modell mit eigenem ':free' bleibt vollstaendig",
          llm.parse_entry("openrouter:google/gemma-4-31b-it:free")
          == ("openrouter", "google/gemma-4-31b-it:free"))


def test_output_instructions_cover_schema():
    _llm, clf, _db = _fresh({})
    props = clf.CLASSIFY_TOOL["input_schema"]["properties"]
    missing = [p for p in props if f'"{p}"' not in clf.JSON_OUTPUT_INSTRUCTIONS]
    check(f"Kompakte JSON-Anweisung nennt jedes Schema-Feld (fehlend: {missing})", not missing)


# --- Failover im echten classify()-Pfad -------------------------------------------------
def test_failover_on_rate_limit():
    llm, clf, db = _fresh({"GEMINI_API_KEY": "AIzaBBBBBBBBBB", "GROQ_API_KEY": "gsk_aaaaaaaaaaaa"})
    fake = FakeHttp([
        ("generativelanguage", lambda b: _resp(
            429, text='{"error": {"details": [{"retryDelay": "40s"}]}}')),
        ("api.groq.com", lambda b: _ok(GOOD_JSON)),
    ])
    llm._post = fake
    result = _run(clf.classify("Zoelle auf Stahl"))
    check("Gemini im Minutenlimit -> Groq liefert das Ergebnis", result.is_market_relevant is True)
    check("model_used nennt den tatsaechlich liefernden Anbieter",
          result.model_used == "groq:openai/gpt-oss-120b")
    remaining = llm.state("gemini").cooldown_until - time.time()
    check("Gemini pausiert genau die genannte retryDelay (~40s)", 35 < remaining <= 40.5)
    check("Tages-Kostendeckel: genau EIN Slot verbraucht trotz zwei Anbietern",
          db.get_classification_calls_today() == 1)

    # Naechster Aufruf: Gemini pausiert -> wird gar nicht erst angefragt.
    fake.calls.clear()
    _run(clf.classify("Noch eine Meldung"))
    check("pausierter Anbieter wird uebersprungen (kein Request an Gemini)",
          all("generativelanguage" not in c["url"] for c in fake.calls))


def test_all_quota_exhausted_raises_transient():
    llm, clf, _db = _fresh({"GEMINI_API_KEY": "AIzaBBBBBBBBBB", "GROQ_API_KEY": "gsk_aaaaaaaaaaaa"})
    llm._post = FakeHttp([
        ("generativelanguage", lambda b: _resp(429, text="quota exceeded PerDay")),
        ("api.groq.com", lambda b: _resp(429, text="tokens per day (TPD)")),
    ])
    t0 = time.monotonic()
    try:
        _run(clf.classify("x"))
        raised = None
    except Exception as exc:  # noqa: BLE001 - Typ wird unten geprueft
        raised = exc
    check("alle im Tageslimit -> LLMUnavailable (voruebergehend, nicht permanent)",
          isinstance(raised, llm.LLMUnavailable) and not isinstance(raised, llm.LLMConfigError))
    check("Tageslimit wird NICHT abgewartet (sofort aufgeben statt 30 Min blockieren)",
          time.monotonic() - t0 < 5)
    check("Tageslimit -> lange Pause (>= 30 Min)",
          llm.state("groq").cooldown_until - time.time() > 1700)


def test_all_auth_broken_is_permanent():
    llm, clf, _db = _fresh({"GEMINI_API_KEY": "AIzaBBBBBBBBBB", "GROQ_API_KEY": "gsk_aaaaaaaaaaaa"})
    llm._post = FakeHttp([("", lambda b: _resp(401, text="invalid api key"))])
    try:
        _run(clf.classify("x"))
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("alle Keys ungueltig -> LLMConfigError (permanent -> Monitor meldet es laut)",
          isinstance(raised, llm.LLMConfigError))
    import app.orchestrator as orch
    check("LLMConfigError zaehlt im Orchestrator als permanenter Fehler",
          isinstance(raised, orch.PERMANENT_CLAUDE_ERRORS))


def test_short_rate_limit_is_waited_out():
    llm, clf, _db = _fresh({"GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "LLM_PROVIDERS": "groq"})
    responses = [_resp(429, text="per minute", headers={"retry-after": "0.2"}), _ok(GOOD_JSON)]
    llm._post = FakeHttp([("api.groq.com", lambda b: responses.pop(0))])
    result = _run(clf.classify("x"))
    check("kurzes Minutenlimit wird abgewartet statt das Statement zu verlieren",
          result.is_market_relevant is True)


def test_sloppy_json_from_small_models():
    llm, clf, _db = _fresh({"GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "LLM_PROVIDERS": "groq"})
    sloppy = (
        "Klar, hier ist die Analyse:\n```json\n"
        '{"is_market_relevant": "false", "sentiment": "Bearish", "confidence": "0.4", '
        '"ticker_calls": [{"ticker": "$xom", "direction": "Short", "confidence": 0.7, "reasoning": "r"}], '
        '"sectors": "Energie", "reasoning": "r", "related_topic_id": "12", '
        '"is_major_escalation": "no"}\n```'
    )
    llm._post = FakeHttp([("api.groq.com", lambda b: _ok(sloppy))])
    r = _run(clf.classify("x"))
    check("'false' als Text wird False (bool('false') waere True!)", r.is_market_relevant is False)
    check("'Bearish' -> negative", r.sentiment == "negative")
    check("'Short' -> short (sonst verwirft actionable_tickers den Ticker still)",
          r.ticker_calls and r.ticker_calls[0]["direction"] == "short")
    check("'$xom' -> XOM", r.ticker_calls and r.ticker_calls[0]["ticker"] == "XOM")
    check("sectors als einzelner String -> Liste (nicht Zeichen fuer Zeichen)", r.sectors == ["Energie"])
    check("related_topic_id als Text '12' -> 12", r.related_topic_id == 12)
    check("'no' -> is_major_escalation False", r.is_major_escalation is False)


def test_unsupported_json_mode_retried_plain():
    llm, clf, _db = _fresh({"OPENROUTER_API_KEY": "sk-or-aaaaaaaaaa", "LLM_PROVIDERS": "openrouter"})
    fake = FakeHttp([("openrouter.ai", lambda b: (
        _resp(400, text="response_format is not supported by this model")
        if "response_format" in b else _ok(GOOD_JSON)
    ))])
    llm._post = fake
    r = _run(clf.classify("x"))
    check("Modell ohne JSON-Modus: zweiter Versuch ohne response_format liefert",
          r.is_market_relevant is True and len(fake.calls) == 2)
    check("zweiter Versuch enthaelt wirklich kein response_format",
          "response_format" not in fake.calls[1]["body"])


def test_invalid_json_everywhere_falls_back():
    llm, clf, _db = _fresh({"GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "LLM_PROVIDERS": "groq"})
    llm._post = FakeHttp([("api.groq.com", lambda b: _ok("Ich kann das nicht beurteilen."))])
    r = _run(clf.classify("x"))
    check("nur unbrauchbare Antworten -> FALLBACK (neutral) statt Exception",
          r is clf.FALLBACK_CLASSIFICATION)


def test_truncated_output_is_not_trusted():
    llm, clf, _db = _fresh({"GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "LLM_PROVIDERS": "groq"})
    llm._post = FakeHttp([("api.groq.com", lambda b: _resp(200, json_body={
        "choices": [{"finish_reason": "length", "message": {"content": '{"is_market_relevant": true'}}]}))])
    r = _run(clf.classify("x"))
    check("bei max_tokens abgeschnittene Antwort wird nicht als Ergebnis genommen",
          r is clf.FALLBACK_CLASSIFICATION)


def test_pacing_spaces_requests():
    llm, clf, _db = _fresh({"GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "LLM_PROVIDERS": "groq"})
    os.environ["GROQ_MIN_INTERVAL_SECONDS"] = "0.3"
    fake = FakeHttp([("api.groq.com", lambda b: _ok(GOOD_JSON))])
    llm._post = fake
    stamps = []
    orig = fake.__call__

    async def stamped(*a, **kw):
        stamps.append(time.monotonic())
        return await orig(*a, **kw)

    llm._post = stamped

    async def two():
        await asyncio.gather(clf.classify("a"), clf.classify("b"))

    _run(two())
    check("Pacing: zweiter Request frühestens nach dem Mindestabstand (auch parallel)",
          len(stamps) == 2 and stamps[1] - stamps[0] >= 0.28)


def test_keys_never_leak_into_status():
    secret = "gsk_SUPERSECRET123456"
    llm, clf, _db = _fresh({"GROQ_API_KEY": secret, "LLM_PROVIDERS": "groq"})
    llm._post = FakeHttp([("api.groq.com", lambda b: _resp(
        401, text=f"Invalid API Key provided: {secret}"))])
    try:
        _run(clf.classify("x"))
    except Exception:  # noqa: BLE001 - hier zaehlt nur der Status danach
        pass
    status = llm.provider_status(anthropic_available=False)
    check("Fehlertext im Status ist um den Key bereinigt (oeffentliches /api/health)",
          secret not in str(status) and status and status[0]["last_error_kind"] == "auth")


def test_anthropic_only_mode_unchanged():
    """Nur Claude konfiguriert = alter Betriebsmodus: Original-Exceptions und
    Ergebnisse unveraendert, damit der Orchestrator 401/403/404 weiter als
    permanent erkennt."""
    llm, clf, _db = _fresh({})

    class Block:
        type = "tool_use"
        name = "classify_statement"
        input = {"is_market_relevant": True, "sentiment": "neutral", "confidence": 0.6,
                 "ticker_calls": [], "sectors": [], "reasoning": "x"}

    class Resp:
        stop_reason = "tool_use"
        content = [Block()]

    class Messages:
        async def create(self, **kw):
            return Resp()

    class Client:
        messages = Messages()

    clf._client = Client()
    r = _run(clf.classify("x"))
    check("nur Claude: Ergebnis kommt wie bisher, model_used = CLAUDE_MODEL",
          r.is_market_relevant is True and r.model_used == clf.CLAUDE_MODEL)

    from anthropic import AuthenticationError
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    class BadMessages:
        async def create(self, **kw):
            raise AuthenticationError(message="invalid x-api-key",
                                      response=httpx.Response(401, request=req), body=None)

    class BadClient:
        messages = BadMessages()

    llm.reset_state()
    clf._client = BadClient()
    try:
        _run(clf.classify("x"))
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("nur Claude: AuthenticationError kommt unveraendert beim Aufrufer an",
          isinstance(raised, AuthenticationError))


def test_anthropic_without_credit_falls_through_to_free():
    llm, clf, _db = _fresh({"GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "LLM_PROVIDERS": "anthropic,groq"})
    from anthropic import BadRequestError
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    class NoCreditMessages:
        async def create(self, **kw):
            raise BadRequestError(
                message="Your credit balance is too low to access the Anthropic API.",
                response=httpx.Response(400, request=req), body=None,
            )

    class NoCreditClient:
        messages = NoCreditMessages()

    clf._client = NoCreditClient()
    llm._post = FakeHttp([("api.groq.com", lambda b: _ok(GOOD_JSON))])
    r = _run(clf.classify("x"))
    check("Claude ohne Guthaben -> Gratis-Anbieter uebernimmt", r.model_used.startswith("groq:"))
    check("Claude ohne Guthaben gilt als Tages-/Kontingentproblem (lange Pause, nicht jeder Call)",
          llm.state("anthropic").last_error_kind == llm.QUOTA
          and llm.state("anthropic").cooldown_until - time.time() > 1700)


def test_escalation_availability():
    llm, clf, _db = _fresh({"GROQ_API_KEY": "gsk_aaaaaaaaaaaa"})
    check("Eskalation auf claude-sonnet-5 ohne Claude-Key: nicht verfuegbar (still auslassen)",
          clf.model_available("claude-sonnet-5") is False)
    check("Eskalation auf konfigurierten Gratis-Anbieter: verfuegbar",
          clf.model_available("groq:openai/gpt-oss-120b") is True)
    try:
        _run(clf.classify("x", model="claude-sonnet-5"))
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("Aufruf mit nicht konfiguriertem Eskalations-Modell -> LLMUnavailable, kein Tages-Slot",
          isinstance(raised, llm.LLMUnavailable) and _db.get_classification_calls_today() == 0)


def test_selftest_modes():
    llm, clf, _db = _fresh({})
    try:
        _run(clf.selftest())
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("Selftest ohne jeden Anbieter: harter Fehler (Loop startet nicht)",
          isinstance(raised, RuntimeError) and not isinstance(raised, clf.SelftestTransientError)
          and "GEMINI_API_KEY" in str(raised))

    llm, clf, _db = _fresh({"GROQ_API_KEY": "gsk_aaaaaaaaaaaa", "LLM_PROVIDERS": "groq"})
    llm._post = FakeHttp([("api.groq.com", lambda b: _resp(429, text="tokens per day"))])
    try:
        _run(clf.selftest())
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("Selftest mit Anbieter im Tageslimit: nur voruebergehend (Loop startet trotzdem)",
          isinstance(raised, clf.SelftestTransientError))


def main():
    test_json_extraction()
    test_http_error_classification()
    test_chain_order()
    test_model_spec_resolution()
    test_output_instructions_cover_schema()
    test_failover_on_rate_limit()
    test_all_quota_exhausted_raises_transient()
    test_all_auth_broken_is_permanent()
    test_short_rate_limit_is_waited_out()
    test_sloppy_json_from_small_models()
    test_unsupported_json_mode_retried_plain()
    test_invalid_json_everywhere_falls_back()
    test_truncated_output_is_not_trusted()
    test_pacing_spaces_requests()
    test_keys_never_leak_into_status()
    test_anthropic_only_mode_unchanged()
    test_anthropic_without_credit_falls_through_to_free()
    test_escalation_availability()
    test_selftest_modes()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
