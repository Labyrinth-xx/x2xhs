from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Sequence

from scraper.models import ProcessedContent, ProcessedStatus, RawTweet
from storage.database import Database


class TweetRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_accounts(self) -> list[str]:
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT handle
                FROM monitored_accounts
                ORDER BY added_at DESC, handle ASC
                """
            )
            rows = await cursor.fetchall()
        return [str(row["handle"]) for row in rows]

    async def add_account(self, handle: str) -> bool:
        normalized = handle.strip().lstrip("@").lower()
        if not normalized:
            return False
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO monitored_accounts (handle)
                VALUES (?)
                """,
                (normalized,),
            )
        return cursor.rowcount > 0

    async def remove_account(self, handle: str) -> bool:
        normalized = handle.strip().lstrip("@").lower()
        if not normalized:
            return False
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM monitored_accounts WHERE handle = ?",
                (normalized,),
            )
        return cursor.rowcount > 0

    async def list_keywords(self) -> list[str]:
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                "SELECT keyword FROM monitored_keywords ORDER BY added_at DESC, keyword ASC"
            )
            rows = await cursor.fetchall()
        return [str(row["keyword"]) for row in rows]

    async def add_keyword(self, keyword: str) -> bool:
        normalized = keyword.strip()
        if not normalized:
            return False
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO monitored_keywords (keyword) VALUES (?)",
                (normalized,),
            )
        return cursor.rowcount > 0

    async def remove_keyword(self, keyword: str) -> bool:
        normalized = keyword.strip()
        if not normalized:
            return False
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM monitored_keywords WHERE keyword = ?",
                (normalized,),
            )
        return cursor.rowcount > 0

    async def save_tweets(self, tweets: Sequence[RawTweet]) -> int:
        inserted = 0
        for tweet in tweets:
            inserted += int(await self._upsert_tweet(tweet))
        return inserted

    async def _upsert_tweet(self, tweet: RawTweet) -> bool:
        exists = await self.get_tweet(tweet.external_id)
        async with self._database.connect() as conn:
            await conn.execute(
                """
                INSERT INTO tweets (
                    external_id, handle, content, url, published_at,
                    source_type, source_value, image_urls
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(external_id) DO UPDATE SET
                    handle=excluded.handle,
                    content=excluded.content,
                    url=excluded.url,
                    published_at=excluded.published_at,
                    source_type=excluded.source_type,
                    source_value=excluded.source_value,
                    image_urls=excluded.image_urls
                """,
                (
                    tweet.external_id,
                    tweet.handle,
                    tweet.content,
                    tweet.url,
                    tweet.published_at.isoformat(),
                    tweet.source_type,
                    tweet.source_value,
                    json.dumps(tweet.image_urls, ensure_ascii=False),
                ),
            )
        return exists is None

    async def get_tweet(self, external_id: str) -> RawTweet | None:
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM tweets WHERE external_id = ?",
                (external_id,),
            )
            row = await cursor.fetchone()
        return self._row_to_tweet(row) if row else None

    async def list_unprocessed_tweets(self, limit: int, handles: Sequence[str] | None = None) -> list[RawTweet]:
        async with self._database.connect() as conn:
            if handles:
                lower_handles = [h.lower() for h in handles]
                placeholders = ",".join("?" for _ in lower_handles)
                cursor = await conn.execute(
                    f"""
                    SELECT t.*
                    FROM tweets t
                    LEFT JOIN processed_content p
                        ON p.tweet_external_id = t.external_id
                    WHERE p.tweet_external_id IS NULL
                    AND LOWER(t.handle) IN ({placeholders})
                    ORDER BY t.published_at DESC
                    LIMIT ?
                    """,
                    (*lower_handles, limit),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT t.*
                    FROM tweets t
                    LEFT JOIN processed_content p
                        ON p.tweet_external_id = t.external_id
                    WHERE p.tweet_external_id IS NULL
                    ORDER BY t.published_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = await cursor.fetchall()
        return [self._row_to_tweet(row) for row in rows]

    async def save_processed_content(self, content: ProcessedContent) -> None:
        async with self._database.connect() as conn:
            await conn.execute(
                """
                INSERT INTO processed_content (
                    tweet_external_id, handle, raw_url, published_at, title_zh,
                    body_zh, tags_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_external_id) DO UPDATE SET
                    handle=excluded.handle,
                    raw_url=excluded.raw_url,
                    published_at=excluded.published_at,
                    title_zh=excluded.title_zh,
                    body_zh=excluded.body_zh,
                    tags_json=excluded.tags_json,
                    status=excluded.status,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    content.tweet_external_id,
                    content.handle,
                    content.raw_url,
                    content.published_at.isoformat(),
                    content.title_zh,
                    content.body_zh,
                    json.dumps(content.tags, ensure_ascii=False),
                    content.status.value,
                ),
            )

    async def list_content_with_tweets(
        self,
        statuses: Sequence[ProcessedStatus],
        limit: int,
        handles: Sequence[str] | None = None,
    ) -> list[tuple[ProcessedContent, RawTweet]]:
        status_placeholders = ",".join("?" for _ in statuses)
        params: list = [status.value for status in statuses]
        handle_clause = ""
        if handles:
            lower_handles = [h.lower() for h in handles]
            handle_placeholders = ",".join("?" for _ in lower_handles)
            handle_clause = f"AND LOWER(p.handle) IN ({handle_placeholders})"
            params.extend(lower_handles)
        params.append(limit)
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                f"""
                SELECT p.*, t.content AS tweet_content, t.source_type, t.source_value, t.image_urls
                FROM processed_content p
                JOIN tweets t ON t.external_id = p.tweet_external_id
                WHERE p.status IN ({status_placeholders})
                {handle_clause}
                ORDER BY p.updated_at DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [(self._row_to_content(row), self._row_to_joined_tweet(row)) for row in rows]

    async def update_processed_status(
        self,
        tweet_external_id: str,
        status: ProcessedStatus,
    ) -> None:
        async with self._database.connect() as conn:
            await conn.execute(
                """
                UPDATE processed_content
                SET status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tweet_external_id = ?
                """,
                (status.value, tweet_external_id),
            )

    async def mark_sent(self, tweet_external_id: str) -> None:
        async with self._database.connect() as conn:
            await conn.execute(
                """
                UPDATE processed_content
                SET status = ?,
                    pushed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tweet_external_id = ?
                """,
                (ProcessedStatus.SENT.value, tweet_external_id),
            )

    async def log_scrape(
        self,
        source_type: str,
        source_value: str,
        endpoint: str,
        fetched_count: int,
        success: bool,
        message: str | None = None,
    ) -> None:
        async with self._database.connect() as conn:
            await conn.execute(
                """
                INSERT INTO scrape_log (
                    source_type, source_value, endpoint, fetched_count, success, message
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_type, source_value, endpoint, fetched_count, int(success), message),
            )

    async def status_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        async with self._database.connect() as conn:
            cursor = await conn.execute("SELECT COUNT(*) AS total FROM tweets")
            tweet_row = await cursor.fetchone()
            counts["tweets"] = int(tweet_row["total"])
            cursor = await conn.execute(
                "SELECT status, COUNT(*) AS total FROM processed_content GROUP BY status"
            )
            for row in await cursor.fetchall():
                counts[row["status"]] = int(row["total"])
            cursor = await conn.execute("SELECT COUNT(*) AS total FROM scrape_log")
            log_row = await cursor.fetchone()
            counts["scrape_log"] = int(log_row["total"])
        return dict(counts)

    def _row_to_tweet(self, row: object) -> RawTweet:
        return RawTweet(
            external_id=row["external_id"],
            handle=row["handle"],
            content=row["content"],
            url=row["url"],
            published_at=datetime.fromisoformat(row["published_at"]),
            source_type=row["source_type"],
            source_value=row["source_value"],
            image_urls=tuple(json.loads(row["image_urls"])),
        )

    def _row_to_joined_tweet(self, row: object) -> RawTweet:
        return RawTweet(
            external_id=row["tweet_external_id"],
            handle=row["handle"],
            content=row["tweet_content"],
            url=row["raw_url"],
            published_at=datetime.fromisoformat(row["published_at"]),
            source_type=row["source_type"],
            source_value=row["source_value"],
            image_urls=tuple(json.loads(row["image_urls"])),
        )

    def _row_to_content(self, row: object) -> ProcessedContent:
        return ProcessedContent(
            tweet_external_id=row["tweet_external_id"],
            handle=row["handle"],
            raw_url=row["raw_url"],
            published_at=datetime.fromisoformat(row["published_at"]),
            title_zh=row["title_zh"],
            body_zh=row["body_zh"],
            tags=tuple(json.loads(row["tags_json"])),
            status=ProcessedStatus(row["status"]),
            pushed_at=row["pushed_at"] if "pushed_at" in row.keys() else None,
        )
