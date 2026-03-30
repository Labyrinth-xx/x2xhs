from __future__ import annotations

import json
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

    async def search_fun_finds(self, n: int = 3) -> list[RawTweet]:
        """开放式搜索：找过去 7 天内最有趣、好笑、有共鸣的推文。不限主题。

        返回 RawTweet 列表，source_type="xai_fun"，source_value 存有趣点说明。
        """
        prompt = (
            f"Search X (Twitter) for the {n} most funny, surprising, or delightfully unexpected English-language tweets "
            f"from the past 7 days. Focus on tweets that would make Chinese tech/internet users laugh or feel a sense of "
            f"shared recognition.\n\n"
            f"Preferred topics (not limited to):\n"
            f"- AI product easter eggs or surprising behaviors\n"
            f"- Unexpected or funny AI experiments\n"
            f"- Ironic tech culture observations\n"
            f"- Anything that makes you go 'wow, I didn't know that' or 'haha, so true'\n\n"
            f"Requirements:\n"
            f"- Must be real tweets with verifiable tweet URLs (https://x.com/handle/status/<tweet_id>)\n"
            f"- English-language tweets preferred\n"
            f"- Avoid political or controversial content\n\n"
            f"For each tweet, output in this exact format (blank line between entries):\n"
            f"作者：@handle\n"
            f"内容：（推文完整原文）\n"
            f"链接：https://x.com/handle/status/<tweet_id>\n"
            f"有趣点：（一句话中文说明为什么有趣）\n"
        )

        response = await self._client.chat.completions.create(
            model=self._config.model,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"plugins": [{"id": "web"}]},
        )

        text = response.choices[0].message.content or ""
        return self._parse_fun_tweets(text)

    # ── 关键词扫描支持 ──

    async def cluster_events(self, tweets_block: str, count: int) -> list[dict]:
        """将多条推文摘要按底层事件分组，返回 MERGE/BEST/KEEP 决策列表。

        prompt 内容由 Opus 实现（build_event_cluster_prompt）。
        失败时返回空列表，由调用方降级为全部 KEEP。
        """
        from processor.prompts import build_event_cluster_prompt

        prompt = build_event_cluster_prompt(tweets_block, count)
        try:
            response = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
            )
            text = (response.choices[0].message.content or "").strip()
            text = _CITATION_RE.sub("", text).strip()
            return self._parse_json_array(text)
        except Exception as exc:
            logger.warning("事件聚类失败: %s", exc)
            return []

    async def fact_check(self, handle: str, content: str) -> dict:
        """核查推文声明可信度，返回 {verifiable, credibility, note}。

        prompt 内容由 Opus 实现（build_fact_check_prompt）。
        失败时返回保守默认值（不阻断流程）。
        """
        from processor.prompts import build_fact_check_prompt

        prompt = build_fact_check_prompt(handle, content)
        try:
            response = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                extra_body={"plugins": [{"id": "web"}]},
            )
            text = (response.choices[0].message.content or "").strip()
            text = _CITATION_RE.sub("", text).strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return dict(json.loads(match.group()))
        except Exception as exc:
            logger.warning("事实核查失败 @%s: %s", handle, exc)
        return {"verifiable": None, "credibility": "unknown", "note": "核查失败"}

    async def suggest_keyword_updates(self, queries_stats: str) -> list[dict]:
        """分析当前查询命中率 + 行业热点，建议关键词 add/retire/modify。

        prompt 内容由 Opus 实现（build_keyword_refresh_prompt）。
        失败时返回空列表。
        """
        from processor.prompts import build_keyword_refresh_prompt

        prompt = build_keyword_refresh_prompt(queries_stats)
        try:
            response = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                extra_body={"plugins": [{"id": "web"}]},
            )
            text = (response.choices[0].message.content or "").strip()
            text = _CITATION_RE.sub("", text).strip()
            return self._parse_json_array(text)
        except Exception as exc:
            logger.warning("关键词刷新建议失败: %s", exc)
            return []

    async def merge_cluster_digest(
        self, event_label: str, tweets_text: str, citation_urls: tuple[str, ...]
    ) -> DigestContent:
        """将多条同事件推文综合为一篇中文综述。

        由 EventDeduplicator.merge_to_digest() 调用。
        """
        from processor.prompts import build_merge_digest_prompt

        prompt = build_merge_digest_prompt(event_label, tweets_text)
        response = await self._client.chat.completions.create(
            model=self._config.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            extra_body={"plugins": [{"id": "web"}]},
        )
        text = (response.choices[0].message.content or "").strip()
        text = _CITATION_RE.sub("", text).strip()
        return self._parse_digest(event_label, text, citation_urls)

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
        body_match = re.search(r"正文[：:]\s*([\s\S]+?)(?=\n标签[：:]|$)", clean)
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

    def _parse_fun_tweets(self, text: str) -> list[RawTweet]:
        """从模型输出文本解析出趣文推文列表。source_value 存「有趣点」说明。"""
        tweets: list[RawTweet] = []
        blocks = re.split(r"\n\s*\n", text.strip())

        for block in blocks:
            handle_m = re.search(r"作者[：:]\s*@?(\w+)", block)
            content_m = re.search(r"内容[：:]\s*(.+?)(?=链接[：:]|有趣点[：:]|$)", block, re.DOTALL)
            url_m = re.search(r"链接[：:]\s*(https://x\.com/\S+)", block)
            fun_m = re.search(r"有趣点[：:]\s*(.+)", block)

            if not (handle_m and content_m):
                continue

            handle = handle_m.group(1).strip()
            content = content_m.group(1).strip()
            url = url_m.group(1).strip() if url_m else f"https://x.com/{handle}"
            fun_point = fun_m.group(1).strip() if fun_m else ""

            id_m = re.search(r"/status/(\d+)", url)
            if not id_m:
                logger.warning("趣文缺少具体 tweet URL，跳过: handle=%s url=%s", handle, url)
                continue
            external_id = id_m.group(1)

            tweets.append(RawTweet(
                external_id=external_id,
                handle=handle,
                content=content,
                url=url,
                published_at=datetime.now(tz=timezone.utc),
                source_type="xai_fun",
                source_value=fun_point,
            ))

        if not tweets:
            logger.warning("xAI fun_finds 解析失败，文本长度=%d", len(text))

        return tweets

    def _parse_json_array(self, text: str) -> list[dict]:
        """从文本中提取 JSON 数组，解析失败返回空列表。"""
        try:
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                return list(json.loads(match.group()))
        except Exception as exc:
            logger.debug("JSON 数组解析失败: %s | text[:200]=%r", exc, text[:200])
        return []
