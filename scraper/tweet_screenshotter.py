from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_COOKIE_DOMAINS = [".x.com", ".twitter.com"]


class TweetScreenshotter:
    def __init__(self, auth_token: str, output_dir: Path) -> None:
        self._auth_token = auth_token
        self._output_dir = output_dir

    async def screenshot(self, tweet_url: str, file_key: str) -> Path | None:
        if not self._auth_token:
            logger.warning("TWITTER_AUTH_TOKEN 未配置，跳过推文截图")
            return None
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / f"{file_key}_tweet.png"
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 620, "height": 900},
                    device_scale_factor=2,
                )
                await context.add_cookies(
                    [
                        {
                            "name": "auth_token",
                            "value": self._auth_token,
                            "domain": domain,
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                        }
                        for domain in _COOKIE_DOMAINS
                    ]
                )
                page = await context.new_page()
                await page.goto(tweet_url, wait_until="domcontentloaded", timeout=30000)
                article = page.locator('article[data-testid="tweet"]').first
                await article.wait_for(state="visible", timeout=15000)
                # 等待推文用户名出现，确保卡片头部已渲染
                await page.locator('[data-testid="User-Name"]').first.wait_for(
                    state="visible", timeout=10000
                )
                # 关掉 cookie 弹窗（如果出现）
                for label in ("Accept all cookies", "Refuse non-essential cookies"):
                    btn = page.get_by_role("button", name=label)
                    if await btn.count() > 0:
                        await btn.click()
                        await page.wait_for_timeout(800)
                        break
                await article.scroll_into_view_if_needed()
                await page.wait_for_timeout(1000)
                # 将鼠标移出 article 区域，避免触发 hover card 遮挡内容
                await page.mouse.move(0, 0)
                await page.wait_for_timeout(500)
                # 用页面截图 + clip，避免元素边界截断头像等溢出内容
                box = await article.bounding_box()
                padding = 12
                await page.screenshot(
                    path=str(output_path),
                    clip={
                        "x": max(0, box["x"] - padding),
                        "y": max(0, box["y"] - padding),
                        "width": box["width"] + padding * 2,
                        "height": box["height"] + padding * 2,
                    },
                )
                await browser.close()
            logger.info("推文截图完成: %s", output_path.name)
            return output_path
        except Exception as exc:
            logger.warning("推文截图失败 [%s]: %s", tweet_url, exc)
            return None
