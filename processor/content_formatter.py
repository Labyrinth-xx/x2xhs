from __future__ import annotations

import re
from dataclasses import replace

from scraper.models import ProcessedContent

# 匹配所有 URL（含 t.co 短链）
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# 匹配 @用户名（字母、数字、下划线）
_MENTION_RE = re.compile(r"@\w+")


def _strip_links_and_mentions(text: str) -> str:
    """移除 URL 和 @提及，避免平台引流标记。"""
    text = _URL_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    # 清理因删除产生的多余空格和行首行尾空白
    lines = [" ".join(line.split()) for line in text.splitlines()]
    # 合并连续空行（最多保留一个空行）
    cleaned: list[str] = []
    prev_blank = False
    for line in lines:
        is_blank = line == ""
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank
    return "\n".join(cleaned).strip()


class ContentFormatter:
    def format(self, content: ProcessedContent) -> ProcessedContent:
        title = _strip_links_and_mentions(content.title_zh.strip())
        body = _strip_links_and_mentions(content.body_zh.strip())
        tags = self._normalize_tags(content.tags)
        return replace(content, title_zh=title, body_zh=body, tags=tags)

    def _normalize_tags(self, tags: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [tag.strip().lstrip("#") for tag in tags if tag.strip()]
        unique = list(dict.fromkeys(normalized))
        return tuple(unique[:5])
