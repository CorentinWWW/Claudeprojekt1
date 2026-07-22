import datetime
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

from app.config import DB_PATH, DEDUP_SIMILARITY_THRESHOLD, DEDUP_WINDOW_SECONDS
from app.util import text_similarity

SCHEMA = """
CREATE TABLE IF NOT EXISTS statements (
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
    duplicate_of_id INTEGER,
    related_topic_id INTEGER,
    is_major_escalation INTEGER NOT NULL DEFAULT 0,
    expected_move_pct REAL,
    expected_horizon TEXT
);
CREATE INDEX IF NOT EXISTS idx_statements_ingested_at ON statements(ingested_at DESC);

-- Ein Datensatz pro tatsaechlichem Claude-Klassifikations-Call (siehe app/classifier.py:
-- classify()) - unabhaengig davon, ob das Statement am Ende als Themen-Duplikat verworfen
-- wird (der Call selbst ist trotzdem schon bezahlt). Dient als harter, ueber GitHub-Actions-
-- Laeufe hinweg persistenter Kostendeckel (MAX_CLASSIFICATIONS_PER_DAY), nachdem ein einzelner
-- Nachrichtenschub an einem Tag spuerbare API-Kosten verursacht hat.
CREATE TABLE IF NOT EXISTS classification_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_classification_calls_called_at ON classification_calls(called_at);

-- Kleiner Schluessel-Wert-Speicher fuer "genau einmal"-Zustaende, die einzelne
-- GitHub-Actions-Laeufe ueberleben muessen (z.B. "Tages-Limit-Hinweis fuer den
-- 2026-07-14 wurde bereits verschickt", siehe try_claim_meta_key()).
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Ergebnis-Tracking (Backtesting, siehe app/prices.py + orchestrator): pro alarmiertem,
-- handelbarem Ticker der Kurs zum Alarm-Zeitpunkt und - nach einem Horizont - erneut,
-- um die tatsaechliche Kursbewegung (und ob sie zur Long/Short-Einschaetzung passte) zu
-- messen. Basis fuer die Konfidenz-Kalibrierung im Dashboard. UNIQUE(statement_id,
-- ticker), damit derselbe Alert-Ticker nicht doppelt erfasst wird.
CREATE TABLE IF NOT EXISTS alert_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id INTEGER,
    ticker TEXT NOT NULL,
    direction TEXT,
    confidence REAL,
    alert_ts REAL NOT NULL,
    alert_price REAL,
    followup_ts REAL,
    followup_price REAL,
    return_pct REAL,
    correct INTEGER,
    UNIQUE(statement_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_alert_outcomes_pending
    ON alert_outcomes(followup_price, alert_ts);

-- Leichtgewichtiges Protokoll jedes tatsaechlich verschickten Alerts pro handelbarem
-- Ticker+Richtung - UNABHAENGIG vom Preis-Tracking (alert_outcomes wird nur mit
-- ENABLE_PRICE_TRACKING befuellt). Basis fuer den Ticker-Cooldown (#6): denselben
-- Ticker in dieselbe Richtung nicht innerhalb des Cooldown-Fensters erneut alarmieren.
CREATE TABLE IF NOT EXISTS ticker_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id INTEGER,
    ticker TEXT NOT NULL,
    direction TEXT,
    alerted_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticker_alerts_recent
    ON ticker_alerts(ticker, direction, alerted_at);

-- Paper-Trading (virtuelles Depot, siehe app/paper_trading.py): eine Zeile pro
-- VIRTUELLER Position. entry_price/qty/stake werden beim Eroeffnen (auf einen Alert
-- hin) festgehalten; wird die Position spaeter geschlossen (Stop/Ziel/Signal-Umkehr),
-- kommen exit_price/exit_ts/pnl/close_reason dazu und status wechselt auf 'closed'.
-- Reine Simulation, kein echter Handel.
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id INTEGER,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_ts REAL NOT NULL,
    qty REAL NOT NULL,
    stake REAL NOT NULL,
    stop REAL,
    target REAL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_price REAL,
    exit_ts REAL,
    pnl REAL,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status);
CREATE INDEX IF NOT EXISTS idx_paper_positions_open_ticker
    ON paper_positions(ticker, status);
"""

# Fuer DBs, die vor der Einfuehrung von related_topic_id/is_major_escalation angelegt
# wurden (z.B. aus einem alten GitHub-Actions-Cache wiederhergestellt): CREATE TABLE
# IF NOT EXISTS aendert eine bereits existierende Tabelle nicht, daher per ALTER TABLE
# nachruesten, statt dass insert_statement() spaeter mit "no such column" abstuerzt.
_MIGRATION_COLUMNS = {
    "related_topic_id": "ALTER TABLE statements ADD COLUMN related_topic_id INTEGER",
    "is_major_escalation": "ALTER TABLE statements ADD COLUMN is_major_escalation INTEGER NOT NULL DEFAULT 0",
    # Erwartete Bewegung (#3): nullable, damit alte Zeilen ohne diese Werte weiterhin
    # ladbar bleiben (get_recent/get_pending_alerts nutzen row.get()).
    "expected_move_pct": "ALTER TABLE statements ADD COLUMN expected_move_pct REAL",
    "expected_horizon": "ALTER TABLE statements ADD COLUMN expected_horizon TEXT",
}


@dataclass
class RawStatement:
    source: str
    source_id: str
    text: str
    url: Optional[str] = None
    published_at: Optional[float] = None


@dataclass
class Classification:
    is_market_relevant: bool
    sentiment: str  # "positive" | "negative" | "neutral"
    confidence: float
    # Liste von {"ticker": str, "direction": "long"|"short", "reasoning": str}
    ticker_calls: list = field(default_factory=list)
    sectors: list = field(default_factory=list)
    reasoning: str = ""
    # Falls diese Aussage im Kern zu einem kuerzlich bereits alarmierten Thema
    # gehoert: dessen Statement-ID, sonst None. Siehe app/classifier.py.
    related_topic_id: Optional[int] = None
    # Nur relevant wenn related_topic_id gesetzt ist: true = trotz gleichem Thema
    # so bedeutsame Verschaerfung/neue Entwicklung, dass ein erneuter Alert
    # gerechtfertigt ist.
    is_major_escalation: bool = False
    # Grobe, von Claude geschaetzte erwartete Kursbewegung des staerksten Tickers in
    # Prozent (Betrag, ohne Vorzeichen - die Richtung steckt in direction) und ein
    # Zeithorizont ("Stunden"/"Tage"/"Wochen"). Optional; None, wenn keine Schaetzung.
    expected_move_pct: Optional[float] = None
    expected_horizon: Optional[str] = None


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # Innerhalb des try-Blocks: schlaegt eine PRAGMA-Ausfuehrung fehl (z.B. WAL
        # auf einem restriktiven/Netzwerk-Dateisystem), wuerde die Verbindung sonst
        # nie ueber "finally: conn.close()" geschlossen und leaken.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(statements)").fetchall()}
        for col_name, alter_sql in _MIGRATION_COLUMNS.items():
            if col_name not in existing_cols:
                conn.execute(alter_sql)


def _utc_day_start_epoch() -> float:
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def get_classification_calls_today() -> int:
    """Anzahl tatsaechlicher Claude-Klassifikations-Calls seit Mitternacht UTC - Basis
    fuer den Kostendeckel MAX_CLASSIFICATIONS_PER_DAY (siehe app/classifier.py)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM classification_calls WHERE called_at >= ?",
            (_utc_day_start_epoch(),),
        ).fetchone()
    return row["c"]


def record_classification_call() -> None:
    """Vermerkt einen tatsaechlich ausgefuehrten Claude-Klassifikations-Call OHNE
    Limit-Pruefung. Nur fuer Faelle gedacht, die nie durch das Tages-Limit blockiert
    werden duerfen (siehe classify()/selftest() in app/classifier.py), aber trotzdem
    mitgezaehlt werden sollen. Raeumt bei dieser Gelegenheit gleich Eintraege auf, die
    aelter als 2 Tage sind, statt einen eigenen Cron-/Wartungsjob fuer diese kleine
    Tabelle zu brauchen."""
    now = time.time()
    with get_conn() as conn:
        conn.execute("INSERT INTO classification_calls (called_at) VALUES (?)", (now,))
        conn.execute("DELETE FROM classification_calls WHERE called_at < ?", (now - 2 * 86400,))


def reserve_classification_call_slot(limit: int) -> bool:
    """Atomare Variante von "ist das Tages-Limit erreicht? Falls nein, Call vermerken":
    ein getrennter get_classification_calls_today()-Check gefolgt von einem separaten
    record_classification_call()-Insert waere nur INNERHALB eines einzelnen Prozesses/
    Event-Loops race-frei (dort, weil zwischen den beiden synchronen DB-Calls kein
    "await" liegt). Der Kostendeckel soll aber auch dann hart bleiben, wenn z.B. ein
    haengender vorheriger GitHub-Actions-Lauf und ein neu getriggerter Lauf sich
    ueberschneiden (siehe .github/workflows/monitor.yml: concurrency-Guard reduziert
    das Risiko, schliesst es aber nicht 100% aus) - INSERT...SELECT...WHERE laeuft als
    EINE SQL-Anweisung und wird von SQLites eigener Schreibsperre serialisiert, ist
    also auch prozessuebergreifend atomar. Gibt True zurueck, wenn ein Slot reserviert
    wurde (Call darf gemacht werden), False wenn das Limit bereits erreicht ist."""
    cutoff = _utc_day_start_epoch()
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO classification_calls (called_at)
            SELECT ? WHERE (SELECT COUNT(*) FROM classification_calls WHERE called_at >= ?) < ?
            """,
            (now, cutoff, limit),
        )
        reserved = cur.rowcount > 0
        if reserved:
            conn.execute("DELETE FROM classification_calls WHERE called_at < ?", (now - 2 * 86400,))
    return reserved


def try_claim_meta_key(key: str) -> bool:
    """Atomares "wer zuerst kommt": legt den Key an und gibt True zurueck, wenn er
    vorher NICHT existierte - False, wenn ihn schon jemand (dieser oder ein anderer
    Prozess, z.B. ein ueberlappender GitHub-Actions-Lauf) beansprucht hat. INSERT OR
    IGNORE auf den PRIMARY KEY laeuft als eine SQL-Anweisung und wird von SQLites
    Schreibsperre serialisiert. Genutzt fuer Aktionen, die genau einmal passieren
    sollen (z.B. die Telegram-Notiz beim Erreichen des Tages-Limits, mit dem Datum
    im Key). Die Eintraege sind winzig (einer pro Tag/Ereignis) und werden bewusst
    nicht aufgeraeumt."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            (key, str(time.time())),
        )
        return cur.rowcount > 0


def get_meta(key: str) -> Optional[str]:
    """Liest einen Wert aus dem kleinen meta-Schluessel-Wert-Speicher (oder None)."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    """Setzt/aktualisiert einen Wert im meta-Speicher (im Gegensatz zu
    try_claim_meta_key, das nur einmalig anlegt und nie ueberschreibt). Genutzt z.B. fuer
    den Zeitstempel des letzten Depot-Status (Paper-Trading-Throttle)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_known_source_ids(source_ids: list[str]) -> set[str]:
    """Batch-Variante von "ist source_id schon bekannt?": EIN Query fuer die ganze
    Charge statt einer DB-Verbindung/Query pro einzelnem Statement - relevant bei
    einem Nachrichtenschub mit vielen (z.B. 75) Statements in einem Zyklus."""
    if not source_ids:
        return set()
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in source_ids)
        rows = conn.execute(
            f"SELECT source_id FROM statements WHERE source_id IN ({placeholders})",
            source_ids,
        ).fetchall()
    return {row["source_id"] for row in rows}


def get_dedup_candidates(
    window_seconds: int = DEDUP_WINDOW_SECONDS, limit: int = 500
) -> list[dict]:
    """Kuerzliche, nicht schon als Duplikat markierte Statements (id+text) - EINMAL
    pro Charge geladen und dann in-memory gegen jedes neue Statement verglichen,
    statt einer eigenen DB-Abfrage pro Statement (siehe orchestrator.py:
    _partition_duplicates)."""
    cutoff = time.time() - window_seconds
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, text FROM statements
            WHERE ingested_at >= ? AND duplicate_of_id IS NULL
            ORDER BY ingested_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def insert_statement(
    raw: RawStatement,
    classification: Optional[Classification],
    duplicate_of_id: Optional[int] = None,
) -> Optional[int]:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO statements
                (source, source_id, text, url, published_at, ingested_at,
                 is_market_relevant, sentiment, confidence, tickers, sectors, reasoning,
                 duplicate_of_id, related_topic_id, is_major_escalation,
                 expected_move_pct, expected_horizon)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw.source,
                raw.source_id,
                raw.text,
                raw.url,
                raw.published_at,
                time.time(),
                int(classification.is_market_relevant) if classification else None,
                classification.sentiment if classification else None,
                classification.confidence if classification else None,
                json.dumps(classification.ticker_calls) if classification else None,
                json.dumps(classification.sectors) if classification else None,
                classification.reasoning if classification else None,
                duplicate_of_id,
                classification.related_topic_id if classification else None,
                int(classification.is_major_escalation) if classification else 0,
                classification.expected_move_pct if classification else None,
                classification.expected_horizon if classification else None,
            ),
        )
        if cur.rowcount == 0:
            # INSERT OR IGNORE hat wegen eines UNIQUE-Konflikts (source_id existiert
            # schon - z.B. dieselbe Meldung zweimal in derselben Charge durch
            # ueberlappende Quellen-Ergebnisse) nichts eingefuegt. cur.lastrowid waere
            # in diesem Fall irrefuehrend 0 (keine gueltige ID) - stattdessen die ID
            # der tatsaechlich existierenden Zeile nachschlagen und zurueckgeben.
            row = conn.execute(
                "SELECT id FROM statements WHERE source_id = ?", (raw.source_id,)
            ).fetchone()
            return row["id"] if row else None
        return cur.lastrowid


def mark_alert_sent(statement_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE statements SET alert_sent = 1 WHERE id = ?", (statement_id,)
        )


def _normalize_ticker_calls(raw_tickers: list) -> list[dict]:
    # Vor der Einfuehrung von Long/Short-Empfehlungen wurden hier einfache
    # Ticker-Strings gespeichert; vor der Einfuehrung von Pro-Ticker-Konfidenz fehlte
    # das "confidence"-Feld in sonst schon dict-foermigen Eintraegen. Alte, aus einem
    # GitHub-Actions-Cache ueberlebende Zeilen sollen die Anzeige nicht zum Absturz
    # bringen bzw. keinen KeyError/None-Vergleich beim Sortieren nach Konfidenz ausloesen.
    normalized = []
    for t in raw_tickers:
        if isinstance(t, str):
            normalized.append({"ticker": t, "direction": None, "confidence": None, "reasoning": ""})
        else:
            t.setdefault("confidence", None)
            normalized.append(t)
    return normalized


def get_recent(limit: int = 50, only_relevant: bool = False) -> list[dict]:
    query = "SELECT * FROM statements"
    if only_relevant:
        query += " WHERE is_market_relevant = 1"
    query += " ORDER BY ingested_at DESC LIMIT ?"
    with get_conn() as conn:
        rows = conn.execute(query, (limit,)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["tickers"] = _normalize_ticker_calls(json.loads(d["tickers"]) if d["tickers"] else [])
            d["sectors"] = json.loads(d["sectors"]) if d["sectors"] else []
            result.append(d)
        return result


def get_recent_alerted(hours: int = 24, limit: int = 20) -> list[dict]:
    """Kuerzlich alarmierte Statements - dient als Kontext beim Klassifizieren
    neuer Statements, damit Claude erkennen kann, ob eine neue Meldung im Kern
    zu einem heute schon gemeldeten Thema gehoert (siehe app/classifier.py)."""
    cutoff = time.time() - hours * 3600
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, text, sentiment FROM statements
            WHERE alert_sent = 1 AND ingested_at >= ?
            ORDER BY ingested_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_pending_alerts(hours: int = 24) -> list[dict]:
    """Statements, die als marktrelevant klassifiziert wurden, aber nie tatsaechlich
    alarmiert wurden (z.B. weil der Prozess mitten im Lauf abgebrochen wurde, bevor
    der Telegram-Versand drankam, oder weil der Versand selbst fehlgeschlagen ist).
    Wird bei jedem Zyklus erneut versucht, damit ein abgebrochener Lauf keine
    marktrelevante Meldung stillschweigend verliert."""
    cutoff = time.time() - hours * 3600
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM statements
            WHERE is_market_relevant = 1 AND alert_sent = 0 AND duplicate_of_id IS NULL
                AND ingested_at >= ?
            ORDER BY ingested_at ASC
            """,
            (cutoff,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["tickers"] = _normalize_ticker_calls(json.loads(d["tickers"]) if d["tickers"] else [])
            d["sectors"] = json.loads(d["sectors"]) if d["sectors"] else []
            result.append(d)
        return result


def _resolve_thread_root(conn, topic_id: int, max_hops: int = 20) -> int:
    """Folgt der related_topic_id/duplicate_of_id-Kette RUECKWAERTS bis zur
    eigentlichen Wurzel des Themas. Noetig, weil topic_id selbst schon eine
    Eskalation einer frueheren Meldung sein kann (Claude verlinkt bei einer
    Mehrfach-Eskalation nicht zwangslaeufig auf den urspruenglichen Ur-Alert,
    sondern kann genausogut auf den letzten Zwischenschritt zeigen) - ohne diese
    Aufloesung wuerde die Zeitleiste bei mehrfachen Eskalationen faelschlich nur
    den letzten Zwischenschritt als "Beginn" zeigen und fruehere Glieder verlieren.
    max_hops + seen-Set schuetzen vor einer (eigentlich unmoeglichen, da IDs nur
    auf strikt frueher eingefuegte Zeilen zeigen koennen) Zyklus-Endlosschleife."""
    current = topic_id
    seen = {current}
    for _ in range(max_hops):
        row = conn.execute(
            "SELECT related_topic_id, duplicate_of_id FROM statements WHERE id = ?",
            (current,),
        ).fetchone()
        if row is None:
            break
        nxt = row["related_topic_id"] or row["duplicate_of_id"]
        if nxt is None or nxt in seen:
            break
        current = nxt
        seen.add(current)
    return current


def get_topic_thread(topic_id: int, hours: int = 48, limit: int = 10) -> list[dict]:
    """Alle Meldungen, die zu einem Thema gehoeren, zeitlich aufsteigend sortiert -
    fuer eine kompakte Verlaufs-/Eskalations-Zeitleiste im Alert (#5).

    Loest zuerst die echte Wurzel auf (siehe _resolve_thread_root) und sammelt dann
    per rekursivem CTE die GESAMTE Kette, die (direkt oder ueber Zwischenglieder)
    darauf zeigt - ein einfacher Ein-Hop-Vergleich wuerde bei einer Mehrfach-
    Eskalation (C zeigt auf B, B zeigt auf die Wurzel A) das jeweils uebernaechste
    Kettenglied verlieren. hours begrenzt nur, wie weit zurueck ZUSAETZLICHE
    Kettenglieder gezeigt werden - die aufgeloeste Wurzel selbst wird nie allein
    wegen des Zeitfensters ausgeschlossen (sie ist dem Aufrufer per topic_id bereits
    bekannt/relevant, siehe orchestrator.py: topic_id stammt aus validiertem,
    zeitlich ohnehin begrenztem Themen-Kontext)."""
    cutoff = time.time() - hours * 3600
    with get_conn() as conn:
        root_id = _resolve_thread_root(conn, topic_id)
        rows = conn.execute(
            """
            WITH RECURSIVE thread_ids(id) AS (
                SELECT ?
                UNION
                SELECT s.id FROM statements s
                JOIN thread_ids t
                    ON s.related_topic_id = t.id OR s.duplicate_of_id = t.id
            )
            SELECT id, text, ingested_at, is_major_escalation, alert_sent
            FROM statements
            WHERE id IN (SELECT id FROM thread_ids)
                AND (id = ? OR ingested_at >= ?)
            ORDER BY ingested_at ASC
            LIMIT ?
            """,
            (root_id, root_id, cutoff, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def record_alert_baseline(
    statement_id: Optional[int],
    ticker: str,
    direction: Optional[str],
    confidence: Optional[float],
    alert_ts: float,
    alert_price: Optional[float],
) -> None:
    """Legt zum Alarm-Zeitpunkt den Ausgangskurs eines handelbaren Tickers ab (#2).
    INSERT OR IGNORE ueber UNIQUE(statement_id, ticker): ein erneuter Versuch (z.B.
    Resend) legt nicht doppelt an und ueberschreibt den urspruenglichen Kurs nicht."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO alert_outcomes
                (statement_id, ticker, direction, confidence, alert_ts, alert_price)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (statement_id, ticker, direction, confidence, alert_ts, alert_price),
        )


def get_outcomes_awaiting_followup(horizon_seconds: float, limit: int = 50) -> list[dict]:
    """Offene Ergebnis-Datensaetze, deren Horizont abgelaufen ist und die einen
    Ausgangskurs haben, aber noch keine Nachmessung (followup_price IS NULL)."""
    cutoff = time.time() - horizon_seconds
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, statement_id, ticker, direction, confidence, alert_ts, alert_price
            FROM alert_outcomes
            WHERE followup_price IS NULL AND alert_price IS NOT NULL AND alert_ts <= ?
            ORDER BY alert_ts ASC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def set_outcome_followup(
    outcome_id: int,
    followup_ts: float,
    followup_price: float,
    return_pct: float,
    correct: bool,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE alert_outcomes
            SET followup_ts = ?, followup_price = ?, return_pct = ?, correct = ?
            WHERE id = ?
            """,
            (followup_ts, followup_price, return_pct, int(correct), outcome_id),
        )


def get_calibration_stats() -> dict:
    """Aggregiert die ausgewerteten Ergebnisse (#3): Gesamt-Trefferquote und je
    Konfidenz-Bucket, plus je Richtung. Nur Datensaetze mit vorliegender Nachmessung
    (correct IS NOT NULL) zaehlen."""
    with get_conn() as conn:
        overall = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS hits,
                   AVG(return_pct) AS avg_return
            FROM alert_outcomes WHERE correct IS NOT NULL
            """
        ).fetchone()
        by_bucket = conn.execute(
            """
            -- MIN(..., 9) klemmt eine Konfidenz von genau 1.0 in den 90-100%-Bucket
            -- (bucket=9) statt einen eigenen, unsinnigen "100-110%"-Bucket (bucket=10)
            -- zu erzeugen - CAST(1.0*10 AS INT)=10 waere sonst ein eigener Eintrag,
            -- der den 90-100%-Bucket kuenstlich aufspaltet (Konfidenz ist auf [0,1]
            -- geklemmt, siehe classifier.py: _clamped_confidence, 1.0 ist also ein
            -- ganz normaler, haeufiger Wert).
            SELECT MIN(CAST(confidence * 10 AS INT), 9) AS bucket,
                   COUNT(*) AS n,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS hits
            FROM alert_outcomes
            WHERE correct IS NOT NULL AND confidence IS NOT NULL
            GROUP BY bucket ORDER BY bucket DESC
            """
        ).fetchall()
        by_direction = conn.execute(
            """
            SELECT direction, COUNT(*) AS n,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS hits
            FROM alert_outcomes WHERE correct IS NOT NULL
            GROUP BY direction
            """
        ).fetchall()

    n = overall["n"] or 0
    hits = overall["hits"] or 0
    pending = None
    with get_conn() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM alert_outcomes WHERE correct IS NULL"
        ).fetchone()["c"]
    return {
        "evaluated": n,
        "hits": hits,
        "hit_rate": (hits / n) if n else None,
        "avg_return_pct": overall["avg_return"],
        "pending": pending,
        "by_confidence_bucket": [
            {"bucket": f"{row['bucket'] * 10}-{row['bucket'] * 10 + 10}%",
             "n": row["n"], "hits": row["hits"],
             "hit_rate": (row["hits"] / row["n"]) if row["n"] else None}
            for row in by_bucket
        ],
        "by_direction": {
            row["direction"]: {"n": row["n"], "hits": row["hits"],
                               "hit_rate": (row["hits"] / row["n"]) if row["n"] else None}
            for row in by_direction
        },
    }


def record_ticker_alert(
    statement_id: Optional[int], ticker: str, direction: Optional[str], alerted_at: float
) -> None:
    """Protokolliert einen tatsaechlich verschickten Alert fuer einen handelbaren
    Ticker+Richtung (fuer den Cooldown, #6). Raeumt bei der Gelegenheit Eintraege aelter
    als 30 Tage weg, damit die Tabelle nicht unbegrenzt waechst."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ticker_alerts (statement_id, ticker, direction, alerted_at) "
            "VALUES (?, ?, ?, ?)",
            (statement_id, (ticker or "").upper(), direction, alerted_at),
        )
        conn.execute(
            "DELETE FROM ticker_alerts WHERE alerted_at < ?", (alerted_at - 30 * 86400,)
        )


def ticker_in_cooldown(ticker: str, direction: Optional[str], since_ts: float) -> bool:
    """True, wenn fuer diesen Ticker+Richtung seit since_ts bereits ein Alert
    verschickt wurde (Cooldown noch aktiv, #6)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM ticker_alerts WHERE ticker = ? AND direction = ? "
            "AND alerted_at >= ? LIMIT 1",
            ((ticker or "").upper(), direction, since_ts),
        ).fetchone()
    return row is not None


def get_corroboration_count(statement_id: int) -> int:
    """Wie viele DISTINKTE Quellen dieselbe Meldung gebracht haben (#15): die Meldung
    selbst plus alle als Text-Duplikat auf sie zeigenden Meldungen. >1 = unabhaengig
    bestaetigt (staerkeres Signal)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT source) AS c FROM statements "
            "WHERE id = ? OR duplicate_of_id = ?",
            (statement_id, statement_id),
        ).fetchone()
    return row["c"] or 1


def get_ticker_hitrate(ticker: str) -> Optional[dict]:
    """Historische Trefferquote fuer einen Ticker aus den bereits ausgewerteten
    Ergebnissen (#9): {'n', 'hits', 'hit_rate'} oder None, wenn es noch keine
    ausgewerteten Alerts fuer diesen Ticker gibt (dann wird im Alert nichts angezeigt)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS hits "
            "FROM alert_outcomes WHERE ticker = ? AND correct IS NOT NULL",
            ((ticker or "").upper(),),
        ).fetchone()
    n = row["n"] or 0
    if n == 0:
        return None
    hits = row["hits"] or 0
    return {"n": n, "hits": hits, "hit_rate": hits / n}


def get_alerts_sent_today() -> int:
    """Anzahl heute (seit Mitternacht UTC) verschickter Alerts - Metrik fuer /api/health."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM ticker_alerts WHERE alerted_at >= ?",
            (_utc_day_start_epoch(),),
        ).fetchone()
    return row["c"] or 0


def get_performance_stats(limit: int = 5) -> dict:
    """Aggregierte Performance der ausgewerteten Alerts (#11): Gesamtzahl, Trefferquote,
    Durchschnittsrendite sowie die besten/schlechtesten Ticker nach mittlerer Rendite.
    Nur Datensaetze mit vorliegender Nachmessung (return_pct IS NOT NULL)."""
    with get_conn() as conn:
        overall = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS hits, "
            "AVG(return_pct) AS avg_return "
            "FROM alert_outcomes WHERE return_pct IS NOT NULL"
        ).fetchone()
        by_ticker_rows = conn.execute(
            """
            SELECT ticker, COUNT(*) AS n,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS hits,
                   AVG(return_pct) AS avg_return
            FROM alert_outcomes
            WHERE return_pct IS NOT NULL
            GROUP BY ticker
            ORDER BY avg_return DESC
            """
        ).fetchall()

    n = overall["n"] or 0
    hits = overall["hits"] or 0
    by_ticker = [
        {
            "ticker": r["ticker"],
            "n": r["n"],
            "hits": r["hits"],
            "hit_rate": (r["hits"] / r["n"]) if r["n"] else None,
            "avg_return_pct": r["avg_return"],
        }
        for r in by_ticker_rows
    ]
    return {
        "evaluated": n,
        "hits": hits,
        "hit_rate": (hits / n) if n else None,
        "avg_return_pct": overall["avg_return"],
        "best_tickers": by_ticker[:limit],
        "worst_tickers": list(reversed(by_ticker[-limit:])) if by_ticker else [],
    }


def count_alerted_statements_since(since_ts: float) -> int:
    """Anzahl DISTINKTER alarmierter Statements seit since_ts (fuer das Alerts-pro-Stunde-
    Ratelimit, #6). ticker_alerts hat eine Zeile pro Ticker - hier zaehlen aber die
    Meldungen (= Nachrichten), daher DISTINCT statement_id."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT statement_id) AS c FROM ticker_alerts WHERE alerted_at >= ?",
            (since_ts,),
        ).fetchone()
    return row["c"] or 0


def get_last_alert_direction(ticker: str, since_ts: float) -> Optional[str]:
    """Zuletzt fuer diesen Ticker alarmierte Richtung ('long'/'short') seit since_ts -
    fuer die Richtungswechsel-Erkennung (#2). None, wenn es keinen juengeren Alert gibt."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT direction FROM ticker_alerts WHERE ticker = ? AND alerted_at >= ? "
            "ORDER BY alerted_at DESC LIMIT 1",
            ((ticker or "").upper(), since_ts),
        ).fetchone()
    return row["direction"] if row else None


def get_recent_alerted_sector_counts(hours: int) -> dict:
    """Wie oft je Sektor in den letzten `hours` Stunden alarmiert wurde (fuer den
    Sektor-Cluster-Hinweis, #4). Sektoren stehen als JSON-Liste in statements.sectors."""
    cutoff = time.time() - hours * 3600
    counts: dict[str, int] = {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sectors FROM statements WHERE alert_sent = 1 AND ingested_at >= ?",
            (cutoff,),
        ).fetchall()
    for row in rows:
        try:
            sectors = json.loads(row["sectors"]) if row["sectors"] else []
        except (TypeError, ValueError):
            continue
        for sec in sectors:
            if isinstance(sec, str) and sec.strip():
                key = sec.strip()
                counts[key] = counts.get(key, 0) + 1
    return counts


def get_source_reliability() -> list[dict]:
    """Trefferquote je Quelle (#7): verbindet die ausgewerteten Ergebnisse mit der Quelle
    des ausloesenden Statements. Nur Datensaetze mit vorliegender Nachmessung."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.source AS source, COUNT(*) AS n,
                   SUM(CASE WHEN o.correct = 1 THEN 1 ELSE 0 END) AS hits,
                   AVG(o.return_pct) AS avg_return
            FROM alert_outcomes o JOIN statements s ON o.statement_id = s.id
            WHERE o.correct IS NOT NULL
            GROUP BY s.source
            ORDER BY n DESC
            """
        ).fetchall()
    return [
        {"source": r["source"], "n": r["n"], "hits": r["hits"],
         "hit_rate": (r["hits"] / r["n"]) if r["n"] else None,
         "avg_return_pct": r["avg_return"]}
        for r in rows
    ]


def get_kelly_inputs() -> dict:
    """Eingaben fuer den Kelly-lite Positionsanteil (#5): Gesamt-Trefferquote sowie
    mittlerer GEWINN- und VERLUST-Betrag (jeweils |return_pct|) aus den ausgewerteten
    Ergebnissen. hit_rate/avg_win_pct/avg_loss_pct sind None, wenn es dafuer keine Daten
    gibt."""
    with get_conn() as conn:
        overall = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS hits "
            "FROM alert_outcomes WHERE correct IS NOT NULL"
        ).fetchone()
        win = conn.execute(
            "SELECT AVG(ABS(return_pct)) AS a FROM alert_outcomes "
            "WHERE correct = 1 AND return_pct IS NOT NULL"
        ).fetchone()
        loss = conn.execute(
            "SELECT AVG(ABS(return_pct)) AS a FROM alert_outcomes "
            "WHERE correct = 0 AND return_pct IS NOT NULL"
        ).fetchone()
    n = overall["n"] or 0
    return {
        "hit_rate": (overall["hits"] / n) if n else None,
        "avg_win_pct": win["a"],
        "avg_loss_pct": loss["a"],
        "n": n,
    }


def get_outcomes_for_export(limit: int = 1000) -> list[dict]:
    """Ausgewertete (und offene) Ergebnis-Datensaetze fuer den CSV-Export (#9), inkl.
    Quelle des ausloesenden Statements. Neueste zuerst."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT o.id, o.statement_id, s.source AS source, o.ticker, o.direction,
                   o.confidence, o.alert_ts, o.alert_price, o.followup_ts,
                   o.followup_price, o.return_pct, o.correct
            FROM alert_outcomes o LEFT JOIN statements s ON o.statement_id = s.id
            ORDER BY o.alert_ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_paper_position(
    statement_id: Optional[int],
    ticker: str,
    direction: str,
    entry_price: float,
    entry_ts: float,
    qty: float,
    stake: float,
    stop: Optional[float],
    target: Optional[float],
) -> int:
    """Legt eine neue offene virtuelle Position an (Paper-Trading) und gibt ihre ID zurueck."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_positions
                (statement_id, ticker, direction, entry_price, entry_ts, qty, stake,
                 stop, target, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (statement_id, (ticker or "").upper(), direction, entry_price, entry_ts,
             qty, stake, stop, target),
        )
        return cur.lastrowid


def get_open_paper_positions() -> list[dict]:
    """Alle aktuell offenen virtuellen Positionen (aelteste zuerst)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status = 'open' ORDER BY entry_ts ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_paper_position_for_ticker(ticker: str) -> Optional[dict]:
    """Die offene Position fuer einen Ticker (oder None) - fuer die Erkennung, ob schon
    eine Position laeuft (nicht doppelt eroeffnen bzw. bei Gegenrichtung schliessen)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM paper_positions WHERE status = 'open' AND ticker = ? "
            "ORDER BY entry_ts DESC LIMIT 1",
            ((ticker or "").upper(),),
        ).fetchone()
    return dict(row) if row else None


def count_open_paper_positions() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM paper_positions WHERE status = 'open'"
        ).fetchone()
    return row["c"] or 0


def close_paper_position(
    position_id: int, exit_price: float, exit_ts: float, pnl: float, reason: str
) -> None:
    """Schliesst eine offene Position: haelt Ausstiegskurs, realisierten Gewinn/Verlust
    (EUR) und den Grund fest. Die WHERE-Bedingung status='open' macht den Aufruf
    idempotent - eine bereits geschlossene Position wird nicht erneut veraendert."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE paper_positions
            SET status = 'closed', exit_price = ?, exit_ts = ?, pnl = ?, close_reason = ?
            WHERE id = ? AND status = 'open'
            """,
            (exit_price, exit_ts, pnl, reason, position_id),
        )


def get_paper_realized_pnl() -> float:
    """Summe der realisierten Gewinne/Verluste (EUR) aller geschlossenen Positionen."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) AS s FROM paper_positions WHERE status = 'closed'"
        ).fetchone()
    return row["s"] or 0.0


def get_open_paper_stake_sum() -> float:
    """Summe des in offenen Positionen gebundenen Einsatzkapitals (EUR)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(stake), 0.0) AS s FROM paper_positions WHERE status = 'open'"
        ).fetchone()
    return row["s"] or 0.0


def get_paper_closed_stats() -> dict:
    """Kennzahlen der geschlossenen virtuellen Positionen: Anzahl, Treffer (pnl > 0) und
    realisierter Gesamtgewinn (fuer den Depot-Status)."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(pnl), 0.0) AS realized
            FROM paper_positions WHERE status = 'closed'
            """
        ).fetchone()
    n = row["n"] or 0
    return {"closed": n, "wins": row["wins"] or 0, "realized_pnl": row["realized"] or 0.0}


def get_stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM statements").fetchone()["c"]
        relevant = conn.execute(
            "SELECT COUNT(*) AS c FROM statements WHERE is_market_relevant = 1"
        ).fetchone()["c"]
        duplicates = conn.execute(
            "SELECT COUNT(*) AS c FROM statements WHERE duplicate_of_id IS NOT NULL"
        ).fetchone()["c"]
        alerts_sent = conn.execute(
            "SELECT COUNT(*) AS c FROM statements WHERE alert_sent = 1"
        ).fetchone()["c"]
        sentiment_rows = conn.execute(
            """
            SELECT sentiment, COUNT(*) AS c FROM statements
            WHERE is_market_relevant = 1 AND sentiment IS NOT NULL
            GROUP BY sentiment
            """
        ).fetchall()
        by_source_rows = conn.execute(
            "SELECT source, COUNT(*) AS c FROM statements GROUP BY source"
        ).fetchall()

    return {
        "total": total,
        "market_relevant": relevant,
        "duplicates": duplicates,
        "alerts_sent": alerts_sent,
        "sentiment_breakdown": {r["sentiment"]: r["c"] for r in sentiment_rows},
        "by_source": {r["source"]: r["c"] for r in by_source_rows},
    }
