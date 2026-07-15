"""Tests fuer die Prioritaets-Reserve oberhalb des normalen Tages-Limits.

Wichtige Meldungen (direkte Trump-Posts, harte Wirtschaftsthemen) duerfen ein
Extra-Kontingent (PRIORITY_CLASSIFICATIONS_PER_DAY) oberhalb von
MAX_CLASSIFICATIONS_PER_DAY nutzen, waehrend normale Meldungen ab MAX gesperrt
bleiben. So bleibt das Kostenlimit gedeckelt, ohne dass eine wirklich wichtige
Meldung an einem lauten Nachrichtentag stumm untergeht.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def _make_fake_client():
    class FakeContentBlock:
        type = "tool_use"
        name = "classify_statement"
        input = {
            "is_market_relevant": True,
            "sentiment": "neutral",
            "confidence": 0.6,
            "ticker_calls": [],
            "sectors": [],
            "reasoning": "x",
        }

    class FakeResponse:
        stop_reason = "tool_use"
        content = [FakeContentBlock()]

    class FakeMessages:
        async def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    return FakeClient()


def test_reserve():
    """MAX=2, PRIORITY=2 -> absolutes Tages-Maximum 4.

    - Normale Calls duerfen nur bis MAX (2).
    - Prioritaets-Calls duerfen bis MAX+PRIORITY (4).
    - Oberhalb des absoluten Maximums werden auch Prioritaets-Calls gesperrt.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["MAX_CLASSIFICATIONS_PER_DAY"] = "2"
    os.environ["PRIORITY_CLASSIFICATIONS_PER_DAY"] = "2"

    import importlib
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.classifier as clf
    importlib.reload(clf)
    clf._client = _make_fake_client()

    async def call(priority):
        try:
            await clf.classify("Testtext", priority=priority)
            return "ok"
        except clf.DailyCapExceeded:
            return "capped"

    async def run():
        return {
            "normal1": await call(False),
            "normal2": await call(False),
            "normal3": await call(False),  # MAX erreicht -> gesperrt
            "prio1": await call(True),     # Reserve: geht durch (Zaehler 2->3)
            "prio2": await call(True),     # Reserve: geht durch (Zaehler 3->4)
            "prio3": await call(True),     # absolutes Maximum erreicht -> gesperrt
        }

    r = asyncio.run(run())

    check("Normaler Call 1 geht durch", r["normal1"] == "ok")
    check("Normaler Call 2 geht durch", r["normal2"] == "ok")
    check("Normaler Call 3 wird bei MAX gesperrt", r["normal3"] == "capped")
    check("Prioritaets-Call 1 nutzt die Reserve (geht durch)", r["prio1"] == "ok")
    check("Prioritaets-Call 2 nutzt die Reserve (geht durch)", r["prio2"] == "ok")
    check("Prioritaets-Call 3 wird am absoluten Maximum gesperrt", r["prio3"] == "capped")
    check("Zaehler steht am Ende bei 4 (MAX+PRIORITY, kein Ueberschreiten)",
          db.get_classification_calls_today() == 4)

    # Fehlermeldung eines gesperrten Prioritaets-Calls nennt das hoehere Limit (4),
    # nicht das normale (2) - sonst waere die Meldung fuer den Nutzer irrefuehrend.
    async def capped_priority_message():
        try:
            await clf.classify("Testtext", priority=True)
            return None
        except clf.DailyCapExceeded as exc:
            return str(exc)

    msg = asyncio.run(capped_priority_message())
    check("Fehlermeldung des gesperrten Prio-Calls nennt das Reserve-Limit (4)",
          msg is not None and "(4)" in msg)

    os.unlink(tmp.name)


def test_priority_zero_disables_reserve():
    """PRIORITY=0 -> Reserve deaktiviert: auch Prioritaets-Calls enden hart bei MAX."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["MAX_CLASSIFICATIONS_PER_DAY"] = "1"
    os.environ["PRIORITY_CLASSIFICATIONS_PER_DAY"] = "0"

    import importlib
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.classifier as clf
    importlib.reload(clf)
    clf._client = _make_fake_client()

    async def call(priority):
        try:
            await clf.classify("Testtext", priority=priority)
            return "ok"
        except clf.DailyCapExceeded:
            return "capped"

    async def run():
        return [await call(True), await call(True)]

    r = asyncio.run(run())
    check("Bei PRIORITY=0 geht der erste Prio-Call durch", r[0] == "ok")
    check("Bei PRIORITY=0 wird der zweite Prio-Call bei MAX gesperrt", r[1] == "capped")

    os.unlink(tmp.name)


def test_is_high_priority():
    """Die billige, Claude-freie Wichtigkeits-Heuristik (Quelle + Signalwoerter)."""
    # orchestrator erst hier importieren, nachdem oben schon eine DB konfiguriert wurde.
    import importlib
    import app.orchestrator as orch
    importlib.reload(orch)
    is_high_priority = orch.is_high_priority

    def raw(text, source="news_gdelt"):
        return SimpleNamespace(source=source, text=text)

    # Quelle Truth Social ist IMMER prioritaer (Trumps eigene Worte).
    check("Truth-Social-Post ist immer prioritaer",
          is_high_priority(raw("irgendein harmloser Text", source="truth_social")) is True)

    # Harte Wirtschafts-Signalwoerter -> prioritaer (auch aus News).
    check("'tariff' -> prioritaer", is_high_priority(raw("New 25% tariff on steel imports")) is True)
    check("Deutsches 'Zoelle' -> prioritaer", is_high_priority(raw("Trump kuendigt neue Zoelle an")) is True)
    check("'sanctions' -> prioritaer", is_high_priority(raw("US imposes fresh sanctions on Russia")) is True)
    check("'interest rate' -> prioritaer", is_high_priority(raw("Calls for an interest rate cut")) is True)
    check("'executive order' -> prioritaer", is_high_priority(raw("Signs executive order on energy")) is True)
    check("'shutdown' -> prioritaer", is_high_priority(raw("Government shutdown looms")) is True)

    # Gross-/Kleinschreibung egal.
    check("Signalwort case-insensitive", is_high_priority(raw("TARIFF war escalates")) is True)

    # Kein Signalwort + normale Quelle -> nicht prioritaer.
    check("Harmlose News ist nicht prioritaer",
          is_high_priority(raw("Trump praises the crowd at a rally")) is False)
    check("Teilwort ('tariffs' in 'notariffsomething') matcht NICHT ueber Wortgrenze",
          is_high_priority(raw("This is a notariffsomething word")) is False)

    # Robustheit: leerer/None-Text crasht nicht.
    check("Leerer Text -> nicht prioritaer (kein Crash)", is_high_priority(raw("")) is False)
    check("None-Text -> nicht prioritaer (kein Crash)", is_high_priority(raw(None)) is False)
    check("Fehlendes source-Attribut crasht nicht",
          is_high_priority(SimpleNamespace(text="new tariff")) is True)


def main():
    test_reserve()
    test_priority_zero_disables_reserve()
    test_is_high_priority()

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
