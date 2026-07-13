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
    is_major_escalation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_statements_ingested_at ON statements(ingested_at DESC);
"""

# Fuer DBs, die vor der Einfuehrung von related_topic_id/is_major_escalation angelegt
# wurden (z.B. aus einem alten GitHub-Actions-Cache wiederhergestellt): CREATE TABLE
# IF NOT EXISTS aendert eine bereits existierende Tabelle nicht, daher per ALTER TABLE
# nachruesten, statt dass insert_statement() spaeter mit "no such column" abstuerzt.
_MIGRATION_COLUMNS = {
    "related_topic_id": "ALTER TABLE statements ADD COLUMN related_topic_id INTEGER",
    "is_major_escalation": "ALTER TABLE statements ADD COLUMN is_major_escalation INTEGER NOT NULL DEFAULT 0",
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
                 duplicate_of_id, related_topic_id, is_major_escalation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    # Ticker-Strings gespeichert. Alte, aus einem GitHub-Actions-Cache
    # ueberlebende Zeilen sollen die Anzeige nicht zum Absturz bringen.
    normalized = []
    for t in raw_tickers:
        if isinstance(t, str):
            normalized.append({"ticker": t, "direction": None, "reasoning": ""})
        else:
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
