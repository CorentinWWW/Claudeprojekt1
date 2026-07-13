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
    duplicate_of_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_statements_ingested_at ON statements(ingested_at DESC);
"""


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
    tickers: list = field(default_factory=list)
    sectors: list = field(default_factory=list)
    reasoning: str = ""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def is_known(source_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM statements WHERE source_id = ?", (source_id,)
        ).fetchone()
        return row is not None


def find_recent_duplicate(
    text: str,
    window_seconds: int = DEDUP_WINDOW_SECONDS,
    threshold: float = DEDUP_SIMILARITY_THRESHOLD,
) -> Optional[dict]:
    """Sucht unter kuerzlichen Statements eines mit sehr aehnlichem Text.

    Verhindert, dass dieselbe reale Aussage (z.B. von GDELT UND RSS gemeldet)
    doppelt klassifiziert wird und doppelte Alerts ausloest.
    """
    cutoff = time.time() - window_seconds
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM statements
            WHERE ingested_at >= ? AND duplicate_of_id IS NULL
            ORDER BY ingested_at DESC
            LIMIT 200
            """,
            (cutoff,),
        ).fetchall()

    for row in rows:
        if text_similarity(text, row["text"]) >= threshold:
            return dict(row)
    return None


def insert_statement(
    raw: RawStatement,
    classification: Optional[Classification],
    duplicate_of_id: Optional[int] = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO statements
                (source, source_id, text, url, published_at, ingested_at,
                 is_market_relevant, sentiment, confidence, tickers, sectors, reasoning,
                 duplicate_of_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(classification.tickers) if classification else None,
                json.dumps(classification.sectors) if classification else None,
                classification.reasoning if classification else None,
                duplicate_of_id,
            ),
        )
        return cur.lastrowid


def mark_alert_sent(statement_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE statements SET alert_sent = 1 WHERE id = ?", (statement_id,)
        )


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
            d["tickers"] = json.loads(d["tickers"]) if d["tickers"] else []
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
