"""Tests fuer den billigen, Claude-freien Relevanz-Vorfilter (app/prefilter.py) und
seine Einbindung im Orchestrator.

Der Vorfilter ist bewusst KONSERVATIV: er darf ausschliesslich offensichtliche
Nicht-Ereignisse (Listicles/Ratgeber/Personal-Finance-Clickbait) verwerfen und muss
jedes plausible echte Marktereignis durchlassen - ein faelschlich verworfenes echtes
Ereignis waere fuer immer verloren, ein faelschlich durchgelassenes kostet nur einen
Claude-Call und wird danach sauber als nicht relevant klassifiziert.
"""
import asyncio
import importlib
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


# Echte, diskrete Marktereignisse - muessen ALLE durchkommen (looks_market_relevant True).
REAL_EVENTS = [
    "Fed cuts interest rates by 25 basis points",
    "Trump announces 25% tariff on imported semiconductors",
    "Nvidia reports record Q3 earnings, beats estimates",
    "Apple recalls iPhone batteries over safety concern",
    "ECB signals it may hold rates amid inflation worries",
    "Boeing wins $10 billion defense contract",
    "US inflation rises to 3.4% in latest CPI report",
    "Microsoft to acquire gaming studio for $5 billion",
    "Oil prices surge after OPEC production cut",
    "Tesla stock plunges after disappointing delivery numbers",
    "Bundesbank warnt vor Rezession in Deutschland",  # deutschsprachig, kein Muster
]

# Offensichtliche Nicht-Ereignisse / Clickbait - muessen ALLE verworfen werden (False).
NON_EVENTS = [
    "5 stocks to watch this week",
    "How to invest in your 20s",
    "3 dividend stocks to buy now",
    "The Motley Fool: Is Apple a buy?",
    "Here is why you should max out your 401k",
    "Here's how to save for retirement",
    "Best 10 credit cards of 2026",
    "What to know about retirement planning",
    "Prime Day deals: save on electronics",
    "A beginner's guide to index funds",
    "Personal finance tips for millennials",
    "7 charts that explain the market",
    "Top 5 ETFs for beginners",
]


def test_pure_filter():
    from app.prefilter import looks_market_relevant

    for h in REAL_EVENTS:
        check(f"behaelt echtes Ereignis: {h[:45]!r}", looks_market_relevant(h) is True)
    for h in NON_EVENTS:
        check(f"verwirft Nicht-Ereignis: {h[:45]!r}", looks_market_relevant(h) is False)

    check("leerer Text -> False", looks_market_relevant("") is False)
    check("None -> False", looks_market_relevant(None) is False)
    check("nur Whitespace -> False", looks_market_relevant("   ") is False)
    # 'here' als blosses Wort mitten im Satz darf NICHT verwerfen (kein Fehlalarm).
    check("blosses 'here' im Satz verwirft nicht",
          looks_market_relevant("The company here posted record profit") is True)


def _run_poll_with_prefilter(enabled: bool):
    """Fuehrt EINEN poll_once-Zyklus mit frischer DB aus und gibt (klassifizierte
    Texte, prefiltered-Zaehler) zurueck. Frische DB pro Aufruf, damit der Tier-1-
    Text-Dedup nicht ueber zwei Szenarien hinweg wirkt (dieselbe Ueberschrift waere
    sonst im zweiten Lauf ein Duplikat des ersten)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["ENABLE_PREFILTER"] = "true" if enabled else "false"
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)

    orch.source_health["fake"] = {
        "last_poll_at": None, "last_success_at": None, "last_error": None,
        "last_error_at": None, "total_fetched": 0, "prefiltered": 0,
    }

    class FakeSource:
        name = "fake"

        async def poll(self):
            return [
                db.RawStatement(source="fake", source_id="1", text="Fed cuts interest rates by 25 basis points"),
                db.RawStatement(source="fake", source_id="2", text="5 stocks to watch this week"),
                db.RawStatement(source="fake", source_id="3", text="How to invest in your 20s"),
            ]

    classified = []

    async def fake_classify(text, recent_context=None, priority=False, model=None, _bypass_daily_cap=False):
        classified.append(text)
        return db.Classification(is_market_relevant=False, sentiment="neutral", confidence=0.1)

    orig = orch.classify
    orch.classify = fake_classify
    try:
        asyncio.run(orch.poll_once([FakeSource()], asyncio.Semaphore(1)))
    finally:
        orch.classify = orig

    prefiltered = orch.source_health["fake"]["prefiltered"]
    os.unlink(tmp.name)
    return classified, prefiltered


def test_orchestrator_integration():
    """poll_once muss vorgefilterte Meldungen verwerfen, BEVOR classify() (und damit
    ein Cap-Slot) verbraucht wird, und den Zaehler in source_health fuehren."""
    classified, prefiltered = _run_poll_with_prefilter(enabled=True)
    check("nur die echte Meldung wird an Claude gegeben (2 Clickbait vorgefiltert)",
          classified == ["Fed cuts interest rates by 25 basis points"])
    check("prefiltered-Zaehler steht auf 2", prefiltered == 2)

    classified2, prefiltered2 = _run_poll_with_prefilter(enabled=False)
    check("deaktiviert: alle 3 Meldungen gehen an Claude", len(classified2) == 3)
    check("deaktiviert: prefiltered-Zaehler bleibt 0", prefiltered2 == 0)


def main():
    test_pure_filter()
    test_orchestrator_integration()

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
