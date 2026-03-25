from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ProcessedStatus(str, Enum):
    SENT = "sent"
    FILTERED = "filtered"


@dataclass(frozen=True, slots=True)
class RawTweet:
    external_id: str
    handle: str
    content: str
    url: str
    published_at: datetime
    source_type: str
    source_value: str
    image_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessedContent:
    tweet_external_id: str
    handle: str
    raw_url: str
    published_at: datetime
    title_zh: str
    body_zh: str
    tags: tuple[str, ...]
    mode: str = "light"
    status: ProcessedStatus = ProcessedStatus.SENT
    pushed_at: str | None = None
