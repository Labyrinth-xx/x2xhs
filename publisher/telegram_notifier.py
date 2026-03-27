from __future__ import annotations

import json
import logging
import mimetypes
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx

from config import TelegramConfig

REMOTE_URL_PATTERN = re.compile(r"^https?://", flags=re.IGNORECASE)
logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, config: TelegramConfig) -> None:
        self._config = config
        self._base_url = f"https://api.telegram.org/bot{config.bot_token}"
        self._timeout = 30.0

    async def notify_content(self, payload: dict[str, Any]) -> None:
        title = str(payload.get("title", "")).strip() or "无标题"
        body = str(payload.get("body", "")).strip() or "无正文"
        tags = self._format_tags(payload.get("tags", []))
        images = [str(image).strip() for image in payload.get("images", []) if str(image).strip()]

        await self._send_message(f"📌 {title}")
        await self._send_message(body)
        await self._send_message(tags or "无标签")
        if images:
            await self._send_images(images)

    async def send_text(self, text: str) -> None:
        """发送纯文字消息（供外部调用）。"""
        await self._send_message(text)

    async def _send_message(self, text: str) -> None:
        await self._post("sendMessage", data={"chat_id": self._config.chat_id, "text": text})

    async def _send_images(self, images: list[str]) -> None:
        if len(images) == 1:
            await self._send_single_image(images[0])
            return
        for start in range(0, len(images), 10):
            chunk = images[start : start + 10]
            if len(chunk) == 1:
                await self._send_single_image(chunk[0])
                continue
            await self._send_media_group(chunk)

    async def _send_single_image(self, image: str) -> None:
        if self._is_remote(image):
            await self._post(
                "sendPhoto",
                data={"chat_id": self._config.chat_id, "photo": image},
            )
            return

        path = Path(image).resolve()
        if not path.exists():
            logger.warning("图片不存在，跳过发送: %s", path)
            return
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as file_handle:
            await self._post(
                "sendPhoto",
                data={"chat_id": self._config.chat_id},
                files={"photo": (path.name, file_handle, mime_type)},
            )

    async def _send_media_group(self, images: list[str]) -> None:
        with ExitStack() as stack:
            media: list[dict[str, str]] = []
            files: list[tuple[str, tuple[str, Any, str]]] = []
            for index, image in enumerate(images):
                if self._is_remote(image):
                    media.append({"type": "photo", "media": image})
                    continue

                path = Path(image).resolve()
                if not path.exists():
                    logger.warning("图片不存在，跳过发送: %s", path)
                    continue
                attach_name = f"image_{index}"
                mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                file_handle = stack.enter_context(path.open("rb"))
                files.append((attach_name, (path.name, file_handle, mime_type)))
                media.append({"type": "photo", "media": f"attach://{attach_name}"})

            if not media:
                return
            await self._post(
                "sendMediaGroup",
                data={
                    "chat_id": self._config.chat_id,
                    "media": json.dumps(media, ensure_ascii=False),
                },
                files=files or None,
            )

    async def _post(
        self,
        method: str,
        data: dict[str, Any],
        files: Any = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}/{method}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, data=data, files=files)
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(
                f"Telegram API 调用失败 [{method}] HTTP {response.status_code}: {response.text}"
            ) from None
        if not response.is_success or not payload.get("ok", False):
            raise RuntimeError(
                f"Telegram API 调用失败 [{method}] HTTP {response.status_code}: {payload}"
            )
        return payload

    def _format_tags(self, tags: Any) -> str:
        normalized = []
        for tag in tags or []:
            value = str(tag).strip().lstrip("#")
            if value:
                normalized.append(f"#{value}")
        return " ".join(normalized)

    def _is_remote(self, image: str) -> bool:
        return bool(REMOTE_URL_PATTERN.match(image))
