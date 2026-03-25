from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any

import feedparser
import httpx

from config import ScraperConfig
from scraper.models import RawTweet

logger = logging.getLogger(__name__)

IMG_PATTERN = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[^>]+>")
HANDLE_PATTERN = re.compile(r"/([^/]+)/status/")


class RSSHubClient:
    def __init__(self, config: ScraperConfig) -> None:
        self._config = config

    async def fetch_user_tweets(self, handle: str) -> list[RawTweet]:
        path = f"/twitter/user/{handle}"
        text = await self._request_feed(path)
        return self._parse_feed(text, "account", handle)

    async def _request_feed(self, path: str) -> str:
        url = f"{self._config.base_url}{path}"
        for attempt in range(self._config.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    return response.text
            except httpx.HTTPError as exc:
                logger.warning("RSSHub 请求失败: %s (%s)", url, exc)
                if attempt == self._config.max_retries - 1:
                    raise
                await asyncio.sleep(2**attempt)
        raise RuntimeError(f"RSSHub 请求超过重试上限: {url}")

    def _parse_feed(self, text: str, source_type: str, source_value: str) -> list[RawTweet]:
        feed = feedparser.parse(text)
        tweets: list[RawTweet] = []
        for entry in feed.entries:
            if len(tweets) >= self._config.max_tweets:
                break
            if self._is_retweet(entry) or self._is_reply(entry):
                continue
            tweets.append(self._entry_to_tweet(entry, source_type, source_value))
        return tweets

    def _is_retweet(self, entry: dict[str, Any]) -> bool:
        title = getattr(entry, "title", "") or ""
        summary = getattr(entry, "summary", "") or ""
        text = (title + " " + summary).strip()
        return text.startswith("RT @")

    def _is_reply(self, entry: dict[str, Any]) -> bool:
        title = getattr(entry, "title", "") or ""
        summary = getattr(entry, "summary", "") or ""
        text = (title + " " + summary).strip()
        return text.startswith("@")

    def _entry_to_tweet(
        self,
        entry: dict[str, Any],
        source_type: str,
        source_value: str,
    ) -> RawTweet:
        summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
        url = getattr(entry, "link", "")
        external_id = getattr(entry, "id", url)
        handle = self._extract_handle(entry, url, source_value, source_type)
        return RawTweet(
            external_id=external_id,
            handle=handle,
            content=self._clean_html(summary or getattr(entry, "title", "")),
            url=url,
            published_at=self._parse_published_at(entry),
            source_type=source_type,
            source_value=source_value,
            image_urls=self._extract_images(entry, summary),
        )

    def _extract_handle(
        self,
        entry: dict[str, Any],
        url: str,
        source_value: str,
        source_type: str,
    ) -> str:
        if source_type == "account":
            return source_value.lower()
        author = getattr(entry, "author", "").strip()
        if author:
            return author.replace("@", "").lower()
        match = HANDLE_PATTERN.search(url)
        return (match.group(1) if match else source_value).lower()

    def _extract_images(self, entry: dict[str, Any], summary: str) -> tuple[str, ...]:
        urls: list[str] = []
        for item in getattr(entry, "media_content", []) or []:
            value = item.get("url", "").strip()
            if value:
                urls.append(value)
        urls.extend(IMG_PATTERN.findall(summary))
        unique_urls = tuple(dict.fromkeys(urls))
        return unique_urls

    def _parse_published_at(self, entry: dict[str, Any]) -> datetime:
        time_struct = getattr(entry, "published_parsed", None)
        if time_struct is None:
            return datetime.now(tz=timezone.utc)
        return datetime(*time_struct[:6], tzinfo=timezone.utc)

    def _clean_html(self, value: str) -> str:
        text = TAG_PATTERN.sub("", value)
        return unescape(text).strip()
