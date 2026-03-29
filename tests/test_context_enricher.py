from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processor.context_enricher import ContextEnricher, ResearchBrief
from scraper.models import RawTweet

# ── 测试数据 ──────────────────────────────────────────────────────────────

_TWEET = RawTweet(
    external_id="123",
    handle="elonmusk",
    content="Just retweeted an interesting ant metaphor about ideology.",
    url="https://x.com/elonmusk/status/123",
    published_at=datetime(2026, 3, 27, tzinfo=timezone.utc),
    source_type="rsshub",
    source_value="",
)

_VALID_JSON = """{
  "author_recent": "马斯克于3月12日宣布将 xAI 与 Tesla 的 AI 部门合并，引发股东诉讼。",
  "event_context": "DOGE预计2026年7月关闭，3月初已开始裁减人员。",
  "notable_connections": "这条推文发布于 OpenAI 宣布新一轮融资的同一天。"
}"""

# ── 工具函数 ──────────────────────────────────────────────────────────────

def _make_enricher() -> ContextEnricher:
    return ContextEnricher(openrouter_api_key="test-key")


def _mock_response(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    response = MagicMock()
    response.choices = [choice]
    return response


# ── 测试用例 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_brief_on_success() -> None:
    enricher = _make_enricher()
    with patch.object(
        enricher._client.chat.completions,
        "create",
        new=AsyncMock(return_value=_mock_response(_VALID_JSON)),
    ):
        brief = await enricher.fetch(_TWEET)

    assert brief is not None
    assert "马斯克" in brief.author_recent
    assert brief.event_context != ""
    assert brief.notable_connections != ""


@pytest.mark.asyncio
async def test_returns_none_on_api_exception() -> None:
    enricher = _make_enricher()
    with patch.object(
        enricher._client.chat.completions,
        "create",
        new=AsyncMock(side_effect=RuntimeError("network error")),
    ):
        brief = await enricher.fetch(_TWEET)

    assert brief is None


@pytest.mark.asyncio
async def test_returns_none_on_invalid_json() -> None:
    enricher = _make_enricher()
    with patch.object(
        enricher._client.chat.completions,
        "create",
        new=AsyncMock(return_value=_mock_response("这不是 JSON")),
    ):
        brief = await enricher.fetch(_TWEET)

    assert brief is None


@pytest.mark.asyncio
async def test_returns_none_on_all_empty_fields() -> None:
    empty_json = """{
      "author_recent": "",
      "event_context": "",
      "notable_connections": ""
    }"""
    enricher = _make_enricher()
    with patch.object(
        enricher._client.chat.completions,
        "create",
        new=AsyncMock(return_value=_mock_response(empty_json)),
    ):
        brief = await enricher.fetch(_TWEET)

    assert brief is None


@pytest.mark.asyncio
async def test_citation_markers_stripped() -> None:
    json_with_citations = """{
      "author_recent": "马斯克已离开DOGE[[1]](https://example.com)。",
      "event_context": "相关报道[2]。",
      "notable_connections": "这与之前的声明矛盾。"
    }"""
    enricher = _make_enricher()
    with patch.object(
        enricher._client.chat.completions,
        "create",
        new=AsyncMock(return_value=_mock_response(json_with_citations)),
    ):
        brief = await enricher.fetch(_TWEET)

    assert brief is not None
    assert "[[1]]" not in brief.author_recent
    assert "[2]" not in brief.event_context


# ── ResearchBrief.to_prompt_section() ────────────────────────────────────

def test_to_prompt_section_renders_all_fields() -> None:
    brief = ResearchBrief(
        author_recent="发布者近期动态内容",
        event_context="事件脉络内容",
        notable_connections="关联线索内容",
    )
    section = brief.to_prompt_section()

    assert "发布者近期动态：发布者近期动态内容" in section
    assert "相关事件脉络：事件脉络内容" in section
    assert "值得注意的关联：关联线索内容" in section
    assert "你已了解到" in section


def test_to_prompt_section_skips_empty_fields() -> None:
    brief = ResearchBrief(
        author_recent="发布者近期动态内容",
        event_context="",
        notable_connections="关联线索内容",
    )
    section = brief.to_prompt_section()

    assert "发布者近期动态：发布者近期动态内容" in section
    assert "相关事件脉络：" not in section
    assert "值得注意的关联：关联线索内容" in section


def test_to_prompt_section_field_order() -> None:
    """event_context 应出现在 author_recent 之前。"""
    brief = ResearchBrief(
        author_recent="人物动态",
        event_context="事件脉络",
        notable_connections="关联线索",
    )
    section = brief.to_prompt_section()

    assert section.index("事件脉络") < section.index("人物动态")
    assert section.index("人物动态") < section.index("关联线索")


def test_is_empty_returns_true_when_all_blank() -> None:
    brief = ResearchBrief(
        author_recent="", event_context="", notable_connections="",
    )
    assert brief.is_empty() is True


def test_is_empty_returns_false_when_any_field_present() -> None:
    brief = ResearchBrief(
        author_recent="有内容", event_context="", notable_connections="",
    )
    assert brief.is_empty() is False
