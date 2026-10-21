"""SQLite storage. One row per scored article or data-release event."""
import hashlib
import sqlite3
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    published_ts REAL NOT NULL,
    fetched_ts REAL NOT NULL,
    relevant INTEGER DEFAULT 0,
    scored INTEGER DEFAULT 0,
    direction INTEGER,          -- +1 bullish GBP/USD, -1 bearish, 0 neutral
    confidence REAL,            -- 0..1
    driver TEXT,
    reason TEXT,
    source_weight REAL,
    scorer TEXT                 -- 'rules' | 'llm' | 'surprise'
);
CREATE INDEX IF NOT EXISTS idx_pub ON articles(published_ts);
"""


def connect(path=None):
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def make_id(source, title):
    return hashlib.sha1(f"{source}|{title.strip().lower()}".encode()).hexdigest()


def insert_article(conn, source, title, url, published_ts, relevant, source_weight):
    """Insert if new. Returns True if inserted (dedupes on source+title)."""
    aid = make_id(source, title)
    cur = conn.execute(
        "INSERT OR IGNORE INTO articles (id, source, title, url, published_ts, fetched_ts,"
        " relevant, source_weight) VALUES (?,?,?,?,?,?,?,?)",
        (aid, source, title, url, published_ts, time.time(), int(relevant), source_weight),
    )
    conn.commit()
    return cur.rowcount == 1


def unscored(conn, limit=200):
    return conn.execute(
        "SELECT * FROM articles WHERE relevant=1 AND scored=0 ORDER BY published_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()


def save_score(conn, aid, direction, confidence, driver, reason, scorer):
    conn.execute(
        "UPDATE articles SET scored=1, direction=?, confidence=?, driver=?, reason=?, scorer=?"
        " WHERE id=?",
        (direction, confidence, driver, reason, scorer, aid),
    )
    conn.commit()


def scored_since(conn, since_ts):
    return conn.execute(
        "SELECT * FROM articles WHERE scored=1 AND published_ts>=? ORDER BY published_ts DESC",
        (since_ts,),
    ).fetchall()
