from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from openai import AsyncOpenAI

from config import XAIConfig
from scraper.models import DigestContent, RawTweet

logger = logging.getLogger(__name__)

_OR_BASE_URL = "https://openrouter.ai/api/v1"
_CITATION_RE = re.compile(r"\[\[?\d+\]?\]\([^)]+\)")
_TAG_RE = re.compile(r"[#＃]")


class XAIClient:
    """通过 OpenRouter 调用 xAI Grok 实现 X 搜索 + 网络新闻综合查询。

    功能一（降级）：search_viral_fallback — 在 twscrape 失效时使用，
                    调 x_search 找最有影响力的推文。
    功能二：search_digest — 同时搜 X + 网络新闻，直接生成中文综述文章。
    """

    def __init__(self, config: XAIConfig, openrouter_api_key: str) -> None:
        self._config = config
        self._client = AsyncOpenAI(
            api_key=openrouter_api_key,
            base_url=_OR_BASE_URL,
        )

    async def search_digest(self, keyword: str) -> DigestContent:
        """搜索 X + 网络新闻，生成关键词领域的中文综述文章。"""
        prompt = (
            f"请同时搜索 X（Twitter）和网络，找出关于「{keyword}」最近 7 天的重要讨论和新闻动态。\n\n"
            f"用中文写一篇 500-800 字的小红书风格文章，格式如下：\n"
            f"标题：（15字内，有吸引力，不加引号）\n"
            f"正文：（综合提炼核心观点、争议焦点、最新进展，不逐条翻译，换行分段）\n"
            f"标签：（5个话题标签，用空格分隔，不含 # 号）\n\n"
            f"严格按照「标题：」「正文：」「标签：」三个标签分隔，方便解析。"
        )

        response = await self._client.chat.completions.create(
            model=self._config.model,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"plugins": [{"id": "web"}]},
        )

        text = response.choices[0].message.content or ""
        citation_urls = self._extract_citations(response)
        return self._parse_digest(keyword, text, citation_urls)

    async def search_viral_fallback(self, keyword: str, n: int = 5) -> list[RawTweet]:
        """twscrape 失效时的降级方案：用 xAI x_search 找最有影响力的推文。

        返回 RawTweet 列表，source_type="xai_viral"。
        """
        prompt = (
            f"请搜索 X（Twitter），找出过去 7 天内关于「{keyword}」最有影响力、"
            f"转发量或讨论度最高的 {n} 条推文。\n\n"
            f"对每条推文，按以下格式输出（每条之间空一行）：\n"
            f"作者：@handle\n"
            f"内容：（推文完整原文）\n"
            f"链接：https://x.com/handle/status/id\n"
        )

        response = await self._client.chat.completions.create(
            model=self._config.model,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"plugins": [{"id": "web"}]},
        )

        text = response.choices[0].message.content or ""
        return self._parse_viral_tweets(text, keyword)

    # ── 解析工具 ──

    def _extract_citations(self, response) -> tuple[str, ...]:
        """从 response.citations 或 inline citation markers 提取 URL。"""
        # xAI API 把 citations 放在 response.citations（非标准 openai 字段）
        citations = getattr(response, "citations", None) or []
        if citations:
            return tuple(str(c) for c in citations if str(c).startswith("http"))

        # fallback：从正文中提取 [[n]](url) 格式的引用
        text = response.choices[0].message.content or ""
        urls = re.findall(r"\]\((\bhttps?://[^\s)]+)\)", text)
        return tuple(urls)

    def _parse_digest(
        self, keyword: str, text: str, citation_urls: tuple[str, ...]
    ) -> DigestContent:
        """从模型输出文本解析出 title/body/tags。"""
        # 去掉 inline citation markers（[[1]](url) 格式）
        clean = _CITATION_RE.sub("", text).strip()

        title = ""
        body = ""
        tags: list[str] = []

        title_match = re.search(r"标题[：:]\s*(.+)", clean)
        body_match = re.search(r"正文[：:]\s*([\s\S]+?)(?=标签[：:]|$)", clean)
        tags_match = re.search(r"标签[：:]\s*(.+)", clean)

        if title_match:
            title = title_match.group(1).strip()
        if body_match:
            body = body_match.group(1).strip()
        if tags_match:
            raw_tags = tags_match.group(1).strip()
            tags = [_TAG_RE.sub("", t).strip() for t in raw_tags.split() if t.strip()]

        # 解析失败时的兜底：把整段文字当正文，截取前 15 字做标题
        if not title or not body:
            logger.warning("xAI digest 解析失败，使用兜底处理")
            body = clean
            title = clean[:15].replace("\n", " ")

        if not tags:
            tags = [keyword, "AI", "科技", "新闻", "资讯"]

        return DigestContent(
            keyword=keyword,
            title_zh=title[:30],
            body_zh=body[:1200],
            tags=tuple(tags[:5]),
            citation_urls=citation_urls,
        )

    def _parse_viral_tweets(self, text: str, keyword: str) -> list[RawTweet]:
        """从模型输出文本解析出推文列表。"""
        tweets: list[RawTweet] = []
        blocks = re.split(r"\n\s*\n", text.strip())

        for block in blocks:
            handle_m = re.search(r"作者[：:]\s*@?(\w+)", block)
            content_m = re.search(r"内容[：:]\s*(.+?)(?=链接[：:]|$)", block, re.DOTALL)
            url_m = re.search(r"链接[：:]\s*(https://x\.com/\S+)", block)

            if not (handle_m and content_m):
                continue

            handle = handle_m.group(1).strip()
            content = content_m.group(1).strip()
            url = url_m.group(1).strip() if url_m else f"https://x.com/{handle}"

            # 从 URL 提取 tweet_id，否则用时间戳做伪 ID
            id_m = re.search(r"/status/(\d+)", url)
            external_id = id_m.group(1) if id_m else f"xai_{keyword}_{len(tweets)}"

            tweets.append(RawTweet(
                external_id=external_id,
                handle=handle,
                content=content,
                url=url,
                published_at=datetime.now(tz=timezone.utc),
                source_type="xai_viral",
                source_value=keyword,
            ))

        if not tweets:
            logger.warning("xAI viral_fallback 解析失败，文本长度=%d", len(text))

        return tweets
