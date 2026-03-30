from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _split_csv(value: str) -> tuple[str, ...]:
    items = [item.strip() for item in value.split(",")]
    return tuple(item for item in items if item)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"缺少必填环境变量: {name}")
    return value


def _parse_int(name: str, default: str) -> int:
    value = os.getenv(name, default).strip()
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数，当前值: {value!r}") from exc


def _parse_float(name: str, default: str) -> float:
    value = os.getenv(name, default).strip()
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字，当前值: {value!r}") from exc


def _parse_max_tweets() -> int:
    value = os.getenv("SCRAPER_MAX_TWEETS", "20").strip()
    try:
        max_tweets = int(value)
    except ValueError as exc:
        raise ValueError("SCRAPER_MAX_TWEETS 必须是整数") from exc
    if max_tweets <= 0:
        raise ValueError("SCRAPER_MAX_TWEETS 必须大于 0")
    return max_tweets


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    base_url: str
    accounts: tuple[str, ...]
    keywords: tuple[str, ...]
    max_tweets: int
    timeout_seconds: float = 15.0
    max_retries: int = 3
    twitter_username: str = ""
    twitter_password: str = ""
    twitter_email: str = ""
    twitter_auth_token: str = ""
    twitter_ct0: str = ""
    min_faves: int = 100
    min_retweets: int = 20


@dataclass(frozen=True, slots=True)
class ProcessorConfig:
    openrouter_api_key: str
    model: str = "google/gemini-flash-1.5"
    max_concurrency: int = 2


@dataclass(frozen=True, slots=True)
class PublisherConfig:
    image_dir: Path
    twitter_auth_token: str = ""


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass(frozen=True, slots=True)
class FilterConfig:
    threshold: float = 7.5
    model: str = "deepseek/deepseek-chat"
    expire_hours: int = 72


@dataclass(frozen=True, slots=True)
class XAIConfig:
    model: str = "x-ai/grok-4-fast"
    viral_min_faves: int = 500
    viral_candidate_limit: int = 15
    sweep_interval_hours: int = 8
    sweep_max_queries: int = 10
    keyword_score_threshold: float = 7.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    root_dir: Path
    data_dir: Path
    db_path: Path
    scraper: ScraperConfig
    processor: ProcessorConfig
    publisher: PublisherConfig
    telegram: TelegramConfig | None
    filter: FilterConfig = FilterConfig()
    poll_interval_minutes: int = 60
    xai: XAIConfig | None = None


def load_config() -> AppConfig:
    root_dir = Path(__file__).resolve().parent
    load_dotenv(root_dir / ".env")

    data_dir = root_dir / "data"
    output_dir = root_dir / "output"

    scraper_accounts_env = os.getenv("SCRAPER_ACCOUNTS", "").strip()
    scraper = ScraperConfig(
        base_url=_require_env("RSSHUB_BASE_URL").rstrip("/"),
        accounts=_split_csv(scraper_accounts_env) if scraper_accounts_env else (),
        keywords=_split_csv(os.getenv("SCRAPER_KEYWORDS", "")),
        max_tweets=_parse_max_tweets(),
        twitter_username=os.getenv("TWITTER_USERNAME", "").strip(),
        twitter_password=os.getenv("TWITTER_PASSWORD", "").strip(),
        twitter_email=os.getenv("TWITTER_EMAIL", "").strip(),
        twitter_auth_token=os.getenv("TWSCRAPE_AUTH_TOKEN", "").strip(),
        twitter_ct0=os.getenv("TWSCRAPE_CT0", "").strip(),
        min_faves=_parse_int("TWITTER_MIN_FAVES", "100"),
        min_retweets=_parse_int("TWITTER_MIN_RETWEETS", "20"),
    )
    processor = ProcessorConfig(
        openrouter_api_key=_require_env("OPENROUTER_API_KEY"),
        model=os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat"),
    )
    publisher = PublisherConfig(
        image_dir=output_dir / "images",
        twitter_auth_token=os.getenv("TWITTER_AUTH_TOKEN", ""),
    )
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    telegram = (
        TelegramConfig(bot_token=telegram_bot_token, chat_id=telegram_chat_id)
        if telegram_bot_token and telegram_chat_id
        else None
    )
    filter_cfg = FilterConfig(
        threshold=_parse_float("FILTER_THRESHOLD", "7.5"),
        model=os.getenv("FILTER_MODEL", "deepseek/deepseek-chat").strip(),
        expire_hours=_parse_int("FILTER_EXPIRE_HOURS", "72"),
    )
    poll_interval = _parse_int("POLL_INTERVAL_MINUTES", "60")
    xai_model = os.getenv("XAI_MODEL", "").strip()
    xai_cfg = (
        XAIConfig(
            model=xai_model,
            viral_min_faves=_parse_int("VIRAL_MIN_FAVES", "500"),
            viral_candidate_limit=_parse_int("VIRAL_CANDIDATE_LIMIT", "15"),
            sweep_interval_hours=_parse_int("SWEEP_INTERVAL_HOURS", "8"),
            sweep_max_queries=_parse_int("SWEEP_MAX_QUERIES", "10"),
            keyword_score_threshold=_parse_float("KEYWORD_SCORE_THRESHOLD", "7.0"),
        )
        if xai_model
        else None
    )
    return AppConfig(
        root_dir=root_dir,
        data_dir=data_dir,
        db_path=data_dir / "x2xhs.db",
        scraper=scraper,
        processor=processor,
        publisher=publisher,
        telegram=telegram,
        filter=filter_cfg,
        poll_interval_minutes=poll_interval,
        xai=xai_cfg,
    )
