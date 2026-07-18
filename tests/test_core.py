"""Smoke-Tests fuer die Fixes aus dem 5-Agenten-Bug-Hunt (Tasks 22-27).

Deckt ab: Schema-Migration, insert_statement-Kollision, get_known_source_ids/
get_dedup_candidates, related_topic_id-Validierung (orchestrator), max_tokens-
Truncation-Erkennung (classifier), Dashboard-Auth/MAX_TEST_TEXT_LENGTH,
BoundedSeenSet, RSS-Quelle ohne Themen-/Personenfilter, Truth-Social Account-Scope,
Telegram-Digest Zeilen-Budget.

Kein echter Netzwerk-Call (Anthropic/Telegram werden gemockt).
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
os.environ.setdefault("DASHBOARD_API_KEY", "test-secret-key")

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# ---------------------------------------------------------------------------
# 1. Schema-Migration
# ---------------------------------------------------------------------------
def test_schema_migration():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute(
        """
        CREATE TABLE statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            url TEXT,
            published_at REAL,
            ingested_at REAL NOT NULL,
            is_market_relevant INTEGER,
            sentiment TEXT,
            confidence REAL,
            tickers TEXT,
            sectors TEXT,
            reasoning TEXT,
            alert_sent INTEGER NOT NULL DEFAULT 0,
            duplicate_of_id INTEGER
        )
        """
    )
    conn.commit()
    conn.close()

    os.environ["DB_PATH"] = tmp.name
    import importlib
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)

    db.init_db()
    with db.get_conn() as conn2:
        cols = {row["name"] for row in conn2.execute("PRAGMA table_info(statements)").fetchall()}
    check("migration: related_topic_id Spalte vorhanden", "related_topic_id" in cols)
    check("migration: is_major_escalation Spalte vorhanden", "is_major_escalation" in cols)

    raw = db.RawStatement(source="test", source_id="mig-1", text="hallo")
    cls = db.Classification(is_market_relevant=True, sentiment="positive", confidence=0.9,
                             related_topic_id=None, is_major_escalation=False)
    sid = db.insert_statement(raw, cls)
    check("migration: insert_statement nach Migration funktioniert", sid is not None)

    os.unlink(tmp.name)
    return db, config


# ---------------------------------------------------------------------------
# 2. insert_statement Kollision + get_known_source_ids + get_dedup_candidates
# ---------------------------------------------------------------------------
def test_insert_and_batch_queries(db):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    import importlib
    import app.config as config
    importlib.reload(config)
    importlib.reload(db)
    db.init_db()

    raw = db.RawStatement(source="test", source_id="dup-1", text="Erste Version")
    cls = db.Classification(is_market_relevant=True, sentiment="negative", confidence=0.8)
    first_id = db.insert_statement(raw, cls)
    check("insert_statement: erster Insert liefert gueltige ID", isinstance(first_id, int) and first_id > 0)

    raw2 = db.RawStatement(source="test", source_id="dup-1", text="Zweite Version (Duplikat source_id)")
    second_id = db.insert_statement(raw2, cls)
    check("insert_statement: Kollision liefert dieselbe existierende ID (nicht 0/None)", second_id == first_id)

    raw3 = db.RawStatement(source="test", source_id="unique-2", text="Andere Meldung")
    third_id = db.insert_statement(raw3, cls)

    known = db.get_known_source_ids(["dup-1", "unique-2", "nicht-vorhanden"])
    check("get_known_source_ids: erkennt bekannte IDs", known == {"dup-1", "unique-2"})

    candidates = db.get_dedup_candidates()
    cand_ids = {c["id"] for c in candidates}
    check("get_dedup_candidates: liefert eingefuegte Statements", first_id in cand_ids and third_id in cand_ids)

    os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# 3. BoundedSeenSet
# ---------------------------------------------------------------------------
def test_bounded_seen_set():
    from app.util import BoundedSeenSet
    s = BoundedSeenSet(maxlen=3)
    s.add("a")
    s.add("b")
    s.add("c")
    check("BoundedSeenSet: enthaelt a,b,c", "a" in s and "b" in s and "c" in s)
    s.add("d")
    check("BoundedSeenSet: verdraengt aeltestes Element (a) nach Ueberschreiten von maxlen", "a" not in s)
    check("BoundedSeenSet: b,c,d weiterhin vorhanden", "b" in s and "c" in s and "d" in s)
    check("BoundedSeenSet: Groesse bleibt bei maxlen", len(s) == 3)


# ---------------------------------------------------------------------------
# 4. news_rss akzeptiert ALLE Feed-Eintraege (kein Themen-/Personenfilter mehr -
#    der Monitor deckt allgemein marktrelevante Nachrichten ab, nicht nur einen
#    bestimmten Namen/ein bestimmtes Thema). Praezision entsteht nachgelagert durch
#    die Claude-Klassifikation + den Tages-Kostendeckel, nicht durch einen
#    Ingestion-seitigen Text-Filter.
# ---------------------------------------------------------------------------
def test_news_rss_no_topic_filter():
    import asyncio
    from app.sources.news_rss import RssNewsSource

    src = RssNewsSource()

    class FakeEntry(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    entries = [
        FakeEntry({"link": "https://example.com/a", "title": "Trump kuendigt Zoelle an", "summary": ""}),
        FakeEntry({"link": "https://example.com/b", "title": "Fed hebt Leitzins an", "summary": ""}),
        FakeEntry({"link": "https://example.com/c", "title": "Unternehmen X meldet Rekordgewinn", "summary": ""}),
        FakeEntry({"link": "https://example.com/d", "title": "Lokales Sportergebnis ohne Marktbezug", "summary": ""}),
    ]

    class FakeClient:
        async def get(self, url):
            raise AssertionError("sollte in diesem Test nicht aufgerufen werden")

    async def fake_fetch_feed(feed_url, client):
        return entries

    src._fetch_feed = fake_fetch_feed
    results = asyncio.run(src.poll())
    links = {r.source_id for r in results}
    check("alle 4 Eintraege werden durchgereicht, kein Themen-/Personenfilter mehr",
          links == {"https://example.com/a", "https://example.com/b",
                     "https://example.com/c", "https://example.com/d"})


# ---------------------------------------------------------------------------
# 5. Truth Social Account-Scope
# ---------------------------------------------------------------------------
def test_truth_social_account_scope():
    from app.sources.truth_social import TruthSocialSource
    src = TruthSocialSource(handle="realDonaldTrump")

    own_status = {
        "id": "111",
        "content": "<p>Wichtige Ankuendigung</p>",
        "url": "https://truthsocial.com/@realDonaldTrump/111",
        "account": {"username": "realDonaldTrump", "acct": "realDonaldTrump"},
    }
    foreign_status = {
        "id": "222",
        "content": "<p>Fremder Post</p>",
        "url": "https://truthsocial.com/@someoneElse/222",
        "account": {"username": "someoneElse", "acct": "someoneElse"},
    }
    malformed = "not_a_dict"

    results = src._to_raw_statements([own_status, foreign_status, malformed], enforce_account_scope=True)
    ids = {r.source_id for r in results}
    check("truth_social: eigener Account wird uebernommen", "111" in ids)
    check("truth_social: fremder Account wird beim Browser-Fallback verworfen", "222" not in ids)
    check("truth_social: malformed Eintrag (kein dict) bringt Verarbeitung nicht zum Absturz", len(ids) == 1)

    src2 = TruthSocialSource(handle="realDonaldTrump")
    results2 = src2._to_raw_statements([foreign_status], enforce_account_scope=False)
    check("truth_social: direkter API-Pfad (enforce=False) filtert nicht nach Account",
          "222" in {r.source_id for r in results2})


# ---------------------------------------------------------------------------
# 6. Telegram Digest Zeilen-Budget (kein Char-Slicing mitten im HTML)
# ---------------------------------------------------------------------------
def test_telegram_digest_budget():
    from app.telegram_alert import _format_digest, TELEGRAM_MAX_LENGTH, DIGEST_MAX_DETAIL_LINES
    from app.db import RawStatement, Classification

    items = []
    for i in range(30):
        raw = RawStatement(source="test", source_id=f"s{i}", text="X" * 150 + f" Meldung {i}")
        cls = Classification(
            is_market_relevant=True,
            sentiment="negative",
            confidence=0.9,
            reasoning="Y" * 150 + f" Begruendung {i}",
            ticker_calls=[{"ticker": f"TICK{i}", "direction": "short", "reasoning": "Y" * 100}] * 5,
        )
        items.append((raw, cls, i))

    text = _format_digest(items)
    check("digest: Ergebnis unter TELEGRAM_MAX_LENGTH", len(text) <= TELEGRAM_MAX_LENGTH)
    check("digest: kein Rohtext/keine Begruendung ungekuerzt enthalten (extrem kurz)",
          "Y" * 150 not in text and "X" * 150 not in text)
    check("digest: kein abgeschnittenes HTML-Tag ('<b>' vollstaendig geschlossen)",
          "<b>" in text and "</b>" in text)

    # Kleine Anzahl Items -> alles sollte reinpassen, kein "+X weitere"
    small_items = items[:2]
    small_text = _format_digest(small_items)
    check("digest: bei wenigen Items kein Abschneide-Hinweis", "weitere" not in small_text)

    from app.telegram_alert import _format_ticker_calls_compact
    compact = _format_ticker_calls_compact([
        {"ticker": "LOW", "direction": "short", "confidence": 0.2, "reasoning": ""},
        {"ticker": "HIGH", "direction": "long", "confidence": 0.95, "reasoning": ""},
    ])
    check("digest-Ticker: nach Konfidenz sortiert (hoechste zuerst) + Prozent angezeigt",
          compact.index("HIGH") < compact.index("LOW") and "95%" in compact and "20%" in compact)


# ---------------------------------------------------------------------------
# 7. related_topic_id Validierung (orchestrator._classify_and_store)
# ---------------------------------------------------------------------------
def test_related_topic_id_validation():
    import app.orchestrator as orch
    from app.db import Classification, RawStatement

    async def fake_classify(text, recent_context=None, priority=False, _bypass_daily_cap=False):
        return Classification(
            is_market_relevant=True,
            sentiment="negative",
            confidence=0.9,
            related_topic_id=99999,  # nicht in recent_context enthalten -> Halluzination
            is_major_escalation=True,
        )

    orig_classify = orch.classify
    orig_insert = orch.insert_statement
    orch.classify = fake_classify
    orch.insert_statement = lambda raw, cls: 12345

    async def run():
        raw = RawStatement(source="test", source_id="halluc-1", text="Testaussage")
        sem = asyncio.Semaphore(1)
        recent_context = [{"id": 1, "text": "anderes Thema", "sentiment": "neutral"}]
        result = await orch._classify_and_store(raw, sem, recent_context)
        return result

    try:
        result = asyncio.run(run())
        _, cls, _ = result
        check("related_topic_id-Validierung: halluzinierte ID wird auf None korrigiert",
              cls.related_topic_id is None)
        check("related_topic_id-Validierung: is_major_escalation wird bei ungueltiger ID zurueckgesetzt",
              cls.is_major_escalation is False)
    finally:
        orch.classify = orig_classify
        orch.insert_statement = orig_insert


# ---------------------------------------------------------------------------
# 8. max_tokens Truncation -> RuntimeError
# ---------------------------------------------------------------------------
def test_max_tokens_truncation():
    import importlib

    # Frische, initialisierte DB: classify() prueft jetzt intern den taeglichen
    # Kostendeckel (get_classification_calls_today), braucht also eine DB mit der
    # classification_calls-Tabelle statt des (evtl. schon geloeschten) DB_PATH aus
    # einem vorherigen Test.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.classifier as clf
    importlib.reload(clf)

    class FakeResponse:
        stop_reason = "max_tokens"
        content = []

    class FakeMessages:
        async def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    orig_client = clf._client
    clf._client = FakeClient()
    try:
        async def run():
            try:
                await clf.classify("Testtext")
                return False
            except RuntimeError as exc:
                return "max_tokens" in str(exc)
        result = asyncio.run(run())
        check("classifier: max_tokens stop_reason wirft RuntimeError statt stille Korruption", result)
    finally:
        clf._client = orig_client
        os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# 9. Dashboard Auth + MAX_TEST_TEXT_LENGTH
# ---------------------------------------------------------------------------
def test_dashboard_auth():
    import importlib
    from fastapi.testclient import TestClient

    # Eigene, frische DB fuer diesen Test + Reload der Modulkette (config -> db ->
    # dashboard), da dashboard.py Funktionen per "from app.db import ..." direkt
    # bindet - ein reiner db-Reload ohne dashboard-Reload wuerde sonst weiterhin
    # auf die alten (an den alten DB_PATH gebundenen) Funktionsobjekte zeigen.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["DASHBOARD_API_KEY"] = "test-secret-key"

    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.dashboard as dash
    importlib.reload(dash)

    dash.config.DASHBOARD_API_KEY = "test-secret-key"

    client = TestClient(dash.app)

    resp_no_key = client.get("/api/statements")
    check("dashboard: /api/statements ohne Key -> 401", resp_no_key.status_code == 401)

    resp_wrong_key = client.get("/api/statements", headers={"X-API-Key": "falsch"})
    check("dashboard: /api/statements mit falschem Key -> 401", resp_wrong_key.status_code == 401)

    resp_ok = client.get("/api/statements", headers={"X-API-Key": "test-secret-key"})
    check("dashboard: /api/statements mit korrektem Key -> 200", resp_ok.status_code == 200)

    resp_health = client.get("/api/health")
    check("dashboard: /api/health OHNE Key erreichbar (bewusst ungeschuetzt)", resp_health.status_code == 200)

    too_long = "x" * (dash.MAX_TEST_TEXT_LENGTH + 1)
    resp_413 = client.post(
        "/api/test",
        json={"text": too_long, "send_telegram": False},
        headers={"X-API-Key": "test-secret-key"},
    )
    check("dashboard: /api/test mit zu langem Text -> 413", resp_413.status_code == 413)

    os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# 10. Extrem kurze Einzel-Nachricht (Konfidenz, Headline, Ticker - sonst nichts)
# ---------------------------------------------------------------------------
def test_short_message_format():
    from app.telegram_alert import _format_message
    from app.db import RawStatement, Classification

    raw = RawStatement(
        source="test", source_id="short-1",
        text="X" * 300 + " ein sehr langer Rohtext der NICHT in der Nachricht landen soll",
    )
    full_reasoning = "Neue Stahlzoelle angekuendigt, betrifft mehrere Autobauer und Stahlproduzenten stark."
    cls = Classification(
        is_market_relevant=True,
        sentiment="negative",
        confidence=0.87,
        reasoning=full_reasoning,
        ticker_calls=[
            {"ticker": "X", "direction": "short", "confidence": 0.55, "reasoning": "profitiert nicht"},
            {"ticker": "NUE", "direction": "long", "confidence": 0.91, "reasoning": "profitiert von Zoellen"},
        ],
    )
    text = _format_message(raw, cls)
    check("Nachricht: Konfidenz enthalten", "87%" in text)
    check("Nachricht: volle Ueberschrift NICHT abgekuerzt", full_reasoning in text)
    check("Nachricht: Ticker mit ausgeschriebenem long/short + eigener Konfidenz",
          "NUE (long) – 91%" in text and "X (short) – 55%" in text)
    check("Nachricht: sicherste Ticker-Einschaetzung steht ganz oben (NUE 91% vor X 55%)",
          text.index("NUE (long)") < text.index("X (short)"))
    check("Nachricht: roher Statement-Text NICHT enthalten", "X" * 300 not in text)
    check("Nachricht: kein Disclaimer/Sektoren-Block mehr", "Finanzberatung" not in text and "Sektoren" not in text)

    cls_escalation = Classification(
        is_market_relevant=True, sentiment="negative", confidence=0.9,
        reasoning="Eskalation der Lage.", related_topic_id=5, is_major_escalation=True,
    )
    text_esc = _format_message(raw, cls_escalation)
    check("Nachricht: Eskalations-Marker bei is_major_escalation", "⚠️" in text_esc)

    cls_no_tickers = Classification(
        is_market_relevant=True, sentiment="neutral", confidence=0.6,
        reasoning="Allgemeine Aussage ohne konkreten Ticker-Bezug.",
    )
    text_no_tickers = _format_message(raw, cls_no_tickers)
    check("Nachricht: Platzhalter bei fehlenden Tickern crasht nicht", "–" in text_no_tickers)


# ---------------------------------------------------------------------------
# 11. GDELT-Query enthaelt sourcelang:english (Cross-Language-Duplikate)
# ---------------------------------------------------------------------------
def test_gdelt_sourcelang_filter():
    import app.sources.news_gdelt as gdelt
    import inspect
    src = inspect.getsource(gdelt.GdeltNewsSource.poll)
    check("GDELT-Query enthaelt sourcelang:english", "sourcelang:english" in src)


# ---------------------------------------------------------------------------
# 12. MAX_CONCURRENT_CLASSIFICATIONS Default ist 1 (Duplikat-Race vermeiden)
# ---------------------------------------------------------------------------
def test_concurrency_default():
    saved = os.environ.pop("MAX_CONCURRENT_CLASSIFICATIONS", None)
    try:
        import importlib
        import app.config as config
        importlib.reload(config)
        check("MAX_CONCURRENT_CLASSIFICATIONS Default ist 1", config.MAX_CONCURRENT_CLASSIFICATIONS == 1)
    finally:
        if saved is not None:
            os.environ["MAX_CONCURRENT_CLASSIFICATIONS"] = saved


def main():
    db, config = test_schema_migration()
    test_insert_and_batch_queries(db)
    test_bounded_seen_set()
    test_news_rss_no_topic_filter()
    test_truth_social_account_scope()
    test_telegram_digest_budget()
    test_related_topic_id_validation()
    test_max_tokens_truncation()
    test_dashboard_auth()
    test_short_message_format()
    test_gdelt_sourcelang_filter()
    test_concurrency_default()

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
