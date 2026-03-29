import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processor.context_enricher import ResearchBrief
from processor.translator import ClaudeTranslator
from scraper.models import RawTweet


def _make_tweet(handle: str = "elonmusk", content: str = "test content") -> RawTweet:
    return RawTweet(
        external_id="1",
        handle=handle,
        content=content,
        url="https://x.com/elonmusk/status/1",
        published_at=datetime(2026, 3, 27, tzinfo=timezone.utc),
        source_type="rsshub",
        source_value="",
    )


def test_sanitize_json_keeps_object_keys_intact() -> None:
    raw = """{
  "title_zh": "标题",
  "body_zh": "正文",
  "tags": ["A", "B", "C"]
}"""

    sanitized = ClaudeTranslator._sanitize_json(raw)
    data = json.loads(sanitized)

    assert data["title_zh"] == "标题"
    assert data["body_zh"] == "正文"
    assert data["tags"] == ["A", "B", "C"]


def test_sanitize_json_escapes_bare_quotes_inside_values() -> None:
    raw = """{
  "title_zh": "标题",
  "body_zh": "这里提到 "auto mode" 和 "agents"",
  "tags": ["A", "B", "C"]
}"""

    sanitized = ClaudeTranslator._sanitize_json(raw)
    data = json.loads(sanitized)

    assert data["body_zh"] == '这里提到 "auto mode" 和 "agents"'


# ── _build_prompt 背景调研注入 ─────────────────────────────────────────────

def _make_dummy_translator() -> ClaudeTranslator:
    """构造一个无需真实 API key 的 translator 实例（仅测试 _build_prompt）。"""
    from config import ProcessorConfig
    cfg = ProcessorConfig(openrouter_api_key="test-key", model="test-model")
    return ClaudeTranslator(cfg)


def test_build_prompt_injects_brief_when_present() -> None:
    translator = _make_dummy_translator()
    tweet = _make_tweet()
    brief = ResearchBrief(
        author_recent="马斯克已离开DOGE",
        event_context="近期事件脉络",
        notable_connections="时间巧合线索",
    )
    prompt = translator._build_prompt(tweet, brief)

    assert "你已了解到" in prompt
    assert "马斯克已离开DOGE" in prompt
    assert "原文内容" in prompt


def test_build_prompt_no_brief_section_when_none() -> None:
    translator = _make_dummy_translator()
    tweet = _make_tweet()
    prompt = translator._build_prompt(tweet, None)

    assert "你已了解到" not in prompt
    assert "原文内容" in prompt


def test_build_prompt_brief_appears_before_tweet_content() -> None:
    translator = _make_dummy_translator()
    tweet = _make_tweet(content="原始推文内容")
    brief = ResearchBrief(
        author_recent="背景内容",
        event_context="", notable_connections="",
    )
    prompt = translator._build_prompt(tweet, brief)

    assert prompt.index("背景内容") < prompt.index("原始推文内容")
