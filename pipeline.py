from __future__ import annotations

import logging
import re
from pathlib import Path

from config import AppConfig
from processor.content_formatter import ContentFormatter
from processor.scorer import TweetScorer
from processor.translator import ClaudeTranslator, TranslationSkipped
from processor.viral_selector import ViralSelector
from publisher.image_overlay import TweetImageOverlayer
from publisher.telegram_notifier import TelegramNotifier
from scraper.image_downloader import ImageDownloader
from scraper.models import DigestContent, ProcessedContent, RawTweet
from scraper.rsshub_client import RSSHubClient
from scraper.twscrape_client import TwscrapeClient
from scraper.tweet_screenshotter import TweetScreenshotter
from scraper.xai_client import XAIClient
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
        self._scorer = TweetScorer(config.processor.openrouter_api_key, config.filter)
        self._formatter = ContentFormatter()
        self._threshold_override: float | None = None
        self._last_candidates: list[dict] = []
        self._overlayer = TweetImageOverlayer()
        self._screenshotter = TweetScreenshotter(
            auth_token=config.publisher.twitter_auth_token,
            output_dir=config.publisher.image_dir,
        )
        self._downloader = ImageDownloader(output_dir=config.publisher.image_dir)
        self._telegram = TelegramNotifier(config.telegram) if config.telegram else None
        self._xai = XAIClient(config.xai, config.processor.openrouter_api_key) if config.xai else None
        self._viral_selector = ViralSelector(config.processor)
        self._presented_candidate_ids: set[str] = set()

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
        # 重启后预填已展示 ID，避免重复推送
        if not self._presented_candidate_ids:
            existing = await self._repo.list_scored_candidates(
                min_score=0, limit=50,
            )
            self._presented_candidate_ids = {c["external_id"] for c in existing}
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

    # ── 评分+候选推送（定时任务调用） ──

    async def score_and_present(
        self,
        scrape_first: bool = True,
        accounts: list[str] | None = None,
    ) -> dict:
        """抓取 → 评分未打分推文 → 返回候选列表消息。"""
        await self.setup()

        fetched = inserted = scored = 0
        if scrape_first:
            result = await self.scrape(use_rsshub=True, accounts=accounts)
            fetched = result["fetched"]
            inserted = result["inserted"]

        # 评分
        unscored = await self._repo.list_unscored_tweets(limit=50)
        feedback_rows = await self._repo.list_recent_feedback(limit=10)
        feedback_lines = [
            f"{fb['content']}（{fb['created_at'][:10]}）"
            for fb in feedback_rows
        ] if feedback_rows else None

        for tweet in unscored:
            result = await self._scorer.score(
                tweet.handle, tweet.content, feedback_lines,
            )
            if result is None:
                continue
            score, reason, detail = result
            await self._repo.save_score(tweet.external_id, score, reason, detail)
            scored += 1

        # 过期清理
        expired = await self._repo.expire_old_tweets(
            hours=self._config.filter.expire_hours,
        )
        if expired:
            logger.info("过期清理 %d 条", expired)

        # 取候选
        candidates = await self._repo.list_scored_candidates(
            min_score=self.threshold,
            limit=5,
        )

        message = self._format_candidates(candidates) if candidates else None
        return {
            "fetched": fetched,
            "inserted": inserted,
            "scored": scored,
            "candidates": candidates,
            "message": message,
        }

    def _format_candidates(
        self,
        candidates: list[dict],
        new_ids: set[str] | None = None,
    ) -> str:
        """格式化候选推文列表为 Telegram 消息。"""
        lines = ["📋 候选推文\n"]
        for c in candidates:
            preview = c["content"][:150].replace("\n", " ")
            source_type = c.get("source_type", "account")
            source_value = c.get("source_value", c["handle"])
            if source_type == "keyword":
                source_tag = f" （关键词: {source_value}）"
            elif source_value != c["handle"]:
                source_tag = f" （来自 @{source_value}）"
            else:
                source_tag = ""
            title_line = f"「{c['preview_title']}」\n" if c.get("preview_title") else ""
            new_badge = "🆕 " if (new_ids and c["external_id"] in new_ids) else ""
            lines.append(
                f"{new_badge}📌 [{c['filter_score']:g}分] @{c['handle']}{source_tag}\n"
                f"{title_line}"
                f"{preview}...\n"
                f"💡 {c['filter_reason']}\n"
            )
        return "\n".join(lines)

    @staticmethod
    def _sort_candidates_new_first(
        candidates: list[dict],
        new_ids: set[str],
    ) -> list[dict]:
        """新推文置顶，其余按评分降序。"""
        new = sorted(
            [c for c in candidates if c["external_id"] in new_ids],
            key=lambda c: c["filter_score"], reverse=True,
        )
        old = sorted(
            [c for c in candidates if c["external_id"] not in new_ids],
            key=lambda c: c["filter_score"], reverse=True,
        )
        return new + old

    async def process_candidate(self, index: int) -> dict:
        """用户确认后处理指定候选：翻译 + 截图 + 覆盖图 → 发送到 Telegram。"""
        await self.setup()

        if not self._last_candidates or index < 1 or index > len(self._last_candidates):
            raise ValueError(f"无效的候选序号 {index}，当前有 {len(self._last_candidates)} 条候选")

        candidate = self._last_candidates[index - 1]
        tweet = await self._repo.get_tweet(candidate["external_id"])
        if tweet is None:
            raise ValueError(f"找不到推文: {candidate['external_id']}")

        # 长度检查
        text_only = _URL_RE.sub("", tweet.content).strip()
        if len(text_only) < self._MIN_TRANSLATABLE_CHARS:
            await self._repo.save_filtered(
                tweet_external_id=tweet.external_id,
                handle=tweet.handle,
                raw_url=tweet.url,
                published_at=tweet.published_at,
            )
            return {"success": False, "reason": "内容太短"}

        # 翻译
        translated = await self._translator.translate(tweet)
        content = self._formatter.format(translated)

        # 截图 + 覆盖图 + 组装 payload
        payload = await self._build_payload(content, tweet)

        # 发送到 Telegram
        if self._telegram is None:
            raise ValueError("未配置 Telegram")
        await self._telegram.notify_content(payload)

        # 标记 sent
        await self._repo.save_sent(content)
        self._cleanup_images(payload.get("cleanup_paths", []))

        return {"success": True, "title": content.title_zh, "handle": tweet.handle}

    # ── 反馈方法 ──

    async def add_feedback(self, content: str) -> None:
        await self.setup()
        await self._repo.save_feedback(content)

    async def list_recent_scores(self, limit: int = 10) -> list[dict]:
        await self.setup()
        return await self._repo.list_recent_scores(limit)

    @property
    def threshold(self) -> float:
        if self._threshold_override is not None:
            return self._threshold_override
        return self._config.filter.threshold

    @property
    def candidate_count(self) -> int:
        return len(self._last_candidates)

    async def set_threshold(self, value: float) -> None:
        """运行时调整评分阈值（不持久化到 .env，重启恢复默认）。"""
        self._threshold_override = value

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

        _MAX_DELIVER = 5  # 绝对上限，防止 NLP 误解析导致刷屏
        effective_limit = min(limit or 2, _MAX_DELIVER)

        # 未指定账号时，只推送监控账号的推文；关键词抓取结果仅用于发现，不自动投递
        delivery_handles = accounts
        if delivery_handles is None:
            delivery_handles = await self._repo.list_accounts() or None

        # 先评分未评分推文，确保候选有分数
        unscored = await self._repo.list_unscored_tweets(limit=50)
        if unscored:
            feedback_rows = await self._repo.list_recent_feedback(limit=10)
            feedback_lines = [
                f"{fb['content']}（{fb['created_at'][:10]}）"
                for fb in feedback_rows
            ] if feedback_rows else None
            for tweet in unscored:
                result = await self._scorer.score(
                    tweet.handle, tweet.content, feedback_lines,
                )
                if result is None:
                    continue
                score, reason, detail = result
                await self._repo.save_score(tweet.external_id, score, reason, detail)

        candidates = await self._repo.list_candidate_tweets(
            effective_limit,
            handles=delivery_handles,
            force=force,
            min_score=self.threshold,
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
            except TranslationSkipped as exc:
                await self._repo.save_filtered(
                    tweet_external_id=tweet.external_id,
                    handle=tweet.handle,
                    raw_url=tweet.url,
                    published_at=tweet.published_at,
                )
                logger.info("模型跳过 [%s]: %s", tweet.external_id, exc.reason)
                errors.append(f"@{tweet.handle} 跳过: {exc.reason}")
                continue
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
            # 截图（X 平台内置翻译已在截图中；同时返回引用卡片底部 Y）
            screenshot_result = await self._screenshotter.screenshot(tweet.url, file_key)
            screenshot_path: Path | None = None
            quoted_bottom_y: int | None = None
            if screenshot_result:
                screenshot_path, quoted_bottom_y = screenshot_result
            if screenshot_path and screenshot_path.exists():
                created_paths.append(str(screenshot_path))

            # 下载原始图片
            downloaded_paths = ()
            if tweet.image_urls:
                downloaded_paths = await self._downloader.download_many(tweet.image_urls, file_key)
                created_paths.extend(str(path) for path in downloaded_paths if path.exists())

            # 引用推文翻译卡片 — 暂时禁用，主推文已由 X 平台 Google 翻译覆盖
            # 待启用时取消注释：
            # overlay_path: Path | None = None
            # if screenshot_path and screenshot_path.exists() and quoted_bottom_y is not None:
            #     try:
            #         translations = await self._translator.translate_literal_parts(tweet)
            #         quoted_translation = translations[1] if len(translations) >= 2 else None
            #         if quoted_translation:
            #             overlay_path = self._overlayer.append_at_y(
            #                 screenshot_path, quoted_translation, quoted_bottom_y
            #             )
            #             if overlay_path and overlay_path.exists():
            #                 created_paths.append(str(overlay_path))
            #     except Exception as exc:
            #         logger.warning("引用推文翻译卡片生成失败，使用原始截图: %s", exc)
            overlay_path: Path | None = None

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

    # ── 功能一：关键词爆文搜索 ──

    async def keyword_viral(self, keyword: str) -> dict:
        """按关键词找最有价值的单条推文，翻译截图后发送到 Telegram。

        流程：twscrape 高阈值预筛 → AI 质量选择 → 翻译截图 → Telegram。
        twscrape 失效时自动降级为 xAI x_search。
        """
        await self.setup()

        if self._telegram is None:
            raise ValueError("未配置 Telegram，请检查 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")

        viral_min_faves = (
            self._config.xai.viral_min_faves if self._config.xai else 500
        )
        viral_limit = (
            self._config.xai.viral_candidate_limit if self._config.xai else 15
        )

        # Step 1: twscrape 高阈值预筛
        candidates: list[RawTweet] = []
        try:
            original_min_faves = self._config.scraper.min_faves
            # 临时构造高阈值查询（直接调底层方法）
            candidates = await self._twscrape.fetch_keyword_tweets(
                keyword, min_faves_override=viral_min_faves, limit=viral_limit
            )
            logger.info("twscrape 抓到 %d 条候选（关键词: %s）", len(candidates), keyword)
        except Exception as exc:
            logger.warning("twscrape 预筛失败: %s", exc)

        # Step 2: 降级 — xAI x_search
        if not candidates:
            if self._xai is None:
                return {"success": False, "reason": "twscrape 无结果且未配置 XAI_MODEL"}
            logger.info("降级到 xAI x_search（关键词: %s）", keyword)
            try:
                candidates = await self._xai.search_viral_fallback(keyword, n=viral_limit)
            except Exception as exc:
                logger.error("xAI viral_fallback 失败: %s", exc)
                return {"success": False, "reason": f"xAI 搜索失败: {exc}"}

        if not candidates:
            return {"success": False, "reason": f"关键词 [{keyword}] 未找到候选推文"}

        # Step 3: AI 质量选择
        selected = await self._viral_selector.select_best_tweet(keyword, candidates)

        # Step 4: 保存到 DB
        await self._repo.save_tweets([selected])

        # Step 5: 翻译
        text_only = _URL_RE.sub("", selected.content).strip()
        if len(text_only) < self._MIN_TRANSLATABLE_CHARS:
            return {"success": False, "reason": "选中推文内容太短"}

        try:
            translated = await self._translator.translate(selected)
            content = self._formatter.format(translated)
        except TranslationSkipped as exc:
            return {"success": False, "reason": f"翻译跳过: {exc.reason}"}
        except Exception as exc:
            return {"success": False, "reason": f"翻译失败: {exc}"}

        # Step 6: 截图 + 发送
        payload = await self._build_payload(content, selected)
        await self._telegram.notify_content(payload)
        await self._repo.save_sent(content)
        self._cleanup_images(payload.get("cleanup_paths", []))

        return {
            "success": True,
            "title": content.title_zh,
            "handle": selected.handle,
            "source": "twscrape" if selected.source_type == "keyword" else "xai",
        }

    # ── 功能二：话题综述 ──

    async def topic_digest(self, keyword: str) -> dict:
        """用 xAI 搜索 X + 网络新闻，生成中文话题综述，发送到 Telegram 待审。"""
        await self.setup()

        if self._xai is None:
            return {"success": False, "reason": "未配置 XAI_MODEL，话题综述需要 xAI"}

        if self._telegram is None:
            raise ValueError("未配置 Telegram，请检查 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")

        try:
            digest = await self._xai.search_digest(keyword)
        except Exception as exc:
            logger.error("xAI search_digest 失败: %s", exc)
            return {"success": False, "reason": f"xAI 生成失败: {exc}"}

        tags_text = "  ".join(f"#{t}" for t in digest.tags)
        message = (
            f"📰 {keyword}\n\n"
            f"【{digest.title_zh}】\n\n"
            f"{digest.body_zh}\n\n"
            f"{tags_text}"
        )
        if digest.citation_urls:
            sources = "\n".join(f"- {u}" for u in digest.citation_urls[:5])
            message += f"\n\n来源：\n{sources}"

        return {
            "success": True,
            "keyword": keyword,
            "title": digest.title_zh,
            "body_length": len(digest.body_zh),
            "message": message,
        }

    def _cleanup_images(self, images: list[str] | object) -> None:
        for image_str in images if isinstance(images, list) else []:
            path = Path(image_str)
            try:
                if path.exists():
                    path.unlink()
            except Exception as exc:
                logger.warning("图片删除失败: %s — %s", path, exc)
