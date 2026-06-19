"""SQLite schema bootstrap."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id              TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    media_paths     TEXT NOT NULL DEFAULT '[]',   -- JSON array
    targets         TEXT NOT NULL DEFAULT '[]',   -- JSON array
    status          TEXT NOT NULL,
    scheduled_at    TEXT,                          -- ISO8601 UTC
    created_at      TEXT NOT NULL,
    published_at    TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}'    -- JSON object
);

CREATE INDEX IF NOT EXISTS idx_posts_status       ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_scheduled_at ON posts(scheduled_at);

CREATE TABLE IF NOT EXISTS post_targets (
    post_id     TEXT NOT NULL,
    platform    TEXT NOT NULL,
    status      TEXT NOT NULL,
    remote_id   TEXT,
    remote_url  TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (post_id, platform),
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS publish_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      TEXT NOT NULL,
    platform     TEXT NOT NULL,
    success      INTEGER NOT NULL,                 -- 0 / 1
    error        TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_log_post_id  ON publish_log(post_id);
CREATE INDEX IF NOT EXISTS idx_log_created  ON publish_log(created_at);
"""


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Open (and if needed create) the queue database, returning a connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
