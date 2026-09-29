"""Tests fuer die Skalierung UND die unveraenderte Korrektheit der Chargen-internen
Duplikaterkennung (_partition_duplicates).

Hintergrund (echter Fund, kein theoretisches Risiko): die Charge wurde bisher als
flache Liste gefuehrt und jedes neue Statement gegen ALLE bereits akzeptierten
verglichen - O(n^2) teure SequenceMatcher-Aufrufe. Im Live-Betrieb faellt das nie auf
(eine Charge ist ein Poll-Zyklus, also eine Handvoll Meldungen). Im Backtest ueber drei
Monate mit 24 Tickern ist die Charge dagegen fuenfstellig: ein Lauf verbrannte >69
Minuten CPU, ohne dass auch nur EIN Claude-Call passierte - das Log blieb komplett leer,
der Lauf sah aus wie aufgehaengt.

Der Fix darf die Erkennung selbst NICHT aufweichen - genau das pruefen die
Korrektheits-Tests hier. Das Zeitfenster ist keine neue Regel, sondern dieselbe, die
der DB-Teil ueber db.get_dedup_candidates(window_seconds=...) laengst anwendet; die
Charge war bisher der einzige Pfad ohne sie.
"""
import datetime
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

import app.orchestrator as orch
from app.config import DEDUP_WINDOW_SECONDS
from app.db import RawStatement

failures = []


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _no_db():
    """Frische Backtest-DB = keine bekannten IDs, keine DB-Kandidaten."""
    orch.get_known_source_ids = lambda ids: set()
    orch.get_dedup_candidates = lambda: []


BASE = datetime.datetime(2026, 5, 5, tzinfo=datetime.timezone.utc).timestamp()


def _stmt(i, text, published_at):
    return RawStatement(
        source="news_finnhub", source_id=f"https://x/{i}", text=text,
        published_at=published_at,
    )


# --- Korrektheit: der Fix darf die Erkennung nicht aufweichen -----------------------
def test_syndizierte_kopie_wird_weiterhin_erkannt():
    """Der eigentliche Zweck der Chargen-Dedup: dieselbe Agenturmeldung, von mehreren
    Portalen fast zeitgleich syndiziert. Das ist der LIVE-Fall - er muss unveraendert
    funktionieren."""
    _no_db()
    text = "Federal Reserve raises rates sharply amid persistent inflation concerns"
    stmts = [
        _stmt(1, f"{text} (Quelle: Reuters)", BASE),
        _stmt(2, f"{text} (Quelle: Bloomberg)", BASE + 120),   # 2 Minuten spaeter
        _stmt(3, f"{text} (Quelle: CNBC)", BASE + 600),        # 10 Minuten spaeter
    ]
    keep, dupes = orch._partition_duplicates(stmts)
    check("zeitnahe syndizierte Kopien werden weiterhin als Duplikat erkannt",
          len(keep) == 1 and len(dupes) == 2)
    check("die erste Meldung ist die behaltene (primary)",
          keep and keep[0].source_id == "https://x/1")


def test_ohne_zeitstempel_weiterhin_dedupliziert():
    """Meldungen ohne published_at duerfen nicht still an der Erkennung vorbeirutschen,
    nur weil sie sich keinem Zeitfenster zuordnen lassen."""
    _no_db()
    text = "Chipmaker announces surprise merger with a large rival firm"
    stmts = [
        _stmt(1, f"{text} (Quelle: Reuters)", None),
        _stmt(2, f"{text} (Quelle: Bloomberg)", None),
    ]
    keep, dupes = orch._partition_duplicates(stmts)
    check("zeitlose Meldungen werden weiterhin gegeneinander dedupliziert",
          len(keep) == 1 and len(dupes) == 1)


def test_verschiedene_meldungen_bleiben_erhalten():
    """Gegenprobe: inhaltlich verschiedene Meldungen zur selben Zeit duerfen NICHT
    zusammengelegt werden."""
    _no_db()
    stmts = [
        _stmt(1, "Federal Reserve raises rates amid inflation concerns (Quelle: Reuters)", BASE),
        _stmt(2, "Oil prices plunge after OPEC increases its output quota (Quelle: Bloomberg)", BASE + 60),
        _stmt(3, "Retailer reports deep quarterly loss and announces layoffs (Quelle: CNBC)", BASE + 120),
    ]
    keep, dupes = orch._partition_duplicates(stmts)
    check("verschiedene Meldungen bleiben alle erhalten", len(keep) == 3 and len(dupes) == 0)


def test_gleicher_text_monate_auseinander_ist_kein_duplikat():
    """Die neue Regel: zwei wortgleiche Meldungen mit MONATEN Abstand sind keine
    Syndizierung, sondern zwei getrennte Ereignisse (z.B. 'Fed raises rates' im Mai und
    im August). Genau dieselbe Logik wendet der DB-Teil ueber sein Zeitfenster laengst
    an - die Charge war bisher als einziger Pfad davon ausgenommen und haette das
    August-Ereignis stillschweigend verworfen."""
    _no_db()
    text = "Federal Reserve raises rates sharply amid persistent inflation concerns"
    stmts = [
        _stmt(1, f"{text} (Quelle: Reuters)", BASE),
        _stmt(2, f"{text} (Quelle: Reuters)", BASE + 90 * 86400),  # 3 Monate spaeter
    ]
    keep, dupes = orch._partition_duplicates(stmts)
    check("wortgleiche Meldung 3 Monate spaeter wird NICHT als Duplikat verworfen",
          len(keep) == 2 and len(dupes) == 0)

    # Grenzfall: knapp INNERHALB des Fensters muss weiterhin greifen.
    stmts_innerhalb = [
        _stmt(1, f"{text} (Quelle: Reuters)", BASE),
        _stmt(2, f"{text} (Quelle: Bloomberg)", BASE + DEDUP_WINDOW_SECONDS - 60),
    ]
    keep2, dupes2 = orch._partition_duplicates(stmts_innerhalb)
    check("knapp innerhalb des Zeitfensters wird weiterhin dedupliziert",
          len(keep2) == 1 and len(dupes2) == 1)


def test_exakte_abkuerzung_aendert_ergebnis_nicht():
    """text_similarity(min_ratio=...) nutzt real_quick_ratio()/quick_ratio() als
    garantierte Obergrenzen. Das MUSS dieselbe Ja/Nein-Entscheidung liefern wie die
    volle Berechnung - sonst waere es keine Optimierung, sondern eine stille
    Verhaltensaenderung."""
    from app.util import text_similarity

    paare = [
        ("Fed raises rates amid inflation", "Fed raises rates amid inflation"),
        ("Fed raises rates amid inflation", "Fed raises rates amid inflation concerns"),
        ("Fed raises rates amid inflation", "Oil prices plunge after OPEC output hike"),
        ("Apple beats quarterly revenue guidance", "Apple beats quarterly revenue guidance (Reuters)"),
        ("Tesla halts production in Berlin", "Tesla expands production in Berlin"),
        ("kurz", "ein deutlich laengerer text der voellig anders aussieht"),
    ]
    schwelle = 0.82
    alle_gleich = True
    for a, b in paare:
        voll = text_similarity(a, b) >= schwelle
        abgekuerzt = text_similarity(a, b, min_ratio=schwelle) >= schwelle
        if voll != abgekuerzt:
            alle_gleich = False
            print(f"      Abweichung: {a!r} vs {b!r}: voll={voll} abgekuerzt={abgekuerzt}")
    check("Abkuerzung liefert exakt dieselbe Ja/Nein-Entscheidung wie die volle Berechnung",
          alle_gleich)


# --- Skalierung ---------------------------------------------------------------------
def test_grosse_charge_bleibt_bearbeitbar():
    """Der eigentliche Regressionstest: eine Backtest-grosse Charge (ueber 3 Monate
    gestreut) muss in Sekunden durchlaufen, nicht in Stunden. Vor dem Fix brauchten
    allein 500 Statements ~46s (gemessen), 5000 waeren ueber eine Stunde gewesen."""
    _no_db()
    import random
    firms = ["Apple", "Microsoft", "Nvidia", "Tesla", "Amazon", "Meta", "Roku", "Snap",
             "SoFi", "Upstart", "Etsy", "Pinterest", "DoorDash", "Roblox", "Affirm"]
    verbs = ["beats", "misses", "raises", "cuts", "announces", "delays", "expands",
             "halts", "launches", "acquires", "faces", "reports", "warns"]
    objs = ["quarterly revenue guidance", "a share buyback", "a federal probe",
            "an antitrust lawsuit", "subscriber growth", "a dividend increase",
            "its AI roadmap", "a restructuring plan", "margin pressure from tariffs"]
    outlets = ["Reuters", "Bloomberg", "CNBC", "MarketWatch", "Barron's", "WSJ"]
    random.seed(7)
    n = 5000
    # Echte Schlagzeilen sind praktisch alle verschieden - deshalb eine konkrete Zahl
    # mit hinein (Quartal/Prozentwert), sonst erzeugt das kleine Vokabular massenhaft
    # WORTGLEICHE Texte und die (voellig korrekt arbeitende) Erkennung wuerde
    # erwartungsgemaess fast alles zusammenlegen - das wuerde hier die Skalierung
    # messen, aber nichts ueber die Trennschaerfe aussagen.
    stmts = [
        _stmt(i,
              f"{random.choice(firms)} {random.choice(verbs)} {random.choice(objs)} "
              f"by {random.uniform(0.1, 40):.1f}% in Q{random.randint(1, 4)} "
              f"(Quelle: {random.choice(outlets)})",
              BASE + random.random() * 90 * 86400)
        for i in range(n)
    ]

    t0 = time.perf_counter()
    keep, dupes = orch._partition_duplicates(stmts)
    dauer = time.perf_counter() - t0
    print(f"      {n} Statements in {dauer:.2f}s ({len(keep)} behalten, {len(dupes)} Duplikate)")

    # Grosszuegige Schwelle: es geht um "Sekunden statt Stunden", nicht um eine exakte
    # Laufzeit (die haengt von der Maschine ab und soll den Test nicht flaky machen).
    check(f"{n} Statements in unter 60s verarbeitet (vor dem Fix: >1 Stunde)", dauer < 60)
    check("dabei wurden ueberhaupt Duplikate gefunden (Erkennung laeuft noch)", len(dupes) > 0)
    check("und nicht alles faelschlich als Duplikat verworfen", len(keep) > n * 0.3)


def test_vergleichsdeckel_greift():
    """Harte Obergrenze je Statement (analog zum DB-Teil, db.get_dedup_candidates:
    limit=500): selbst wenn ein einzelnes Zeitfenster extrem voll ist, darf die Zahl
    teurer Vergleiche pro Statement nicht unbegrenzt wachsen."""
    _no_db()
    check("DEDUP_BATCH_MAX_COMPARISONS ist gesetzt und endlich",
          isinstance(orch.DEDUP_BATCH_MAX_COMPARISONS, int)
          and 0 < orch.DEDUP_BATCH_MAX_COMPARISONS <= 5000)

    from app import util
    orig = util.text_similarity
    counter = {"n": 0}

    def zaehlende_similarity(a, b, min_ratio=None):
        counter["n"] += 1
        return 0.0  # nie ein Duplikat -> Charge waechst maximal an

    # Alle im GLEICHEN Zeitfenster, damit der Deckel ueberhaupt greifen kann.
    n = 1500
    stmts = [_stmt(i, f"Voellig einzigartige Meldung Nummer {i}", BASE + i) for i in range(n)]

    orch_similarity = orch.text_similarity
    orch.text_similarity = zaehlende_similarity
    try:
        orch._partition_duplicates(stmts)
        vergleiche_gesamt = counter["n"]
    finally:
        orch.text_similarity = orch_similarity
        util.text_similarity = orig

    # Ohne Deckel waeren es n*(n-1)/2 = ~1.1 Mio Vergleiche.
    ohne_deckel = n * (n - 1) // 2
    obergrenze = n * orch.DEDUP_BATCH_MAX_COMPARISONS
    print(f"      {vergleiche_gesamt} Vergleiche (ohne Deckel waeren es {ohne_deckel})")
    check("Vergleiche bleiben unter der harten Obergrenze n * DEDUP_BATCH_MAX_COMPARISONS",
          vergleiche_gesamt <= obergrenze)
    check("und deutlich unter dem ungedeckelten O(n^2)-Fall",
          vergleiche_gesamt < ohne_deckel)


def main():
    test_syndizierte_kopie_wird_weiterhin_erkannt()
    test_ohne_zeitstempel_weiterhin_dedupliziert()
    test_verschiedene_meldungen_bleiben_erhalten()
    test_gleicher_text_monate_auseinander_ist_kein_duplikat()
    test_exakte_abkuerzung_aendert_ergebnis_nicht()
    test_grosse_charge_bleibt_bearbeitbar()
    test_vergleichsdeckel_greift()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
