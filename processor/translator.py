from __future__ import annotations

import json
import logging
from datetime import timezone
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from config import ProcessorConfig
from processor.context_enricher import ContextEnricher, ResearchBrief
from processor.prompts import build_system_prompt
from scraper.models import ProcessedContent, RawTweet

logger = logging.getLogger(__name__)


class TranslationSkipped(Exception):
    """模型判断推文不在处理范围内，应标记跳过而非重试。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_HANDLE_DISPLAY = {
    "claudeai": "Claude官方账号",
    "openai": "OpenAI官方账号",
    "googledeepmind": "Google DeepMind官方账号",
    "xai": "xAI官方账号",
    "grok": "Grok官方账号",
    "perplexity_ai": "Perplexity官方账号",
    "cursor_ai": "Cursor官方账号",
    "deepseek_ai": "DeepSeek官方账号",
}


class TranslationPayload(BaseModel):
    mode: Literal["light", "deep"] = "light"
    title_zh: str = Field(min_length=1)
    body_zh: str
    tags: list[str] = Field(min_length=3, max_length=5)

    @field_validator("title_zh")
    @classmethod
    def truncate_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("body_zh")
    @classmethod
    def validate_body_length(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 50:
            raise ValueError("body_zh 不能少于 50 字")
        if len(stripped) > 1000:
            raise ValueError("body_zh 不能超过 1000 字")
        return stripped

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        normalized = [tag.strip().lstrip("#") for tag in value if tag.strip()]
        if not 3 <= len(normalized) <= 5:
            raise ValueError("tags 必须包含 3-5 个话题")
        return normalized


class OpenRouterTranslator:
    def __init__(
        self,
        config: ProcessorConfig,
        enricher: ContextEnricher | None = None,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=config.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
        )
        self._model = config.model
        self._enricher = enricher

    async def translate(self, tweet: RawTweet) -> ProcessedContent:
        import re as _re

        brief: ResearchBrief | None = None
        if self._enricher is not None:
            brief = await self._enricher.fetch(tweet)

        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._build_prompt(tweet, brief)},
        ]
        last_exc: Exception | None = None
        for attempt in range(3):
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=4000,
                messages=messages,
            )
            msg = response.choices[0].message
            text = msg.content or ""
            # Thinking models (e.g. qwen3) put output in reasoning_content and leave content empty
            if not text:
                text = getattr(msg, "reasoning_content", None) or ""
            # Strip <think>...</think> blocks that some models embed in content
            text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
            try:
                payload = self._parse_response(text)
                break
            except TranslationSkipped:
                raise  # 模型拒绝，不重试
            except ValueError as exc:
                last_exc = exc
                logger.warning("翻译解析失败，第 %d 次重试（共 3 次）: %s", attempt + 1, exc)
        else:
            raise last_exc  # type: ignore[misc]

        return ProcessedContent(
            tweet_external_id=tweet.external_id,
            handle=tweet.handle,
            raw_url=tweet.url,
            published_at=tweet.published_at,
            title_zh=payload.title_zh.strip(),
            body_zh=payload.body_zh.strip(),
            tags=tuple(payload.tags),
            mode=payload.mode,
        )

    def _system_prompt(self) -> str:
        return build_system_prompt()

    def _build_prompt(self, tweet: RawTweet, brief: ResearchBrief | None = None) -> str:
        published_dt = tweet.published_at.astimezone(timezone.utc)
        display_name = _HANDLE_DISPLAY.get(tweet.handle.lower(), f"@{tweet.handle}")
        parts = [
            "请解读以下推文。",
            f"发布者：{display_name}（@{tweet.handle}）",
            f"发布日期：{published_dt.month}月{published_dt.day}日",
        ]
        if brief is not None:
            parts.append(brief.to_prompt_section())
        parts.append(f"原文内容：\n{tweet.content}")
        return "\n".join(parts) + "\n"

    async def translate_literal(self, tweet: RawTweet) -> str:
        """直译推文原文，忠实但语言自然，用于翻译卡片显示。"""
        parts = await self.translate_literal_parts(tweet)
        return parts[0] if parts else ""

    async def translate_literal_parts(self, tweet: RawTweet) -> list[str]:
        """直译推文，若含引用推文则分别返回 [主推文译文, 引用推文译文]，否则返回 [译文]。"""
        response = await self._client.chat.completions.create(
            model="google/gemini-2.0-flash-001",
            max_tokens=1200,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是专业翻译。将推文直译成中文。\n"
                        "要求：\n"
                        "1. 忠实原文，保留每个意思和顺序，不添加、不改写\n"
                        "2. 语言自然流畅，符合中文表达习惯，避免机翻腔调\n"
                        "3. 专有名词、产品名、账号名保留英文原文\n"
                        "4. 删除原文中所有 URL 链接（http://、https:// 开头的内容），不翻译、不保留\n"
                        "5. @用户名 格式的提及：去掉 @ 符号，只保留用户名本身\n"
                        "5b. 保留原文段落结构：原文换行处，译文也要换行，用 \\n 分隔\n"
                        "6. 如果推文包含引用推文（原文中通常出现另一账号名称后跟其内容），\n"
                        "   请分别翻译，以 JSON 返回：{\"main\": \"主推文译文\", \"quoted\": \"引用推文译文\"}\n"
                        "7. 如果没有引用推文，以 JSON 返回：{\"main\": \"译文\", \"quoted\": null}\n"
                        "8. 只返回 JSON，不加任何前缀或解释"
                    ),
                },
                {"role": "user", "content": tweet.content},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        return self._parse_literal_parts(text)

    def _parse_literal_parts(self, text: str) -> list[str]:
        """解析 translate_literal_parts 的 LLM 返回，提取 main 和 quoted 字段。"""
        import re as _re
        try:
            match = _re.search(r"\{.*\}", text, _re.DOTALL)
            if match:
                data = json.loads(self._sanitize_json(match.group()))
                main = (data.get("main") or "").strip()
                quoted = data.get("quoted")
                if main:
                    if quoted and isinstance(quoted, str) and quoted.strip():
                        return [main, quoted.strip()]
                    return [main]
        except Exception:
            pass
        # fallback：整段文本当作单条翻译
        clean = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
        return [clean] if clean else []

    def _parse_response(self, text: str) -> TranslationPayload:
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
        except ValueError as exc:
            # 模型返回了有意义的文本但没有 JSON → 大概率是拒绝/不在范围
            if len(text) > 10:
                reason = text[:200].replace("\n", " ")
                logger.info("模型拒绝处理: %s", reason)
                raise TranslationSkipped(reason) from exc
            logger.error("模型返回中未找到 JSON 对象: %r", text[:400])
            raise ValueError("模型返回中未找到 JSON 对象") from exc

        json_str = self._sanitize_json(text[start:end])
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            logger.error("模型返回 JSON 解析失败: %s | 清洗后: %r | 原文: %r", exc, json_str[:400], text[:400])
            raise ValueError("模型返回内容不是合法 JSON") from exc

        try:
            return TranslationPayload.model_validate(data)
        except ValidationError as exc:
            logger.error("模型返回 JSON 字段校验失败: %s | 数据: %r", exc, data)
            raise ValueError(f"模型返回 JSON 字段不符合要求: {exc.errors()[0]['msg']}") from exc

    @staticmethod
    def _sanitize_json(text: str) -> str:
        """修复模型输出的 JSON：
        1. 将结构层中文引号替换为英文双引号
        2. 移除字符串内的控制字符（如截断的 emoji 残留的代理字符）
        3. 转义字符串内部未转义的英文双引号（模型偶尔在 body_zh 内输出裸 " ）
        """
        OPEN_QUOTES = "\u300c\u300e\u201c"   # 「『"
        CLOSE_QUOTES = "\u300d\u300f\u201d"  # 」』"

        chars: list[str] = []
        in_string = False
        escaped = False
        i = 0
        n = len(text)

        while i < n:
            ch = text[i]
            if escaped:
                chars.append(ch)
                escaped = False
                i += 1
                continue
            if ch == "\\" and in_string:
                chars.append(ch)
                escaped = True
                i += 1
                continue
            if ch == '"':
                if not in_string:
                    in_string = True
                    chars.append(ch)
                else:
                    # 判断这个 " 是否为合法的关闭引号：
                    # 向前跳过空白，若紧跟 : , } ] 则为关闭引号；否则是字符串内裸 " 需转义
                    j = i + 1
                    while j < n and text[j] in " \t\n\r":
                        j += 1
                    if j >= n or text[j] in ":,}]":
                        in_string = False
                        chars.append(ch)
                    else:
                        chars.append('\\"')
                i += 1
                continue
            # 字符串内：转义裸换行/回车，过滤其他控制字符
            if in_string and ord(ch) < 0x20:
                if ch == "\n":
                    chars.append("\\n")
                elif ch == "\r":
                    chars.append("\\r")
                elif ch == "\t":
                    chars.append("\\t")
                # 其余控制字符直接丢弃
                i += 1
                continue
            # 结构层的中文引号替换为英文双引号
            if not in_string and (ch in OPEN_QUOTES or ch in CLOSE_QUOTES):
                chars.append('"')
                in_string = ch in OPEN_QUOTES
                i += 1
                continue
            chars.append(ch)
            i += 1

        return "".join(chars)


# 保持向后兼容的别名
ClaudeTranslator = OpenRouterTranslator
