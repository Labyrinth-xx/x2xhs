"""关键词扫描编排器 — 执行完整的关键词扫描周期。

流程：
  1. 逐个查询 twscrape（顺序执行，查询间隔 2s）
  2. DB 去重（已存在的 external_id 跳过）+ 过滤 48h 以前的推文
  3. xAI 事件聚类（<3 条跳过，全部视为 KEEP）
  4. 每个聚类选代表：MERGE→综述 / BEST→选最佳 / KEEP→直通
  5. 关键词专用 5 维度评分（threshold 由 xai.keyword_score_threshold 决定）
  6. 未知作者 xAI 事实核查（互动量 >10k 跳过）
  7. 入候选池（source_label="关键词扫描"）
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from scraper.models import DigestContent, RawTweet
from scraper.keyword_queries import KeywordQuery, build_twitter_query

if TYPE_CHECKING:
    from config import AppConfig
    from processor.event_dedup import EventDeduplicator
    from processor.scorer import TweetScorer
    from scraper.twscrape_client import TwscrapeClient
    from scraper.xai_client import XAIClient
    from storage.tweet_repo import TweetRepository

logger = logging.getLogger(__name__)

# 推文最大年龄（超出的不进入候选池）
_MAX_TWEET_AGE_HOURS = 48
# 查询间隔，避免触发 Twitter 反爬
_QUERY_DELAY_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class SweepResult:
    """单次扫描周期的汇总结果。"""

    sweep_id: str
    total_fetched: int
    unique_after_dedup: int
    events_merged: int
    candidates_added: int
    duration_seconds: float
    errors: tuple[str, ...]


class KeywordSweepRunner:
    """关键词扫描编排器。

    由 Pipeline.keyword_sweep() 调用，所有重逻辑集中在此，
    pipeline.py 只做薄包装。
    """

    def __init__(
        self,
        twscrape: "TwscrapeClient",
        xai: "XAIClient | None",
        deduplicator: "EventDeduplicator | None",
        scorer: "TweetScorer",
        repo: "TweetRepository",
        config: "AppConfig",
    ) -> None:
        self._twscrape = twscrape
        self._xai = xai
        self._deduplicator = deduplicator
        self._scorer = scorer
        self._repo = repo
        self._config = config

    async def run_sweep(self, queries: list[KeywordQuery]) -> SweepResult:
        """执行完整扫描周期，返回汇总结果。"""
        sweep_id = str(uuid.uuid4())[:8]
        start = time.monotonic()
        errors: list[str] = []

        # Step 1: 收集原始推文
        raw_pool = await self._collect_tweets(queries, errors)
        logger.info("[sweep:%s] 抓取到 %d 条原始推文", sweep_id, len(raw_pool))

        # Step 2: DB 去重 + 年龄过滤
        fresh = await self._filter_fresh(raw_pool)
        logger.info("[sweep:%s] 去重后剩余 %d 条新推文", sweep_id, len(fresh))

        if not fresh:
            return self._make_result(sweep_id, len(raw_pool), 0, 0, 0, start, errors)

        # Step 3: 事件聚类
        clusters = await self._cluster_events(fresh, errors)
        events_merged = sum(1 for c in clusters if c.action == "MERGE")

        # Step 4: 每个聚类选代表（MERGE→综述, BEST→最佳, KEEP→原样）
        representatives = await self._select_representatives(clusters, errors)
        logger.info("[sweep:%s] 聚类后代表推文 %d 条", sweep_id, len(representatives))

        # Step 5: 关键词专用评分
        threshold = (
            self._config.xai.keyword_score_threshold
            if self._config.xai
            else 7.0
        )
        scored = await self._score_candidates(representatives, threshold, errors)
        logger.info("[sweep:%s] 评分通过 %d 条（阈值 %.1f）", sweep_id, len(scored), threshold)

        # Step 6: 事实核查（仅未知作者）
        verified = await self._fact_check(scored, errors)

        # Step 7: 入候选池
        added = await self._insert_to_pool(verified, errors)
        logger.info("[sweep:%s] 新增候选 %d 条", sweep_id, added)

        result = self._make_result(
            sweep_id, len(raw_pool), len(fresh), events_merged, added, start, errors
        )
        await self._save_log(result)
        return result

    # ── 内部步骤 ──

    async def _collect_tweets(
        self, queries: list[KeywordQuery], errors: list[str]
    ) -> list[RawTweet]:
        """逐个执行 twscrape 查询，顺序执行并间隔 2s。"""
        all_tweets: list[RawTweet] = []
        max_q = (
            self._config.xai.sweep_max_queries if self._config.xai else 10
        )
        for kq in queries[:max_q]:
            try:
                twitter_query = build_twitter_query(kq)
                tweets = await self._twscrape.fetch_keyword_tweets(
                    twitter_query,
                    min_faves_override=kq.min_faves,
                )
                # 标记来源类别
                tweets_with_category = [
                    RawTweet(
                        external_id=t.external_id,
                        handle=t.handle,
                        content=t.content,
                        url=t.url,
                        published_at=t.published_at,
                        source_type="keyword_sweep",
                        source_value=kq.category,
                        image_urls=t.image_urls,
                    )
                    for t in tweets
                ]
                all_tweets.extend(tweets_with_category)
                if tweets and kq.id is not None:
                    await self._repo.update_keyword_query_hits(kq.id)
                logger.debug("查询 [%s] 命中 %d 条", kq.category, len(tweets))
            except Exception as exc:
                msg = f"查询 [{kq.category}] 失败: {exc}"
                logger.warning(msg)
                errors.append(msg)
            if _QUERY_DELAY_SECONDS > 0:
                await asyncio.sleep(_QUERY_DELAY_SECONDS)
        return all_tweets

    async def _filter_fresh(self, tweets: list[RawTweet]) -> list[RawTweet]:
        """去除 DB 中已有的 + 超过 48h 的推文。"""
        if not tweets:
            return []
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=_MAX_TWEET_AGE_HOURS)
        # 年龄过滤（确保 aware datetime 后再比较）
        def _to_utc(dt: datetime) -> datetime:
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        recent = [t for t in tweets if _to_utc(t.published_at) >= cutoff]
        if not recent:
            return []
        # DB 去重
        existing_ids = await self._repo.batch_check_exists(
            [t.external_id for t in recent]
        )
        return [t for t in recent if t.external_id not in existing_ids]

    async def _cluster_events(
        self, tweets: list[RawTweet], errors: list[str]
    ) -> list:
        """xAI 事件聚类，失败降级为全部 KEEP。"""
        if self._deduplicator is None:
            from processor.event_dedup import EventCluster
            return [
                EventCluster(event_label=t.content[:50], tweets=(t,), action="KEEP")
                for t in tweets
            ]
        try:
            return await self._deduplicator.cluster(tweets)
        except Exception as exc:
            msg = f"事件聚类失败: {exc}"
            logger.warning(msg)
            errors.append(msg)
            from processor.event_dedup import EventCluster
            return [
                EventCluster(event_label=t.content[:50], tweets=(t,), action="KEEP")
                for t in tweets
            ]

    async def _select_representatives(
        self, clusters: list, errors: list[str]
    ) -> list[RawTweet]:
        """每个聚类选出代表推文（或 DigestContent 转换为 RawTweet 占位）。"""
        representatives: list[RawTweet] = []
        for cluster in clusters:
            try:
                if cluster.action == "KEEP" or len(cluster.tweets) == 1:
                    representatives.append(cluster.tweets[0])
                elif cluster.action == "BEST":
                    if self._deduplicator:
                        representatives.append(self._deduplicator.pick_best(cluster))
                    else:
                        representatives.append(cluster.tweets[0])
                elif cluster.action == "MERGE":
                    if self._deduplicator:
                        digest = await self._deduplicator.merge_to_digest(cluster)
                        # 将综述转换为 RawTweet 占位，供后续评分和入池
                        merged_tweet = self._digest_to_raw_tweet(digest, cluster)
                        representatives.append(merged_tweet)
                    else:
                        representatives.append(cluster.tweets[0])
            except Exception as exc:
                msg = f"聚类代表选取失败: {exc}"
                logger.warning(msg)
                errors.append(msg)
                if cluster.tweets:
                    representatives.append(cluster.tweets[0])
        return representatives

    def _digest_to_raw_tweet(self, digest: DigestContent, cluster) -> RawTweet:
        """将综述内容封装为 RawTweet，source_type 标记为 keyword_merge。"""
        from datetime import datetime, timezone
        best = max(cluster.tweets, key=lambda t: len(t.content))
        return RawTweet(
            external_id=f"merge_{best.external_id}",
            handle="[merged]",
            content=f"{digest.title_zh}\n\n{digest.body_zh}",
            url=best.url,
            published_at=datetime.now(tz=timezone.utc),
            source_type="keyword_merge",
            source_value=cluster.tweets[0].source_value,
            image_urls=(),
        )

    async def _score_candidates(
        self,
        tweets: list[RawTweet],
        threshold: float,
        errors: list[str],
    ) -> list[tuple[RawTweet, float, str, dict]]:
        """对每条推文打关键词专用 5 维度分，筛出高于阈值的。"""
        results: list[tuple[RawTweet, float, str, dict]] = []
        for tweet in tweets:
            try:
                scored = await self._scorer.score_keyword_tweet(
                    handle=tweet.handle,
                    content=tweet.content,
                    category=tweet.source_value,
                )
                if scored is None:
                    continue
                score, reason, detail = scored
                if score >= threshold:
                    results.append((tweet, score, reason, detail))
            except Exception as exc:
                msg = f"评分异常 @{tweet.handle}: {exc}"
                logger.warning(msg)
                errors.append(msg)
        return results

    async def _fact_check(
        self,
        scored: list[tuple[RawTweet, float, str, dict]],
        errors: list[str],
    ) -> list[tuple[RawTweet, float, str, dict, str]]:
        """对未知作者做 xAI 事实核查，结果附加 credibility_note。"""
        from processor.context_enricher import _HANDLE_DISPLAY  # noqa: PLC0415

        known_handles = set(_HANDLE_DISPLAY.keys())
        verified: list[tuple[RawTweet, float, str, dict, str]] = []

        for tweet, score, reason, detail in scored:
            credibility_note = ""
            handle_lower = tweet.handle.lower()
            is_known = handle_lower in known_handles or tweet.source_type == "keyword_merge"

            if not is_known and self._xai is not None:
                try:
                    check = await self._xai.fact_check(tweet.handle, tweet.content[:600])
                    credibility = check.get("credibility", "unknown")
                    note = check.get("note", "")
                    credibility_note = f"[{credibility}] {note}"
                    # 低可信度 + 不可验证 → 跳过
                    if (
                        check.get("verifiable") is False
                        and credibility in ("low", "unknown")
                    ):
                        logger.info(
                            "事实核查不通过，跳过 @%s: %s", tweet.handle, note
                        )
                        continue
                except Exception as exc:
                    msg = f"事实核查异常 @{tweet.handle}: {exc}"
                    logger.warning(msg)
                    errors.append(msg)

            verified.append((tweet, score, reason, detail, credibility_note))
        return verified

    async def _insert_to_pool(
        self,
        verified: list[tuple[RawTweet, float, str, dict, str]],
        errors: list[str],
    ) -> int:
        """保存推文到 DB 并入候选池，返回新增数量。"""
        added = 0
        for tweet, score, reason, detail, credibility_note in verified:
            try:
                await self._repo.save_tweets([tweet])
                await self._repo.save_score(
                    tweet.external_id,
                    score=score,
                    reason=reason,
                    detail=detail,
                )
                if credibility_note:
                    await self._repo.save_credibility_note(
                        tweet.external_id, credibility_note,
                    )
                pool_added = await self._repo.add_to_pool(
                    tweet_external_id=tweet.external_id,
                    source_label="关键词扫描",
                    source_detail=tweet.source_value or tweet.handle,
                    score=score,
                    reason=reason,
                    preview_text=tweet.content[:150],
                )
                if pool_added:
                    added += 1
            except Exception as exc:
                msg = f"入池失败 @{tweet.handle}: {exc}"
                logger.warning(msg)
                errors.append(msg)
        return added

    async def _save_log(self, result: SweepResult) -> None:
        """写入 sweep_log 表。"""
        try:
            await self._repo.save_sweep_log(
                sweep_id=result.sweep_id,
                total_fetched=result.total_fetched,
                unique_after_dedup=result.unique_after_dedup,
                events_merged=result.events_merged,
                candidates_added=result.candidates_added,
                duration_seconds=result.duration_seconds,
                errors=list(result.errors) if result.errors else None,
            )
        except Exception as exc:
            logger.warning("写入 sweep_log 失败: %s", exc)

    @staticmethod
    def _make_result(
        sweep_id: str,
        total_fetched: int,
        unique_after_dedup: int,
        events_merged: int,
        candidates_added: int,
        start: float,
        errors: list[str],
    ) -> SweepResult:
        return SweepResult(
            sweep_id=sweep_id,
            total_fetched=total_fetched,
            unique_after_dedup=unique_after_dedup,
            events_merged=events_merged,
            candidates_added=candidates_added,
            duration_seconds=round(time.monotonic() - start, 1),
            errors=tuple(errors),
        )
