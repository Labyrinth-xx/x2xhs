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
        normalized = handle.strip().lstrip("@")
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
            if cursor.rowcount == 0:
                # May exist with different casing; check case-insensitively
                row = await (await conn.execute(
                    "SELECT handle FROM monitored_accounts WHERE LOWER(handle)=?",
                    (normalized.lower(),),
                )).fetchone()
                return row is None  # False = already exists
        return True

    async def remove_account(self, handle: str) -> bool:
        normalized = handle.strip().lstrip("@")
        if not normalized:
            return False
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM monitored_accounts WHERE LOWER(handle) = LOWER(?)",
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

    async def list_candidate_tweets(
        self,
        limit: int,
        handles: Sequence[str] | None = None,
        force: bool = False,
        min_score: int | None = None,
        published_within_hours: int | None = 168,
    ) -> list[RawTweet]:
        join_condition = "p.status = 'filtered'"
        if not force:
            join_condition += (
                " OR (p.status = 'sent' AND p.pushed_at > datetime('now', '-7 days'))"
            )

        params: list[object] = []
        handle_clause = ""
        if handles:
            lower_handles = [h.lower() for h in handles]
            placeholders = ",".join("?" for _ in lower_handles)
            handle_clause = f"AND LOWER(t.handle) IN ({placeholders})"
            params.extend(lower_handles)

        score_clause = ""
        if min_score is not None:
            score_clause = "AND t.filter_score >= ?"
            params.append(min_score)

        time_clause = ""
        if published_within_hours is not None:
            time_clause = f"AND t.published_at > datetime('now', '-{published_within_hours} hours')"

        params.append(limit)

        async with self._database.connect() as conn:
            cursor = await conn.execute(
                f"""
                SELECT t.*
                FROM tweets t
                LEFT JOIN processed_content p
                    ON p.tweet_external_id = t.external_id
                    AND ({join_condition})
                WHERE p.tweet_external_id IS NULL
                {handle_clause}
                {score_clause}
                {time_clause}
                ORDER BY t.published_at DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [self._row_to_tweet(row) for row in rows]

    async def save_sent(self, content: ProcessedContent) -> None:
        async with self._database.connect() as conn:
            await conn.execute(
                """
                INSERT INTO processed_content (
                    tweet_external_id, handle, raw_url, published_at,
                    title_zh, body_zh, tags_json, status, mode, pushed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(tweet_external_id) DO UPDATE SET
                    handle=excluded.handle,
                    raw_url=excluded.raw_url,
                    published_at=excluded.published_at,
                    title_zh=excluded.title_zh,
                    body_zh=excluded.body_zh,
                    tags_json=excluded.tags_json,
                    status='sent',
                    mode=excluded.mode,
                    pushed_at=CURRENT_TIMESTAMP,
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
                    ProcessedStatus.SENT.value,
                    content.mode,
                ),
            )

    async def save_filtered(
        self,
        tweet_external_id: str,
        handle: str,
        raw_url: str,
        published_at: datetime,
    ) -> None:
        async with self._database.connect() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO processed_content (
                    tweet_external_id, handle, raw_url, published_at,
                    title_zh, body_zh, tags_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tweet_external_id,
                    handle,
                    raw_url,
                    published_at.isoformat(),
                    "[内容太短]",
                    "",
                    "[]",
                    ProcessedStatus.FILTERED.value,
                ),
            )

    # ── 评分相关 ──

    async def save_score(
        self,
        external_id: str,
        score: float,
        reason: str,
        detail: dict | None = None,
    ) -> None:
        preview_title = detail.pop("preview_title", None) if detail else None
        detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
        async with self._database.connect() as conn:
            await conn.execute(
                "UPDATE tweets SET filter_score = ?, filter_reason = ?, filter_scores_detail = ?, preview_title = ? WHERE external_id = ?",
                (score, reason, detail_json, preview_title, external_id),
            )

    async def list_unscored_tweets(self, limit: int = 50, published_within_hours: int = 168) -> list[RawTweet]:
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM tweets
                WHERE filter_score IS NULL
                  AND published_at > datetime('now', '-' || ? || ' hours')
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (published_within_hours, limit),
            )
            rows = await cursor.fetchall()
        return [self._row_to_tweet(row) for row in rows]

    async def list_scored_candidates(
        self,
        min_score: float,
        limit: int = 5,
    ) -> list[dict]:
        """返回达到阈值、尚未处理的推文（按分数降序）。"""
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT t.external_id, t.handle, t.content, t.url,
                       t.published_at, t.filter_score, t.filter_reason,
                       t.source_type, t.source_value, t.image_urls, t.preview_title
                FROM tweets t
                LEFT JOIN processed_content p
                    ON p.tweet_external_id = t.external_id
                WHERE p.tweet_external_id IS NULL
                  AND t.filter_score >= ?
                  AND t.published_at > datetime('now', '-7 days')
                ORDER BY t.filter_score DESC, t.published_at DESC
                LIMIT ?
                """,
                (min_score, limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def expire_old_tweets(self, hours: int = 72) -> int:
        """将超时未处理的已评分推文标记为 filtered。"""
        async with self._database.connect() as conn:
            # 找到超时且未处理的推文
            cursor = await conn.execute(
                """
                SELECT t.external_id, t.handle, t.url, t.published_at
                FROM tweets t
                LEFT JOIN processed_content p ON p.tweet_external_id = t.external_id
                WHERE p.tweet_external_id IS NULL
                  AND t.filter_score IS NOT NULL
                  AND t.created_at < datetime('now', '-' || ? || ' hours')
                """,
                (hours,),
            )
            expired_rows = await cursor.fetchall()
            for row in expired_rows:
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO processed_content (
                        tweet_external_id, handle, raw_url, published_at,
                        title_zh, body_zh, tags_json, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["external_id"],
                        row["handle"],
                        row["url"],
                        row["published_at"],
                        "[过期]",
                        "",
                        "[]",
                        ProcessedStatus.FILTERED.value,
                    ),
                )
        return len(expired_rows)

    # ── 反馈相关 ──

    async def save_feedback(self, content: str) -> None:
        async with self._database.connect() as conn:
            await conn.execute(
                "INSERT INTO scorer_feedback (content) VALUES (?)",
                (content,),
            )

    async def list_recent_feedback(self, limit: int = 10) -> list[dict]:
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                "SELECT content, created_at FROM scorer_feedback ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def delete_feedback(self, feedback_id: int) -> bool:
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM scorer_feedback WHERE id = ?",
                (feedback_id,),
            )
        return cursor.rowcount > 0

    async def list_recent_scores(self, limit: int = 10) -> list[dict]:
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT handle, filter_score, filter_reason, filter_scores_detail,
                       substr(content, 1, 80) AS content_preview,
                       created_at
                FROM tweets
                WHERE filter_score IS NOT NULL
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def purge_expired_sent(self, ttl_days: int = 7) -> int:
        async with self._database.connect() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM processed_content
                WHERE status = ?
                  AND pushed_at < datetime('now', '-' || ? || ' days')
                """,
                (ProcessedStatus.SENT.value, ttl_days),
            )
        return cursor.rowcount

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
        counts: Counter[str] = Counter({
            ProcessedStatus.SENT.value: 0,
            ProcessedStatus.FILTERED.value: 0,
        })
        async with self._database.connect() as conn:
            cursor = await conn.execute("SELECT COUNT(*) AS total FROM tweets")
            tweet_row = await cursor.fetchone()
            counts["tweets"] = int(tweet_row["total"])
            cursor = await conn.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM processed_content
                WHERE status IN (?, ?)
                GROUP BY status
                """,
                (ProcessedStatus.SENT.value, ProcessedStatus.FILTERED.value),
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

