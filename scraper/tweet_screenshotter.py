from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_COOKIE_DOMAINS = [".x.com", ".twitter.com"]

# 隐藏 sticky header 的 CSS（截图前注入，避免头像被遮挡）
_HIDE_HEADER_CSS = """
    div[data-testid="primaryColumn"] > div > div:first-child {
        visibility: hidden !important;
    }
"""


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
                # 关掉 cookie 弹窗（中英文均处理）
                for label in (
                    "接受所有 Cookie", "Accept all cookies",
                    "拒绝非必要的 Cookie", "Refuse non-essential cookies",
                ):
                    btn = page.get_by_role("button", name=label)
                    if await btn.count() > 0:
                        await btn.click()
                        await page.wait_for_timeout(800)
                        break

                # 点击翻译按钮（账号语言设为中文后，英文推文会显示此按钮）
                for label in ("翻译帖子", "Translate post"):
                    btn = page.get_by_text(label, exact=False)
                    if await btn.count() > 0:
                        await btn.first.click()
                        await page.wait_for_timeout(3000)
                        logger.debug("推文翻译已展开")
                        break

                await article.scroll_into_view_if_needed()
                await page.wait_for_timeout(1000)
                # 将鼠标移出 article 区域，避免触发 hover card 遮挡内容
                await page.mouse.move(0, 0)
                await page.wait_for_timeout(300)

                # 隐藏 sticky header，防止遮挡头像
                await page.add_style_tag(content=_HIDE_HEADER_CSS)

                # 用 element.screenshot() 捕获整个 article（不受 viewport 高度限制）
                await article.screenshot(path=str(output_path))

                await browser.close()
            logger.info("推文截图完成: %s", output_path.name)
            return output_path
        except Exception as exc:
            logger.warning("推文截图失败 [%s]: %s", tweet_url, exc)
            return None
