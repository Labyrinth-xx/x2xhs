from __future__ import annotations

import logging
import re
from pathlib import Path

from config import AppConfig
from processor.content_formatter import ContentFormatter
from processor.translator import ClaudeTranslator
from publisher.image_overlay import TweetImageOverlayer
from publisher.telegram_notifier import TelegramNotifier
from scraper.image_downloader import ImageDownloader
from scraper.models import ProcessedContent, RawTweet
from scraper.rsshub_client import RSSHubClient
from scraper.twscrape_client import TwscrapeClient
from scraper.tweet_screenshotter import TweetScreenshotter
from storage.database import Database
from storage.tweet_repo import TweetRepository

logger = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


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

    _MIN_TRANSLATABLE_CHARS = 30

    async def deliver(
        self,
        accounts: list[str] | None = None,
        limit: int | None = None,
        scrape_first: bool = False,
        force: bool = False,
    ) -> dict[str, int | list[str]]:
        """抓取（可选）→ 翻译 → 截图/覆盖图 → Telegram 发送 → 标记 sent。"""
        await self.setup()

        try:
            purged = await self._repo.purge_expired_sent(ttl_days=7)
            if purged:
                logger.info("已清理过期 sent 记录 %d 条", purged)
        except Exception as exc:
            logger.warning("清理过期 sent 记录失败: %s", exc)

        fetched = 0
        inserted = 0
        if scrape_first:
            result = await self.scrape(use_rsshub=True, accounts=accounts)
            fetched = result["fetched"]
            inserted = result["inserted"]

        if self._telegram is None:
            raise ValueError("未配置 Telegram，请检查 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")

        effective_limit = limit or self._config.scraper.max_tweets
        candidates = await self._repo.list_candidate_tweets(
            effective_limit,
            handles=accounts,
            force=force,
        )

        sent = 0
        errors: list[str] = []
        for tweet in candidates:
            text_only = _URL_RE.sub("", tweet.content).strip()
            if len(text_only) < self._MIN_TRANSLATABLE_CHARS:
                await self._repo.save_filtered(
                    tweet_external_id=tweet.external_id,
                    handle=tweet.handle,
                    raw_url=tweet.url,
                    published_at=tweet.published_at,
                )
                message = f"@{tweet.handle} 内容太短，已标记跳过"
                logger.info("%s [%s]", message, tweet.external_id)
                errors.append(message)
                continue

            try:
                translated = await self._translator.translate(tweet)
                content = self._formatter.format(translated)
            except Exception as exc:
                logger.warning("翻译失败 [%s]: %s", tweet.external_id, exc)
                errors.append(f"@{tweet.handle} 翻译失败: {exc}")
                continue

            payload: dict[str, object]
            try:
                payload = await self._build_payload(content, tweet)
            except Exception as exc:
                logger.error("截图失败 [%s]: %s", tweet.external_id, exc)
                errors.append(f"@{tweet.handle} 截图失败: {exc}")
                continue

            cleanup_paths = payload.get("cleanup_paths", payload.get("images", []))
            try:
                await self._telegram.notify_content(payload)
            except Exception as exc:
                logger.error("发送失败 [%s]: %s", tweet.external_id, exc)
                self._cleanup_images(cleanup_paths)
                errors.append(f"@{tweet.handle} 发送失败: {exc}")
                continue

            try:
                await self._repo.save_sent(content)
            except Exception as exc:
                logger.error("发送后写入 sent 失败 [%s]: %s", tweet.external_id, exc)
                errors.append(f"@{tweet.handle} 状态保存失败: {exc}")
                self._cleanup_images(cleanup_paths)
                continue

            self._cleanup_images(cleanup_paths)
            sent += 1

        return {"fetched": fetched, "inserted": inserted, "sent": sent, "errors": errors}

    async def status(self) -> dict[str, int]:
        await self.setup()
        return await self._repo.status_counts()

    async def _build_payload(self, content: ProcessedContent, tweet: RawTweet) -> dict[str, object]:
        file_key = content.tweet_external_id.replace("/", "_").replace(":", "_")[-40:]
        created_paths: list[str] = []

        try:
            # 截图
            screenshot_path = await self._screenshotter.screenshot(tweet.url, file_key)
            if screenshot_path and screenshot_path.exists():
                created_paths.append(str(screenshot_path))

            # 下载原始图片
            downloaded_paths = ()
            if tweet.image_urls:
                downloaded_paths = await self._downloader.download_many(tweet.image_urls, file_key)
                created_paths.extend(str(path) for path in downloaded_paths if path.exists())

            # 图片覆盖（截图存在时，生成中文覆盖图）
            overlay_path: Path | None = None
            if screenshot_path and screenshot_path.exists():
                try:
                    translations = await self._translator.translate_literal_parts(tweet)
                    overlay_path = self._overlayer.append_translations(screenshot_path, translations)
                    if overlay_path and overlay_path.exists():
                        created_paths.append(str(overlay_path))
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
                "cleanup_paths": created_paths,
            }
        except Exception:
            self._cleanup_images(created_paths)
            raise

    def _cleanup_images(self, images: list[str] | object) -> None:
        for image_str in images if isinstance(images, list) else []:
            path = Path(image_str)
            try:
                if path.exists():
                    path.unlink()
            except Exception as exc:
                logger.warning("图片删除失败: %s — %s", path, exc)
