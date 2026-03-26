from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tweets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL UNIQUE,
    handle TEXT NOT NULL,
    content TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_value TEXT NOT NULL,
    image_urls TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS processed_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_external_id TEXT NOT NULL UNIQUE,
    handle TEXT NOT NULL,
    raw_url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    title_zh TEXT NOT NULL,
    body_zh TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    status TEXT NOT NULL,
    preview_path TEXT,
    publish_payload_path TEXT,
    pushed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(tweet_external_id) REFERENCES tweets(external_id)
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_value TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    fetched_count INTEGER NOT NULL,
    success INTEGER NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monitored_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monitored_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scorer_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as conn:
            await conn.executescript(SCHEMA_SQL)
            # Migration: add pushed_at column if it doesn't exist yet (idempotent)
            try:
                await conn.execute(
                    "ALTER TABLE processed_content ADD COLUMN pushed_at TEXT"
                )
            except Exception:
                pass  # Column already exists — safe to ignore
            # Migration: add mode column (idempotent)
            try:
                await conn.execute(
                    "ALTER TABLE processed_content ADD COLUMN mode TEXT NOT NULL DEFAULT 'light'"
                )
            except Exception:
                pass  # Column already exists — safe to ignore
            # Migration: add filter_score / filter_reason to tweets (idempotent)
            for col, col_type in [("filter_score", "INTEGER"), ("filter_reason", "TEXT")]:
                try:
                    await conn.execute(f"ALTER TABLE tweets ADD COLUMN {col} {col_type}")
                except Exception:
                    pass  # Column already exists
            # Migration: 5-state → 2-state (idempotent)
            await conn.execute(
                """
                UPDATE processed_content SET status = 'sent'
                WHERE status IN ('published', 'pushed', 'rejected', 'approved')
                """
            )

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()
