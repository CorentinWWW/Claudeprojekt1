"""Tests fuer die Zweitmeinung bei Grenzfaellen (#4): nur wenn die staerkste
Ticker-Konfidenz knapp um die Schwelle liegt, wird mit dem staerkeren Modell erneut
klassifiziert; sonst bleibt es bei der Erstbewertung. Best-effort bei Tages-Limit."""
import asyncio
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def _reload(enabled=True, band="0.1"):
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["ENABLE_BORDERLINE_ESCALATION"] = "true" if enabled else "false"
    os.environ["CLAUDE_ESCALATION_MODEL"] = "claude-sonnet-5"
    os.environ["ESCALATION_BAND"] = band
    import app.config as config
    importlib.reload(config)
    import app.orchestrator as orch
    importlib.reload(orch)
    return orch


def _cls(conf):
    from app.db import Classification
    return Classification(is_market_relevant=True, sentiment="negative", confidence=conf,
                          ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": conf}])


def main():
    orch = _reload(enabled=True)

    calls = []

    async def fake_classify(text, recent_context=None, priority=False, model=None, _bypass_daily_cap=False):
        calls.append(model)
        # Zweitmeinung liefert ein klar unterscheidbares Ergebnis.
        return _cls(0.99)

    orig = orch.classify
    orch.classify = fake_classify
    try:
        # In-Band (0.85 liegt in [0.8, 1.0]) -> Zweitmeinung mit Eskalations-Modell.
        result = asyncio.run(orch._maybe_escalate("t", [], False, _cls(0.85)))
        check("In-Band: Zweitmeinung wurde geholt", calls == ["claude-sonnet-5"])
        check("In-Band: Zweitmeinung-Ergebnis wird uebernommen (conf 0.99)",
              result.confidence == 0.99)

        # Out-of-Band (0.5) -> keine Zweitmeinung, Original bleibt.
        calls.clear()
        original = _cls(0.5)
        result2 = asyncio.run(orch._maybe_escalate("t", [], False, original))
        check("Out-of-Band: keine Zweitmeinung", calls == [])
        check("Out-of-Band: Original unveraendert", result2 is original)
    finally:
        orch.classify = orig

    # Tages-Limit bei der Zweitmeinung -> Erstbewertung behalten (kein Crash).
    async def capped_classify(text, recent_context=None, priority=False, model=None, _bypass_daily_cap=False):
        raise orch.DailyCapExceeded("Limit")

    orch.classify = capped_classify
    try:
        original = _cls(0.88)
        result3 = asyncio.run(orch._maybe_escalate("t", [], False, original))
        check("Tages-Limit: Erstbewertung bleibt erhalten", result3 is original)
    finally:
        orch.classify = orig

    # Deaktiviert -> nie eine Zweitmeinung, egal wie nah an der Schwelle.
    orch = _reload(enabled=False)
    calls2 = []

    async def fake2(text, recent_context=None, priority=False, model=None, _bypass_daily_cap=False):
        calls2.append(model)
        return _cls(0.99)

    orig2 = orch.classify
    orch.classify = fake2
    try:
        original = _cls(0.9)
        result4 = asyncio.run(orch._maybe_escalate("t", [], False, original))
        check("Deaktiviert: keine Zweitmeinung", calls2 == [])
        check("Deaktiviert: Original bleibt", result4 is original)
    finally:
        orch.classify = orig2

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
