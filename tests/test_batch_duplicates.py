"""Testet den Fix fuer verlorene Batch-Duplikate, wenn die "primary"-Meldung selbst
als Themen-Duplikat gespeichert wird (Agent-5-Fund aus Bug-Hunt Runde 2)."""
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

    import importlib
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)

    # Vorab ein "schon heute alarmiertes" Statement anlegen, auf das related_topic_id
    # zeigen kann.
    already_alerted = db.RawStatement(source="test", source_id="already-1", text="Frueheres Thema")
    already_id = db.insert_statement(
        already_alerted,
        db.Classification(is_market_relevant=True, sentiment="negative", confidence=0.9),
    )
    db.mark_alert_sent(already_id)

    # A ist die "primary" Meldung dieser Charge, B/C sind Tier-1-Textduplikate von A
    # (siehe duplicate_pairs in poll_once). Claude klassifiziert A als Themen-Duplikat
    # von already_id (keine Eskalation) -> A bekommt selbst KEINEN neuen Alert, aber
    # WIRD in der DB gespeichert (duplicate_of_id=already_id).
    raw_a = db.RawStatement(source="test", source_id="A", text="Neue Formulierung des alten Themas")
    raw_b = db.RawStatement(source="test", source_id="B", text="Neue Formulierung des alten Themas B")
    raw_c = db.RawStatement(source="test", source_id="C", text="Neue Formulierung des alten Themas C")

    async def fake_classify(text, recent_context=None, priority=False, _bypass_daily_cap=False):
        return db.Classification(
            is_market_relevant=True, sentiment="negative", confidence=0.9,
            related_topic_id=already_id, is_major_escalation=False,
        )

    orig_classify = orch.classify
    orch.classify = fake_classify
    try:
        async def run():
            sem = asyncio.Semaphore(1)
            recent_context = [{"id": already_id, "text": "Frueheres Thema", "sentiment": "negative"}]
            result = await orch._classify_and_store(raw_a, sem, recent_context)
            return result

        result = asyncio.run(run())
    finally:
        orch.classify = orig_classify

    check("_classify_and_store gibt (raw, None, dup_id) statt bare None zurueck bei Themen-Duplikat",
          result is not None and result[1] is None and isinstance(result[2], int))
    a_dup_id = result[2]

    # Jetzt die volle poll_once()-Logik simulieren: id_by_source_id muss A's ID
    # enthalten, damit B/C (Batch-Duplikate von A) nicht verworfen werden.
    results = [result]
    id_by_source_id = {r[0].source_id: r[2] for r in results}
    check("id_by_source_id enthaelt A trotz Themen-Duplikat-Status", id_by_source_id.get("A") == a_dup_id)

    duplicate_pairs = [(raw_b, raw_a), (raw_c, raw_a)]
    inserted_dup_ids = []
    for dup_raw, primary in duplicate_pairs:
        primary_id = primary["id"] if isinstance(primary, dict) else id_by_source_id.get(primary.source_id)
        check(f"primary_id fuer {dup_raw.source_id} aufloesbar (nicht None)", primary_id is not None)
        if primary_id is not None:
            dup_id = db.insert_statement(dup_raw, None, duplicate_of_id=primary_id)
            inserted_dup_ids.append(dup_id)

    check("B und C wurden tatsaechlich in die DB eingefuegt (nicht verworfen)",
          len(inserted_dup_ids) == 2 and all(x is not None for x in inserted_dup_ids))

    # alert_worthy-Filter darf bei classification=None nicht crashen
    alert_worthy = [
        r for r in results
        if r[1] is not None and r[1].is_market_relevant and r[1].confidence >= 0.5
    ]
    check("alert_worthy-Filter crasht nicht bei classification=None und liefert leere Liste",
          alert_worthy == [])

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
