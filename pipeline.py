from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from config import AppConfig
from processor.content_formatter import ContentFormatter
from processor.translator import ClaudeTranslator
from publisher.image_overlay import TweetImageOverlayer
from publisher.telegram_notifier import TelegramNotifier
from scraper.image_downloader import ImageDownloader
from scraper.models import ProcessedContent, ProcessedStatus
from scraper.rsshub_client import RSSHubClient
from scraper.twscrape_client import TwscrapeClient
from scraper.tweet_screenshotter import TweetScreenshotter
from storage.database import Database
from storage.tweet_repo import TweetRepository

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._database = Database(config.db_path)
        self._repo = TweetRepository(self._database)
        self._rsshub = RSSHubClient(config.scraper)
        self._twscrape = TwscrapeClient(config.scraper)
        self._translator = ClaudeTranslator(config.processor)
        self._formatter = ContentFormatter()
        self._overlayer = TweetImageOverlayer()
        self._screenshotter = TweetScreenshotter(
            auth_token=config.publisher.twitter_auth_token,
            output_dir=config.publisher.image_dir,
        )
        self._downloader = ImageDownloader(output_dir=config.publisher.image_dir)
        self._telegram = TelegramNotifier(config.telegram) if config.telegram else None

    async def setup(self) -> Path:
        self._config.data_dir.mkdir(parents=True, exist_ok=True)
        self._config.publisher.image_dir.mkdir(parents=True, exist_ok=True)
        await self._database.initialize()
        # Seed accounts from .env on first run if DB is empty
        db_accounts = await self._repo.list_accounts()
        if not db_accounts and self._config.scraper.accounts:
            for handle in self._config.scraper.accounts:
                await self._repo.add_account(handle)
            logger.info("从 .env 导入初始账号: %s", list(self._config.scraper.accounts))
        # Seed keywords from .env on first run if DB is empty
        db_keywords = await self._repo.list_keywords()
        if not db_keywords and self._config.scraper.keywords:
            for kw in self._config.scraper.keywords:
                await self._repo.add_keyword(kw)
            logger.info("从 .env 导入初始关键词: %s", list(self._config.scraper.keywords))
        await self._twscrape.setup()
        return self._config.db_path

    async def scrape(
        self,
        use_rsshub: bool,
        accounts: tuple[str, ...] | list[str] | None = None,
        keywords: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, int]:
        if not use_rsshub:
            raise ValueError("当前仅支持 `--rsshub` 抓取模式")
        await self.setup()
        inserted = 0
        fetched = 0
        # When neither accounts nor keywords are specified, use both from DB
        target_accounts = list(accounts) if accounts is not None else await self._repo.list_accounts()
        target_keywords = list(keywords) if keywords is not None else ([] if accounts is not None else await self._repo.list_keywords())
        for handle in target_accounts:
            try:
                tweets = await self._rsshub.fetch_user_tweets(handle)
                fetched += len(tweets)
                inserted += await self._repo.save_tweets(tweets)
                await self._repo.log_scrape("account", handle, f"/twitter/user/{handle}", len(tweets), True)
            except Exception as exc:
                logger.warning("账号抓取跳过 [%s]: %s", handle, exc)
                await self._repo.log_scrape("account", handle, f"/twitter/user/{handle}", 0, False, str(exc))
        for kw in target_keywords:
            try:
                tweets = await self._twscrape.fetch_keyword_tweets(kw)
                fetched += len(tweets)
                inserted += await self._repo.save_tweets(tweets)
                await self._repo.log_scrape("keyword", kw, f"twscrape:{kw}", len(tweets), True)
            except Exception as exc:
                logger.warning("关键词抓取跳过 [%s]: %s", kw, exc)
                await self._repo.log_scrape("keyword", kw, f"twscrape:{kw}", 0, False, str(exc))
        return {"fetched": fetched, "inserted": inserted}

    async def list_accounts(self) -> list[str]:
        await self.setup()
        return await self._repo.list_accounts()

    async def add_account(self, handle: str) -> bool:
        await self.setup()
        return await self._repo.add_account(handle)

    async def remove_account(self, handle: str) -> bool:
        await self.setup()
        return await self._repo.remove_account(handle)

    async def list_keywords(self) -> list[str]:
        await self.setup()
        return await self._repo.list_keywords()

    async def add_keyword(self, keyword: str) -> bool:
        await self.setup()
        return await self._repo.add_keyword(keyword)

    async def remove_keyword(self, keyword: str) -> bool:
        await self.setup()
        return await self._repo.remove_keyword(keyword)

    async def process(self, limit: int | None = None) -> list[ProcessedContent]:
        await self.setup()
        tweets = await self._repo.list_unprocessed_tweets(limit or self._config.scraper.max_tweets)
        processed_items: list[ProcessedContent] = []
        for tweet in tweets:
            content = await self._translator.translate(tweet)
            formatted = self._formatter.format(content)
            await self._repo.save_processed_content(formatted)
            processed_items.append(formatted)
        return processed_items

    async def deliver(
        self,
        accounts: list[str] | None = None,
        limit: int | None = None,
        scrape_first: bool = False,
    ) -> dict[str, int]:
        """抓取（可选）→ 翻译 → 截图/覆盖图 → Telegram 发送 → 标记 sent。"""
        await self.setup()

        fetched = 0
        inserted = 0
        if scrape_first:
            result = await self.scrape(use_rsshub=True, accounts=accounts)
            fetched = result["fetched"]
            inserted = result["inserted"]

        # 翻译所有未处理推文
        await self.process(limit=limit)

        if self._telegram is None:
            raise ValueError("未配置 Telegram，请检查 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")

        effective_limit = limit or self._config.scraper.max_tweets
        # Only filter by handle when accounts are explicitly specified.
        # Keyword-sourced tweets come from various handles and must not be filtered out.
        pairs = await self._repo.list_content_with_tweets(
            [ProcessedStatus.NEW],
            effective_limit,
            handles=accounts,  # None → return all NEW regardless of handle
        )

        sent = 0
        for content, tweet in pairs:
            try:
                payload = await self._build_payload(content, tweet)
                await self._telegram.notify_content(payload)
                await self._repo.mark_sent(content.tweet_external_id)
                self._cleanup_images(payload.get("images", []))
                sent += 1
            except Exception as exc:
                logger.error("发送失败 [%s]: %s", content.tweet_external_id, exc)

        return {"fetched": fetched, "inserted": inserted, "sent": sent}

    async def drain(self) -> int:
        """将所有 new 状态标记为 sent，清空队列。"""
        await self.setup()
        pairs = await self._repo.list_content_with_tweets(
            [ProcessedStatus.NEW],
            limit=10000,
        )
        for content, _ in pairs:
            await self._repo.mark_sent(content.tweet_external_id)
        return len(pairs)

    async def status(self) -> dict[str, int]:
        await self.setup()
        return await self._repo.status_counts()

    async def _build_payload(self, content: ProcessedContent, tweet) -> dict:
        file_key = content.tweet_external_id.replace("/", "_").replace(":", "_")[-40:]

        # 截图
        screenshot_path = await self._screenshotter.screenshot(tweet.url, file_key)

        # 下载原始图片
        downloaded_paths = ()
        if tweet.image_urls:
            downloaded_paths = await self._downloader.download_many(tweet.image_urls, file_key)

        # 图片覆盖（截图存在时，生成中文覆盖图）
        overlay_path: Path | None = None
        if screenshot_path and screenshot_path.exists():
            try:
                translations = await self._translator.translate_literal_parts(tweet)
                overlay_path = self._overlayer.append_translations(screenshot_path, translations)
            except Exception as exc:
                logger.warning("翻译卡片生成失败，使用原始截图: %s", exc)

        images: list[str] = []
        if overlay_path and overlay_path.exists():
            images.append(str(overlay_path))
        elif screenshot_path and screenshot_path.exists():
            images.append(str(screenshot_path))
        for p in downloaded_paths:
            if p.exists():
                images.append(str(p))

        return {
            "title": content.title_zh,
            "body": content.body_zh,
            "tags": list(content.tags),
            "images": images,
        }

    def _cleanup_images(self, images: list[str]) -> None:
        for image_str in images:
            path = Path(image_str)
            try:
                if path.exists():
                    path.unlink()
            except Exception as exc:
                logger.warning("图片删除失败: %s — %s", path, exc)
