"""Tests fuer get_topic_thread(): findet die GESAMTE Kette einer (mehrfach)
eskalierten Themen-Story, nicht nur den letzten Zwischenschritt.

Bug-Hunt-Fund: der urspruengliche Ein-Hop-Vergleich (WHERE id=? OR related_topic_id=?
OR duplicate_of_id=?) verlor bei einer Mehrfach-Eskalation (C zeigt auf B, B zeigt auf
die eigentliche Wurzel A) sowohl A als auch die korrekte Reihenfolge, wenn Claude bei
C related_topic_id=B (statt =A) gesetzt hat - ein durchaus realistischer Fall, da B
naeher am aktuellen Kontext liegt und in recent_context vor A steht."""
import os
import sys
import tempfile
import time
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

    now = time.time()

    # A: der urspruengliche Ur-Alert (keine Verweise).
    a = db.RawStatement(source="test", source_id="a", text="Erste Zoll-Drohung")
    a_id = db.insert_statement(a, db.Classification(is_market_relevant=True, sentiment="negative", confidence=0.9))
    db.mark_alert_sent(a_id)

    # B: Eskalation von A (related_topic_id=A, is_major_escalation=True) -> eigener Alert.
    b = db.RawStatement(source="test", source_id="b", text="Zoll konkretisiert")
    b_id = db.insert_statement(
        b, db.Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                             related_topic_id=a_id, is_major_escalation=True),
    )
    db.mark_alert_sent(b_id)

    # C: Eskalation - Claude verlinkt auf B (den juengsten Kontext-Eintrag), NICHT auf A.
    c = db.RawStatement(source="test", source_id="c", text="Zoll tritt in Kraft")
    c_id = db.insert_statement(
        c, db.Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                             related_topic_id=b_id, is_major_escalation=True),
    )
    db.mark_alert_sent(c_id)

    # Reiner Text-Duplikat-Eintrag von C (Tier-1-Dedup, kein eigener Alert) - sollte
    # als Teil des Threads erscheinen (ist Teil derselben Berichterstattung).
    dup = db.RawStatement(source="test", source_id="dup", text="Zoll tritt in Kraft (Agenturmeldung)")
    dup_id = db.insert_statement(dup, None, duplicate_of_id=c_id)

    # --- Aufruf mit topic_id=B (dem Zwischenschritt, wie ihn orchestrator.py uebergeben
    # wuerde, wenn Claude C mit related_topic_id=B statt =A verknuepft hat) ---
    thread = db.get_topic_thread(b_id)
    ids = [row["id"] for row in thread]

    check("Thread enthaelt alle 4 Eintraege (A, B, C, Duplikat)", set(ids) == {a_id, b_id, c_id, dup_id})
    check("Thread ist zeitlich aufsteigend sortiert", ids == sorted(ids))
    check("Wurzel A steht an erster Stelle (nicht B)", ids[0] == a_id)
    check("A's Text ist im ersten Eintrag", thread[0]["text"] == "Erste Zoll-Drohung")

    # --- Aufruf direkt mit der Wurzel A liefert dasselbe Ergebnis ---
    thread_from_root = db.get_topic_thread(a_id)
    check("Aufruf mit der Wurzel selbst liefert dieselbe vollstaendige Kette",
          {row["id"] for row in thread_from_root} == {a_id, b_id, c_id, dup_id})

    # --- Ein isoliertes Thema (keine Eskalation) liefert nur sich selbst ---
    lonely = db.RawStatement(source="test", source_id="lonely", text="Einzelne Meldung ohne Bezug")
    lonely_id = db.insert_statement(lonely, db.Classification(is_market_relevant=True, sentiment="neutral", confidence=0.9))
    thread_lonely = db.get_topic_thread(lonely_id)
    check("isoliertes Thema liefert nur sich selbst", [row["id"] for row in thread_lonely] == [lonely_id])

    # --- Die aufgeloeste Wurzel wird nie allein wegen des Zeitfensters ausgeschlossen:
    # mit hours=0 (Cutoff = jetzt) muesste die Wurzel A trotzdem erscheinen, auch wenn
    # alle anderen Kettenglieder (B, C, Duplikat - nicht die Wurzel selbst) durch den
    # engen Cutoff herausfallen. ---
    thread_tiny_window = db.get_topic_thread(b_id, hours=0)
    ids_tiny = {row["id"] for row in thread_tiny_window}
    check("Wurzel A erscheint trotz hours=0 (id=root wird nie durch Cutoff ausgeschlossen)",
          a_id in ids_tiny)
    check("Nicht-Wurzel-Kettenglieder unterliegen bei hours=0 weiterhin dem Cutoff",
          b_id not in ids_tiny)

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
