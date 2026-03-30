"""事件聚类与去重 — 将关键词扫描结果按底层事件分组，避免同一事件多条入池。

EventDeduplicator.cluster() 批量发给 xAI 分组。
EventDeduplicator.merge_to_digest() 把 MERGE 类聚合为综合报道。

prompt 内容由 Opus 实现（build_event_cluster_prompt / merge_cluster_prompt）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from scraper.models import DigestContent, RawTweet

if TYPE_CHECKING:
    from scraper.xai_client import XAIClient

logger = logging.getLogger(__name__)

# 每批最多送给 xAI 聚类的推文数量（控制 prompt 长度）
_CLUSTER_BATCH_SIZE = 30


@dataclass(frozen=True, slots=True)
class EventCluster:
    """一组讨论同一底层事件的推文。"""

    event_label: str                # 事件一句话描述
    tweets: tuple[RawTweet, ...]    # 该事件的所有候选推文
    action: str                     # MERGE | BEST | KEEP


def _build_tweets_block(tweets: list[RawTweet]) -> str:
    """将推文列表格式化为聚类 prompt 的输入块。"""
    lines: list[str] = []
    for t in tweets:
        preview = t.content[:200].replace("\n", " ")
        lines.append(f"[{t.external_id}] @{t.handle}: {preview}")
    return "\n".join(lines)


class EventDeduplicator:
    """对关键词扫描结果做事件级聚类，决定每组的处理策略。"""

    def __init__(self, xai: "XAIClient") -> None:
        self._xai = xai

    async def cluster(self, tweets: list[RawTweet]) -> list[EventCluster]:
        """将推文列表按底层事件分组，返回 EventCluster 列表。

        少于 3 条时跳过 xAI 调用，全部视为独立 KEEP。
        xAI 调用失败时降级为全部 KEEP（不阻断主流程）。
        """
        if len(tweets) < 3:
            return [
                EventCluster(
                    event_label=t.content[:50],
                    tweets=(t,),
                    action="KEEP",
                )
                for t in tweets
            ]

        batch = tweets[:_CLUSTER_BATCH_SIZE]
        tweets_block = _build_tweets_block(batch)
        raw_clusters = await self._xai.cluster_events(tweets_block, len(batch))
        if len(tweets) > _CLUSTER_BATCH_SIZE:
            logger.info(
                "推文数 %d 超过批次上限 %d，超出部分将直接标记为 KEEP",
                len(tweets), _CLUSTER_BATCH_SIZE,
            )

        if not raw_clusters:
            logger.warning("事件聚类返回空，降级为全部 KEEP（共 %d 条）", len(tweets))
            return [
                EventCluster(event_label=t.content[:50], tweets=(t,), action="KEEP")
                for t in tweets
            ]

        # 将 xAI 返回的 cluster 列表映射回 RawTweet 对象
        id_to_tweet = {t.external_id: t for t in tweets}
        clusters: list[EventCluster] = []
        assigned_ids: set[str] = set()

        for item in raw_clusters:
            event = str(item.get("event", ""))
            action = str(item.get("action", "KEEP")).upper()
            ids = [str(i) for i in item.get("tweet_ids", [])]

            cluster_tweets = tuple(
                id_to_tweet[i] for i in ids if i in id_to_tweet
            )
            if not cluster_tweets:
                continue

            # 校正 action：单条推文不应为 MERGE
            if action == "MERGE" and len(cluster_tweets) < 2:
                action = "KEEP"
            if action == "BEST" and len(cluster_tweets) < 2:
                action = "KEEP"

            clusters.append(EventCluster(
                event_label=event or cluster_tweets[0].content[:50],
                tweets=cluster_tweets,
                action=action,
            ))
            assigned_ids.update(ids)

        # 没被 xAI 分配的推文单独作为 KEEP
        for t in tweets:
            if t.external_id not in assigned_ids:
                clusters.append(EventCluster(
                    event_label=t.content[:50],
                    tweets=(t,),
                    action="KEEP",
                ))

        logger.info(
            "事件聚类完成：%d 条推文 → %d 个事件（MERGE=%d, BEST=%d, KEEP=%d）",
            len(tweets),
            len(clusters),
            sum(1 for c in clusters if c.action == "MERGE"),
            sum(1 for c in clusters if c.action == "BEST"),
            sum(1 for c in clusters if c.action == "KEEP"),
        )
        return clusters

    async def merge_to_digest(self, cluster: EventCluster) -> DigestContent:
        """将 MERGE 聚类的多条推文合并为一篇综合报道。

        调用 xAI 综合多条推文信息生成中文综述。
        失败时使用兜底：拼接原文内容。
        """
        tweets_text = "\n\n".join(
            f"@{t.handle}: {t.content[:400]}" for t in cluster.tweets[:5]
        )
        citation_urls = tuple(t.url for t in cluster.tweets[:3])

        try:
            result = await self._xai.merge_cluster_digest(
                event_label=cluster.event_label,
                tweets_text=tweets_text,
                citation_urls=citation_urls,
            )
            logger.info(
                "MERGE 成功：事件='%s'，合并 %d 条推文 → '%s'",
                cluster.event_label,
                len(cluster.tweets),
                result.title_zh[:30],
            )
            return result
        except Exception as exc:
            logger.warning(
                "MERGE xAI 调用失败，使用兜底: %s (事件='%s'，%d 条推文)",
                exc, cluster.event_label, len(cluster.tweets),
            )
            best = max(cluster.tweets, key=lambda t: len(t.content))
            return DigestContent(
                keyword=cluster.event_label,
                title_zh=cluster.event_label[:15] or best.content[:15],
                body_zh=tweets_text[:800],
                tags=("AI", "科技", "资讯"),
                citation_urls=citation_urls,
            )

    def pick_best(self, cluster: EventCluster) -> RawTweet:
        """从 BEST 聚类中选内容最长（信息量最多）的推文。"""
        return max(cluster.tweets, key=lambda t: len(t.content))
