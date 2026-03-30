"""关键词自适应刷新 — 每周分析命中率 + 行业热点，推荐 add/retire/modify 建议。

流程：
  1. 从 DB 加载当前启用的查询 + 过去 7 天命中统计
  2. 调用 xAI build_keyword_refresh_prompt → 返回建议列表
  3. 返回 list[KeywordSuggestion]，由 bot.py 推送 Telegram，用户确认后调用
     Pipeline.apply_keyword_suggestion() 执行
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from processor.prompts import build_keyword_refresh_prompt

if TYPE_CHECKING:
    from scraper.xai_client import XAIClient
    from storage.tweet_repo import TweetRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KeywordSuggestion:
    """xAI 建议的关键词操作。"""

    action: str         # add | retire | modify
    category: str       # model_release | ai_app | infra | industry | regulation
    query: str          # 建议的查询字符串（modify/add 时为新查询；retire 时为现有查询）
    reason: str         # 建议理由
    confidence: float   # 0-1，建议置信度


class KeywordRefresher:
    """每周关键词自适应刷新器。

    由 Pipeline.keyword_refresh() 调用，返回建议列表，不直接修改 DB。
    """

    def __init__(self, xai: "XAIClient | None", repo: "TweetRepository") -> None:
        self._xai = xai
        self._repo = repo

    async def suggest_updates(self) -> list[KeywordSuggestion]:
        """分析当前查询命中率，返回 add/retire/modify 建议。

        步骤：
          1. 加载所有查询（含已停用的）
          2. 加载最近 7 天 sweep 统计
          3. 拼接 queries_stats 字符串 → xAI 分析
          4. 解析返回的建议列表
        """
        if self._xai is None:
            logger.info("xAI 未配置，跳过关键词刷新")
            return []

        try:
            queries = await self._repo.list_keyword_queries(enabled_only=False)
            sweep_stats = await self._repo.list_recent_sweep_stats(days=7)
            queries_stats = _build_queries_stats_text(queries, sweep_stats)

            raw_suggestions = await self._xai.suggest_keyword_updates(queries_stats)
            if not raw_suggestions:
                logger.info("xAI 关键词刷新：无建议返回")
                return []

            suggestions = [
                KeywordSuggestion(
                    action=str(s.get("action", "add")).lower(),
                    category=str(s.get("category", "other")),
                    query=str(s.get("query", "")),
                    reason=str(s.get("reason", "")),
                    confidence=float(s.get("confidence", 0.5)),
                )
                for s in raw_suggestions
                if s.get("action") and s.get("query")
            ]
            logger.info("关键词刷新建议 %d 条", len(suggestions))
            return suggestions

        except Exception as exc:
            logger.warning("关键词刷新失败: %s", exc)
            return []


def _build_queries_stats_text(queries: list[dict], sweep_stats: list[dict]) -> str:
    """将查询列表和统计数据格式化为 xAI prompt 的输入文本。"""
    lines: list[str] = []

    lines.append("=== 当前关键词查询 ===")
    if queries:
        for q in queries:
            status = "enabled" if q.get("enabled") else "retired"
            hits = q.get("hit_count", 0)
            last_hit = q.get("last_hit_at", "never")
            lines.append(
                f"[{status}] [{q.get('category', '?')}] "
                f"{q.get('query_template', '?')} "
                f"(min_faves={q.get('min_faves', '?')}, hits={hits}, last_hit={last_hit})"
            )
    else:
        lines.append("（无查询记录，使用默认值）")

    lines.append("")
    lines.append("=== 最近 7 天扫描统计 ===")
    if sweep_stats:
        total_fetched = sum(s.get("total_fetched", 0) for s in sweep_stats)
        total_added = sum(s.get("candidates_added", 0) for s in sweep_stats)
        lines.append(
            f"共运行 {len(sweep_stats)} 次扫描，"
            f"累计抓取 {total_fetched} 条，"
            f"新增候选 {total_added} 条"
        )
        # 最近一次扫描详情
        latest = sweep_stats[-1]
        lines.append(
            f"最近一次：{latest.get('created_at', '?')} — "
            f"fetched={latest.get('total_fetched', 0)}, "
            f"added={latest.get('candidates_added', 0)}"
        )
    else:
        lines.append("（无扫描记录）")

    return "\n".join(lines)
