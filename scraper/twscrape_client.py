from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from playwright.async_api import async_playwright

from config import ScraperConfig
from scraper.models import RawTweet

logger = logging.getLogger(__name__)

_COOKIE_DOMAINS = [".x.com", ".twitter.com"]
_SEARCH_TIMELINE_PATH = "/i/api/graphql/"


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k, default)
    return obj


class TwscrapeClient:
    """用 Playwright 拦截 Twitter 搜索页的 SearchTimeline GraphQL 响应。

    浏览器自动生成 x-client-transaction-id，绕过 Python 直接 HTTP 请求的封锁。
    """

    def __init__(self, config: ScraperConfig) -> None:
        self._config = config

    async def setup(self) -> None:
        pass  # cookie 无需初始化

    def _is_configured(self) -> bool:
        return bool(self._config.twitter_auth_token and self._config.twitter_ct0)

    async def fetch_keyword_tweets(self, keyword: str) -> list[RawTweet]:
        if not self._is_configured():
            logger.warning("未配置 TWSCRAPE_AUTH_TOKEN/CT0，跳过关键词 [%s]", keyword)
            return []

        query = (
            f"{keyword} "
            f"min_faves:{self._config.min_faves} "
            f"min_retweets:{self._config.min_retweets} "
            f"-filter:retweets"
        )
        search_url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"

        captured: list[dict] = []

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                # Set auth cookies
                cookies = []
                for domain in _COOKIE_DOMAINS:
                    cookies.extend([
                        {
                            "name": "auth_token",
                            "value": self._config.twitter_auth_token,
                            "domain": domain,
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                        },
                        {
                            "name": "ct0",
                            "value": self._config.twitter_ct0,
                            "domain": domain,
                            "path": "/",
                            "secure": True,
                            "httpOnly": False,
                        },
                    ])
                await context.add_cookies(cookies)

                page = await context.new_page()

                async def handle_response(response):
                    if "SearchTimeline" in response.url and response.status == 200:
                        try:
                            data = await response.json()
                            captured.append(data)
                        except Exception as exc:
                            logger.debug("SearchTimeline 响应解析失败: %s", exc)

                page.on("response", handle_response)

                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                # Wait for tweets to load
                try:
                    await page.wait_for_selector(
                        '[data-testid="tweet"]', timeout=15000
                    )
                except Exception:
                    logger.debug("等待推文元素超时，继续解析已捕获的 API 响应")
                # Extra wait to capture lazy-loaded results
                await page.wait_for_timeout(2000)
                await browser.close()

        except Exception as exc:
            logger.warning("Playwright 搜索失败 [%s]: %s", keyword, exc)
            return []

        if not captured:
            logger.warning("关键词 [%s] 未捕获到 SearchTimeline 响应", keyword)
            return []

        # Use the last captured response (may contain more results after scroll)
        data = captured[-1]
        tweets = self._parse_response(data, keyword)
        logger.info("关键词 [%s] 抓取到 %d 条推文", keyword, len(tweets))
        return tweets

    def _parse_response(self, data: dict, keyword: str) -> list[RawTweet]:
        instructions = _get(
            data,
            "data", "search_by_raw_query", "search_timeline",
            "timeline", "instructions",
            default=[],
        )
        tweets: list[RawTweet] = []
        for instruction in instructions:
            if instruction.get("type") != "TimelineAddEntries":
                continue
            for entry in instruction.get("entries", []):
                if not entry.get("entryId", "").startswith("tweet-"):
                    continue
                tweet = self._parse_entry(entry, keyword)
                if tweet:
                    tweets.append(tweet)
        return tweets

    def _parse_entry(self, entry: dict, keyword: str) -> RawTweet | None:
        result = _get(
            entry, "content", "itemContent", "tweet_results", "result",
            default={},
        )
        # TweetWithVisibilityResults 包装层
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet", {})
        if result.get("__typename") != "Tweet":
            return None

        tweet_id = result.get("rest_id", "")
        if not tweet_id:
            return None

        legacy = result.get("legacy", {})
        user_result = _get(result, "core", "user_results", "result", default={})
        # screen_name is in user_result.core (new API) or user_result.legacy (old API)
        handle = (
            _get(user_result, "core", "screen_name")
            or _get(user_result, "legacy", "screen_name")
            or "unknown"
        )
        # 长推文（note tweet）full_text 会截断，完整内容在 note_tweet 字段
        note_text = _get(result, "note_tweet", "note_tweet_results", "result", "text")
        text = note_text or legacy.get("full_text", "")

        # 引用推文：拼接到正文末尾，便于翻译器识别
        quoted_result = _get(result, "quoted_status_result", "result", default={})
        if quoted_result.get("__typename") == "TweetWithVisibilityResults":
            quoted_result = quoted_result.get("tweet", {})
        if quoted_result.get("__typename") == "Tweet":
            q_legacy = quoted_result.get("legacy", {})
            q_user = _get(quoted_result, "core", "user_results", "result", default={})
            q_handle = (
                _get(q_user, "core", "screen_name")
                or _get(q_user, "legacy", "screen_name")
                or "unknown"
            )
            q_note = _get(quoted_result, "note_tweet", "note_tweet_results", "result", "text")
            q_text = q_note or q_legacy.get("full_text", "")
            if q_text:
                text = f"{text}\n\n[Quoted @{q_handle}:]\n{q_text}"

        try:
            created_at = parsedate_to_datetime(legacy.get("created_at", ""))
        except Exception:
            created_at = datetime.now(tz=timezone.utc)

        # 优先用 extended_entities 获取图片（包含多图）
        media = _get(legacy, "extended_entities", "media", default=[]) or \
                _get(legacy, "entities", "media", default=[]) or []
        image_urls = tuple(
            m["media_url_https"]
            for m in media
            if m.get("type") == "photo" and "media_url_https" in m
        )

        return RawTweet(
            external_id=tweet_id,
            handle=handle,
            content=text,
            url=f"https://x.com/{handle}/status/{tweet_id}",
            published_at=created_at,
            source_type="keyword",
            source_value=keyword,
            image_urls=image_urls,
        )
