from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image

logger = logging.getLogger(__name__)


class ImageDownloader:
    def __init__(self, output_dir: Path, timeout_seconds: float = 20.0) -> None:
        self._output_dir = output_dir
        self._timeout_seconds = timeout_seconds

    async def download_many(self, image_urls: tuple[str, ...], prefix: str) -> tuple[Path, ...]:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        tasks = [self._download_one(url, prefix, index) for index, url in enumerate(image_urls, 1)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        paths: list[Path] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("图片下载失败: %s", result)
                continue
            paths.append(result)
        return tuple(paths)

    async def _download_one(self, image_url: str, prefix: str, index: int) -> Path:
        image_url = unescape(image_url)
        async with httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=True) as client:
            response = await client.get(image_url)
            response.raise_for_status()
        suffix = self._guess_suffix(image_url, response.content)
        file_path = self._output_dir / f"{prefix}_{index}{suffix}"
        await asyncio.to_thread(file_path.write_bytes, response.content)
        return file_path

    def _guess_suffix(self, image_url: str, content: bytes) -> str:
        parsed = Path(urlparse(image_url).path).suffix.lower()
        if parsed in {".jpg", ".jpeg", ".png", ".webp"}:
            return parsed
        image = Image.open(BytesIO(content))
        format_name = (image.format or "jpg").lower()
        return ".jpg" if format_name == "jpeg" else f".{format_name}"

