from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import timezone

from openai import AsyncOpenAI

from scraper.models import RawTweet

logger = logging.getLogger(__name__)

_OR_BASE_URL = "https://openrouter.ai/api/v1"
_ENRICHMENT_MODEL = "x-ai/grok-4-fast"

# 引用标记清理：[[1]](url) 或 [1] 格式
_CITATION_RE = re.compile(r"\[\[?\d+\]?\]\([^)]+\)|\[\d+\]")

# 常见账号的中文显示名（用于查询，帮助 xAI 定位人物）
_HANDLE_DISPLAY: dict[str, str] = {
    "elonmusk": "Elon Musk（马斯克）",
    "realdonaldtrump": "Donald Trump（特朗普）",
    "sama": "Sam Altman",
    "karpathy": "Andrej Karpathy",
    "claudeai": "Claude AI 官方",
    "anthropicai": "Anthropic 官方",
    "openai": "OpenAI 官方",
    "jack": "Jack Dorsey",
}

_QUERY_TEMPLATE = """\
你是一位调查记者。你的工作方式是：从公开信息中找到别人不容易注意到的事实和关联——\
不是写百科词条，而是像深度报道记者那样挖掘具体的、有时间线的、可验证的事实。

你的任务：为以下推文做背景调研。\
调研结果将提供给一位中文内容作者，帮助他写出有充分事实根据的文章。\
作者的读者是中国小红书上对全球科技和时政感兴趣的普通人。\
你的调研会成为作者的"已知背景"——他不会直接引用，\
但需要这些事实来做出自己的判断和解读。

---

推文信息：
发布者：{display_name}（@{handle}）
发布日期：{year}年{month}月{day}日
推文内容：
{tweet_content}

---

请通过网络搜索，就以下三个维度提供背景事实。

**1. 发布者近期动态（author_recent）**

这个人最近 1-3 个月在做什么？具体的行动、公开表态、争议事件、角色变动。

质量标准——
好的调研："马斯克于3月12日宣布将 xAI 与 Tesla 的 AI 部门合并，引发股东诉讼。"
差的调研："马斯克是 Tesla 和 SpaceX 的 CEO，也是 xAI 的创始人。"
前者是调研，后者是常识。请只提供前者。

**2. 相关事件脉络（event_context）**

与这条推文主题直接相关的事件：起因、关键节点、当前进展。\
按时间顺序整理，每个事件附上具体日期（X月X日，不要说"最近"或"近期"）。\
这是最重要的维度——如果只能写好一个，写好这个。

**3. 值得注意的关联（notable_connections）**

你在调研过程中发现的、从推文本身不容易看出来的事实关联。比如：
- 时间巧合："这条推文发布于 OpenAI 宣布新一轮融资的同一天"
- 前后矛盾："发布者在1月的国会听证会上对同一话题持相反立场"
- 利害关系："发布者的公司在该领域与推文中提到的机构存在直接竞争"

只报告你能通过搜索确认的事实关联。\
不要做主观推测（如"他可能是想转移注意力"、"这暗示了XX"）——\
那是作者的工作，不是你的。\
如果没有发现值得注意的关联，返回空字符串，不要编造。

---

调研要求：
- 优先搜索最近 30 天的信息；需要时回溯至 3-6 个月
- 优先一手信源：官方声明、直接引言、监管文件，而非二手评论文章
- 给出具体日期（X月X日），不要用"最近""近期""日前"等模糊时间词
- 三个维度合计 500-1000 字，按各维度的信息量自行分配——\
信息丰富的维度多写，信息稀少的少写，不要为了凑字数重复或泛化
- 某个维度找不到实质性新信息时，返回空字符串 ""，不要用常识填充

以 JSON 格式返回，不要用列表，不要加引用标记（如[1][2]），\
不要加任何前缀或后缀：

{{
  "author_recent": "发布者近期动态的段落",
  "event_context": "相关事件脉络的段落",
  "notable_connections": "值得注意的关联的段落"
}}

只返回 JSON。\
"""


@dataclass(frozen=True, slots=True)
class ResearchBrief:
    """xAI 调研结果，包含三个维度的背景事实。"""

    author_recent: str
    event_context: str
    notable_connections: str

    def to_prompt_section(self) -> str:
        """渲染为可直接嵌入 Claude user prompt 的背景段落。

        设计要点：
        - header 用"你已了解到"框架，让 Claude 视为自己的已有知识
        - 字段顺序：事件脉络（最具体）→ 人物动态 → 关联线索（供 Claude 发展为分析）
        - 标签指向用法而非描述性
        """
        parts: list[str] = [
            "你已了解到以下与这条推文相关的背景：",
        ]
        if self.event_context:
            parts += ["", f"相关事件脉络：{self.event_context}"]
        if self.author_recent:
            parts += ["", f"发布者近期动态：{self.author_recent}"]
        if self.notable_connections:
            parts += ["", f"值得注意的关联：{self.notable_connections}"]
        return "\n".join(parts)

    def is_empty(self) -> bool:
        return not any([
            self.author_recent,
            self.event_context,
            self.notable_connections,
        ])


class ContextEnricher:
    """调用 xAI（via OpenRouter web 插件）为推文生成实时背景调研简报。

    在 translate() 内部调用，仅对通过评分阈值的推文触发。
    失败时返回 None，不阻断翻译主流程。
    """

    __slots__ = ("_client", "_model")

    def __init__(
        self,
        openrouter_api_key: str,
        model: str = _ENRICHMENT_MODEL,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=openrouter_api_key,
            base_url=_OR_BASE_URL,
            timeout=45.0,
        )
        self._model = model

    async def fetch(self, tweet: RawTweet) -> ResearchBrief | None:
        """返回结构化背景简报，失败时静默返回 None。"""
        try:
            brief = await self._query(tweet)
            if brief is None or brief.is_empty():
                return None
            return brief
        except Exception as exc:
            logger.warning("context enrichment failed for @%s: %s", tweet.handle, exc)
            return None

    async def _query(self, tweet: RawTweet) -> ResearchBrief | None:
        query = self._build_query(tweet)
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": query}],
            max_tokens=2500,
            extra_body={"plugins": [{"id": "web"}]},
        )
        text = (response.choices[0].message.content or "").strip()
        text = _CITATION_RE.sub("", text).strip()
        return self._parse(text)

    def _build_query(self, tweet: RawTweet) -> str:
        published_dt = tweet.published_at.astimezone(timezone.utc)
        display_name = _HANDLE_DISPLAY.get(tweet.handle.lower(), f"@{tweet.handle}")
        return _QUERY_TEMPLATE.format(
            display_name=display_name,
            handle=tweet.handle,
            year=published_dt.year,
            month=published_dt.month,
            day=published_dt.day,
            tweet_content=tweet.content[:800],
        )

    def _parse(self, text: str) -> ResearchBrief | None:
        """从模型输出中提取 JSON，解析为 ResearchBrief。失败返回 None。"""
        try:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                logger.debug("context enricher: no JSON found in response (len=%d)", len(text))
                return None
            data = json.loads(match.group())
            return ResearchBrief(
                author_recent=str(data.get("author_recent", "")).strip(),
                event_context=str(data.get("event_context", "")).strip(),
                notable_connections=str(data.get("notable_connections", "")).strip(),
            )
        except Exception as exc:
            logger.debug("context enricher: parse failed: %s | text[:200]=%r", exc, text[:200])
            return None
