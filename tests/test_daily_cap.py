"""Smoke-Test fuer den neuen taeglichen Klassifikations-Kostendeckel."""
import asyncio
import os
import sys
import tempfile

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def main():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["MAX_CLASSIFICATIONS_PER_DAY"] = "3"

    import importlib
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.classifier as clf
    importlib.reload(clf)

    check("Zaehler startet bei 0", db.get_classification_calls_today() == 0)

    class FakeContentBlock:
        type = "tool_use"
        name = "classify_statement"
        input = {
            "is_market_relevant": True, "sentiment": "neutral", "confidence": 0.6,
            "ticker_calls": [], "sectors": [], "reasoning": "x",
        }

    class FakeResponse:
        stop_reason = "tool_use"
        content = [FakeContentBlock()]

    class FakeMessages:
        async def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    clf._client = FakeClient()

    async def run():
        results = []
        for i in range(5):
            try:
                await clf.classify(f"Testtext {i}")
                results.append("ok")
            except clf.DailyCapExceeded:
                results.append("capped")
        return results

    results = asyncio.run(run())
    check("Erste 3 Calls gehen durch (Limit=3)", results[:3] == ["ok", "ok", "ok"])
    check("4. und 5. Call werden gecappt", results[3:] == ["capped", "capped"])
    check("Zaehler steht bei 3 (gecappte Calls zaehlen nicht mit)", db.get_classification_calls_today() == 3)

    # _bypass_daily_cap (selftest) soll trotz ausgeschoepftem Limit durchgehen, den
    # Call aber trotzdem zaehlen (sonst waere jeder Prozess-Neustart ein unsichtbarer,
    # nicht mitgezaehlter Kostenpunkt ausserhalb des Tages-Limits).
    async def run_bypass():
        return await clf.classify("Selftest-Text", _bypass_daily_cap=True)

    bypass_result = asyncio.run(run_bypass())
    check("_bypass_daily_cap funktioniert trotz ausgeschoepftem Limit", bypass_result.is_market_relevant is True)
    check("_bypass_daily_cap zaehlt den Call trotzdem mit (schliesst Kosten-Leak)", db.get_classification_calls_today() == 4)

    # reserve_classification_call_slot: atomarer Check+Insert - simuliert eine
    # "gleichzeitige" Anfrage aus einem zweiten Prozess kurz vor Erreichen des Limits.
    tmp2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp2.close()
    os.environ["DB_PATH"] = tmp2.name
    importlib.reload(config)
    importlib.reload(db)
    db.init_db()

    for _ in range(5):
        assert db.reserve_classification_call_slot(5) is True
    check("reserve_classification_call_slot: 6. Reservierung bei Limit=5 schlaegt fehl",
          db.reserve_classification_call_slot(5) is False)
    check("reserve_classification_call_slot: Zaehler bleibt bei 5 (kein Ueberschreiten)",
          db.get_classification_calls_today() == 5)

    os.unlink(tmp2.name)
    os.unlink(tmp.name)

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
