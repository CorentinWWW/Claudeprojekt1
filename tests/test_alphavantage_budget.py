"""Tests fuer die historische Alpha-Vantage-Quelle und das harte Kostenlimit.

Zwei Dinge stehen hier im Mittelpunkt:

1. Alpha Vantage antwortet bei Rate-Limit/ungueltigem Key mit HTTP 200 und einer
   Erklaerung im JSON-Body. Ginge das still als "keine Artikel" durch, saehe ein
   kaputter Lauf exakt aus wie ein leerer Zeitraum - genau die Verwechslung, die beim
   GDELT-Rate-Limit einen ganzen Tag gekostet hat.

2. Das Budget ist eine Zusage ueber echtes Geld. Es muss doppelt greifen (Planung UND
   Laufzeit) und die Stichprobe muss den GESAMTEN Zeitraum abdecken, nicht nur dessen
   Anfang. Kein Netz, keine echten Claude-Calls.
"""
import asyncio
import datetime
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

import app.backtest as bt
import app.sources.news_alphavantage as av
from app.db import Classification, RawStatement

failures = []


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _tmp_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return tmp.name


# --- Alpha-Vantage-Parser -------------------------------------------------------------
def test_zeitstempel_format_ohne_Z():
    """Alpha Vantage nutzt 'YYYYMMDDTHHMMSS' OHNE das 'Z', das GDELT anhaengt - der
    GDELT-Parser wuerde hier None liefern und jeden Artikel verwerfen."""
    ts = av.parse_time_published("20260715T133000")
    expected = datetime.datetime(
        2026, 7, 15, 13, 30, tzinfo=datetime.timezone.utc
    ).timestamp()
    check("Alpha-Vantage-Zeitstempel korrekt geparst", ts == expected)
    check("GDELT-Format (mit Z) wird hier NICHT stillschweigend akzeptiert",
          av.parse_time_published("20260715T133000Z") is None)
    check("Unsinn liefert None statt Absturz", av.parse_time_published("kaputt") is None)


def test_feed_parsing():
    data = {"feed": [
        {"title": "Fed hikes rates", "url": "https://x/1",
         "time_published": "20260715T133000", "source": "Reuters"},
        # ohne Zeitstempel -> unbrauchbar fuer eine historische Auswertung
        {"title": "Ohne Zeit", "url": "https://x/2", "source": "AP"},
        # ohne Titel -> nichts zu klassifizieren
        {"title": "", "url": "https://x/3", "time_published": "20260715T140000"},
    ]}
    out = av.parse_feed(data)
    check("nur der vollstaendige Artikel wird uebernommen", len(out) == 1)
    check("Text enthaelt Titel und Quelle (gleiches Format wie die Live-Quellen)",
          out[0].text == "Fed hikes rates (Quelle: Reuters)")
    check("source korrekt gesetzt", out[0].source == "news_alphavantage")


def test_200_mit_fehlertext_wird_zur_exception():
    """Die zentrale Falle: HTTP 200, aber im Body steht eine Erklaerung statt Daten."""
    for key in ("Note", "Information", "Error Message"):
        raised = False
        try:
            av._raise_if_api_message({key: "Rate limit reached"})
        except av.AlphaVantageError:
            raised = True
        check(f"'{key}' im Body wird zur Exception (nicht still zu 0 Artikeln)", raised)

    ok = True
    try:
        av._raise_if_api_message({"feed": []})
    except av.AlphaVantageError:
        ok = False
    check("normale (auch leere) Antwort wirft NICHT", ok)


def test_fehlender_key_wirft_sofort():
    orig = av.ALPHAVANTAGE_API_KEY
    av.ALPHAVANTAGE_API_KEY = ""
    try:
        raised = False
        try:
            asyncio.run(av.fetch_range(
                datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc),
            ))
        except av.AlphaVantageError as exc:
            raised = "ALPHAVANTAGE_API_KEY" in str(exc)
        check("ohne Key: klare Fehlermeldung statt stiller Leerlauf", raised)
    finally:
        av.ALPHAVANTAGE_API_KEY = orig


def test_kein_mehrfach_themenfilter_mehr():
    """Regressionstest fuer einen echten Live-Fund: mehrere kommagetrennte 'topics'
    werden von Alpha Vantage als UND behandelt (ein Artikel muesste zu ALLEN Themen
    gleichzeitig passen) - live gegen die echte API geprueft, ein Thema/kein Thema
    lieferten je 50 Treffer im selben Zeitraum, fuenf Themen kommagetrennt nur 2.
    DEFAULT_TOPICS muss deshalb None bleiben, und ein None-Wert darf NIE als woertlicher
    String 'None' in der Anfrage landen."""
    check("DEFAULT_TOPICS ist None (kein Mehrfach-Themenfilter mehr)",
          av.DEFAULT_TOPICS is None)
    check("kein bereits vorhandener Default enthaelt ein Komma (waere wieder UND-verknuepft)",
          not (av.DEFAULT_TOPICS and "," in av.DEFAULT_TOPICS))

    captured_urls = []

    class CapturingClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, *a, **kw):
            captured_urls.append(url)
            req = __import__("httpx").Request("GET", url)
            return __import__("httpx").Response(200, request=req, json={"feed": []})

    orig_key = av.ALPHAVANTAGE_API_KEY
    av.ALPHAVANTAGE_API_KEY = "dummy-key"
    try:
        asyncio.run(av.fetch_range(
            datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc),
            client=CapturingClient(),
        ))
    finally:
        av.ALPHAVANTAGE_API_KEY = orig_key

    check("tatsaechlicher Request enthaelt KEIN topics= (Standardaufruf ohne Filter)",
          captured_urls and "topics=" not in captured_urls[0])
    check("und erst recht kein woertliches 'topics=None'",
          captured_urls and "None" not in captured_urls[0])


# --- Budget ---------------------------------------------------------------------------
def _statements(n: int, day_offset_start: int = 0) -> list:
    base = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
    return [
        RawStatement(
            source="news_alphavantage", source_id=f"https://x/{i}",
            text=f"Marktmeldung Nummer {i} ueber Zinsen und Zoelle am Aktienmarkt.",
            published_at=(base + datetime.timedelta(days=day_offset_start + i)).timestamp(),
        )
        for i in range(n)
    ]


def test_budget_stichprobe_deckt_ganzen_zeitraum_ab():
    """Die entscheidende Eigenschaft: bei knappem Budget darf NICHT einfach vorne
    abgeschnitten werden - sonst deckt die Stichprobe nur die ersten Tage ab und die
    Auswertung waere systematisch auf eine einzelne Marktphase verzerrt."""
    stmts = _statements(300)
    per_call = bt.estimate_call_cost_usd(stmts[0].text)
    budget = per_call * 30  # reicht fuer ~27 Artikel (90% Sicherheitspuffer)

    picked, was_sampled = bt.sample_within_budget(stmts, budget)
    check("Stichprobe wurde gezogen", was_sampled is True)
    check("Stichprobe passt ins Budget",
          sum(bt.estimate_call_cost_usd(s.text) for s in picked) <= budget)
    check("Stichprobe ist deutlich kleiner als die Gesamtmenge", 0 < len(picked) < 60)

    # Zeitliche Streuung: erster und letzter Treffer duerfen nicht beide am Anfang liegen.
    idx = [int(s.source_id.rsplit("/", 1)[1]) for s in picked]
    check("Stichprobe reicht bis ins letzte Drittel des Zeitraums", max(idx) > 200)
    check("Stichprobe beginnt im ersten Drittel", min(idx) < 100)
    check("Ergebnis ist chronologisch sortiert", idx == sorted(idx))


def test_budget_reproduzierbar_und_abschaltbar():
    stmts = _statements(100)
    budget = bt.estimate_call_cost_usd(stmts[0].text) * 10
    a, _ = bt.sample_within_budget(stmts, budget)
    b, _ = bt.sample_within_budget(stmts, budget)
    check("gleicher Aufruf -> gleiche Stichprobe (fester Seed, nachvollziehbar)",
          [s.source_id for s in a] == [s.source_id for s in b])

    alle, sampled = bt.sample_within_budget(stmts, 0.0)
    check("Budget 0 = aus: nichts wird gekuerzt", len(alle) == 100 and sampled is False)

    grosszuegig, sampled2 = bt.sample_within_budget(stmts, 999.0)
    check("Budget reicht fuer alles: keine Stichprobe", len(grosszuegig) == 100 and sampled2 is False)


def test_budget_stoppt_auch_zur_laufzeit():
    """Zweite Reissleine: selbst wenn die Vorab-Schaetzung danebenliegt, muss der Lauf
    beim Budget abbrechen - es geht um echtes Geld, das der Nutzer nicht freigegeben hat."""
    orig_fetch, orig_classify = bt.fetch_all_raw, bt.classify
    calls = {"n": 0}

    topics = [
        "Federal Reserve raises rates sharply amid inflation concerns.",
        "Tariffs on steel imports spark a trade war between major economies.",
        "Chipmaker announces surprise merger with a large rival firm.",
        "Oil prices plunge after the OPEC cartel increases its output.",
        "Central bank governor hints at an emergency rate cut next quarter.",
        "Retailer reports a deep quarterly loss and announces mass layoffs.",
    ]

    async def fake_fetch_all_raw(start, end):
        base = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
        return ([
            RawStatement(source="news_alphavantage", source_id=f"https://x/{i}", text=t,
                         published_at=(base + datetime.timedelta(days=i)).timestamp())
            for i, t in enumerate(topics)
        ], 0, 1)

    async def fake_classify(text, recent_context=None, _bypass_daily_cap=False, **kw):
        calls["n"] += 1
        return Classification(is_market_relevant=False, sentiment="neutral", confidence=0.1)

    orig_sample = bt.sample_within_budget
    bt.fetch_all_raw = fake_fetch_all_raw
    bt.classify = fake_classify
    try:
        per_call = bt.estimate_call_cost_usd(topics[0])

        # Erst der Normalfall: die Vorab-Stichprobe deckelt bereits sauber.
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 5, 1), datetime.date(2026, 5, 6),
            execute=True, max_calls=0, budget_usd=per_call * 2.5, db_path=_tmp_db(),
        ))
        check("Vorab-Stichprobe deckelt: hoechstens 2 Calls trotz 6 Kandidaten",
              calls["n"] <= 2)
        check("tatsaechliche Kosten bleiben unter dem Budget",
              report.get("actual_cost_usd", 0) <= report.get("budget_usd", 0))

        # Jetzt die eigentliche Frage: greift die ZWEITE Reissleine auch dann, wenn die
        # Planung versagt? Genau dafuer ist sie da (zu niedrige Schaetzung). Die
        # Vorab-Stichprobe wird deshalb neutralisiert - sonst laesst sie den
        # Laufzeit-Stopp nie zum Zug kommen und er waere ungetestet.
        calls["n"] = 0
        bt.sample_within_budget = lambda stmts, budget, **kw: (stmts, False)
        report2 = asyncio.run(bt.run_backtest(
            datetime.date(2026, 5, 1), datetime.date(2026, 5, 6),
            execute=True, max_calls=0, budget_usd=per_call * 2.5, db_path=_tmp_db(),
        ))
    finally:
        bt.fetch_all_raw, bt.classify = orig_fetch, orig_classify
        bt.sample_within_budget = orig_sample

    check("Laufzeit-Stopp greift auch ohne Vorab-Stichprobe (2 statt 6 Calls)",
          calls["n"] == 2)
    check("Report weist den Budget-Abbruch aus", report2.get("stopped_by_budget") is True)
    check("auch bei versagender Planung bleiben die Kosten im Budget",
          report2.get("actual_cost_usd", 0) <= report2.get("budget_usd", 0))


def main():
    test_zeitstempel_format_ohne_Z()
    test_feed_parsing()
    test_200_mit_fehlertext_wird_zur_exception()
    test_fehlender_key_wirft_sofort()
    test_kein_mehrfach_themenfilter_mehr()
    test_budget_stichprobe_deckt_ganzen_zeitraum_ab()
    test_budget_reproduzierbar_und_abschaltbar()
    test_budget_stoppt_auch_zur_laufzeit()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
