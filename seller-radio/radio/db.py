"""SQLite storage layer: schema definition, connection setup, and sync bookkeeping."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from radio.config import DB_PATH

BUSY_TIMEOUT_SECONDS: float = 30.0

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id INTEGER PRIMARY KEY,
    seller TEXT NOT NULL,
    release_id INTEGER NOT NULL,
    price REAL,
    currency TEXT,
    condition TEXT,
    sleeve TEXT,
    ships_from TEXT,
    listing_uri TEXT,
    comments TEXT,
    posted TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    release_id INTEGER PRIMARY KEY,
    artist TEXT,
    title TEXT,
    label TEXT,
    catno TEXT,
    year INTEGER,
    country TEXT,
    genres TEXT,
    styles TEXT,
    formats TEXT,
    format_descriptions TEXT,
    master_id INTEGER,
    have INTEGER,
    want INTEGER,
    rating REAL,
    rating_count INTEGER,
    has_video INTEGER NOT NULL DEFAULT 0,
    video_source TEXT,
    thumb TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    release_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT,
    duration INTEGER,
    position INTEGER,
    dead INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (release_id, video_id)
);

CREATE TABLE IF NOT EXISTS pool (
    release_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    bank_key TEXT,
    discovered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owned (
    release_id INTEGER PRIMARY KEY,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wantlist (
    release_id INTEGER PRIMARY KEY,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    key TEXT PRIMARY KEY,
    synced_at TEXT NOT NULL,
    n INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_release ON listings (release_id);
CREATE INDEX IF NOT EXISTS idx_listings_seller_active ON listings (seller, active);
CREATE INDEX IF NOT EXISTS idx_releases_has_video ON releases (has_video);
CREATE INDEX IF NOT EXISTS idx_pool_source ON pool (source);
CREATE INDEX IF NOT EXISTS idx_videos_release ON videos (release_id);
"""


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection configured for this project.

    Enables row access by column name, write ahead logging, and foreign key enforcement.

    Args:
        db_path: Path to the database file, or ":memory:" for an ephemeral database.

    Returns:
        An open SQLite connection.
    """
    target = str(db_path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create every table and index if they do not already exist.

    Args:
        conn: Open SQLite connection.

    Returns:
        None.
    """
    conn.executescript(SCHEMA)
    conn.commit()


def now_iso() -> str:
    """Return the current UTC timestamp.

    Returns:
        An ISO 8601 timestamp in UTC with second precision.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def record_sync(conn: sqlite3.Connection, key: str, n: int) -> None:
    """Record the outcome of a sync step.

    Args:
        conn: Open SQLite connection.
        key: Stable identifier of the sync step, such as "seller:allcitydublin".
        n: Number of items processed by the step.

    Returns:
        None.
    """
    conn.execute(
        "INSERT INTO sync_log (key, synced_at, n) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET synced_at=excluded.synced_at, n=excluded.n",
        (key, now_iso(), n),
    )
    conn.commit()
