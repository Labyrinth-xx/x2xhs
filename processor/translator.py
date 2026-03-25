from __future__ import annotations

import json
import logging
from datetime import timezone

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from config import ProcessorConfig
from scraper.models import ProcessedContent, RawTweet

logger = logging.getLogger(__name__)

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
    def __init__(self, config: ProcessorConfig) -> None:
        self._client = AsyncOpenAI(
            api_key=config.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
        )
        self._model = config.model

    async def translate(self, tweet: RawTweet) -> ProcessedContent:
        import re as _re

        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._build_prompt(tweet)},
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
        )

    def _system_prompt(self) -> str:
        return (
            "你是一位专业的中文科技资讯作者。"
            "你的任务不是逐句翻译，而是把外文内容转化为适合中文读者阅读的帖子。\n\n"
            "请按以下步骤完成：\n"
            "第一步：理解内容——阅读原文，理解作者真正想表达的观点、逻辑和重点。\n"
            "第二步：提炼核心信息——提取3–6个最重要的信息点或结论。\n"
            "第三步：中文重写——根据理解和信息点，用自然流畅的中文重新写成一篇帖子。\n\n"
            "写作要求：\n"
            "1. body_zh 的第一句话必须自然提到发布者名称和发布日期，日期写成 X月X日 格式。\n"
            "2. 用新闻报道式写法，客观、直接，不要写成社交媒体口吻。\n"
            "3. 严禁使用「快来试试吧」「你觉得如何呢」「一起来看看」等社交媒体号召句式。\n"
            "4. 不要逐句翻译，要用中文表达习惯重新组织语言。\n"
            "5. 逻辑清晰，读起来像中文原创内容。\n"
            "6. 保留关键事实、数据和结论。\n"
            "7. 删除无关的细节和重复内容。\n"
            "8. 语气自然、简洁、易读。\n"
            "9. 严禁在输出中包含任何 URL 或网址（包括 http://、https:// 开头的链接）。\n"
            "10. 严禁在输出中出现 @用户名 格式的账号提及。\n"
            "    如需提及某人，直接用其名字或描述（如「Karpathy」「OpenAI研究员」），不加 @ 符号。\n\n"
            "标题要求（title_zh）：\n"
            "1. 完整概括内容核心，字数不限，但要精炼有力。\n"
            "2. 基于事实本身的吸引力写标题，可以用语气词或感叹号加强语气，但禁止「震惊」「重磅」「颠覆」等夸大词汇。\n"
            "3. 标题必须言之有物，让读者一眼知道这条内容讲了什么。\n"
            "4. 可以制造合理的信息缺口（如「为什么……」「……原来是这样」），但不能脱离事实夸大。\n"
            "5. 禁止平淡陈述式标题，要有一定吸引力，但吸引力来自内容本身而非噱头。\n\n"
            "只返回 JSON，对应字段 title_zh、body_zh、tags。"
            "重要：JSON 结构本身必须使用英文双引号，但 title_zh 和 body_zh 的文字内容里如需引用，请用「」或『』，不要用英文双引号。"
        )

    def _build_prompt(self, tweet: RawTweet) -> str:
        published_dt = tweet.published_at.astimezone(timezone.utc)
        display_name = _HANDLE_DISPLAY.get(tweet.handle.lower(), f"@{tweet.handle}")
        return (
            "请完成内容转化。\n"
            "要求：body_zh 200-800 字；tags 输出 3-5 个。\n"
            f"发布者：{display_name}（@{tweet.handle}）\n"
            f"发布日期：{published_dt.month}月{published_dt.day}日\n"
            f"原文链接：{tweet.url}\n"
            f"原文内容：{tweet.content}\n"
        )

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
