"""关键词查询定义与管理。

KeywordQuery 描述一条 Twitter 高级搜索查询，包含类别、查询模板、互动量门槛。
DEFAULT_QUERIES 的具体查询内容由 Opus 填写，此处为占位骨架。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storage.tweet_repo import TweetRepository

# 支持的关键词类别
CATEGORIES = (
    "model_release",  # 模型发布
    "ai_app",         # AI 应用
    "infra",          # 基础设施（芯片/算力/云）
    "industry",       # 行业动态（融资/并购/战略）
    "regulation",     # 监管政策
)


@dataclass(frozen=True, slots=True)
class KeywordQuery:
    """单条关键词搜索查询配置。

    query_template 是 Twitter 高级搜索语法，fetch 时会自动附加
    `-filter:retweets lang:en` 和 `min_faves:{min_faves}`。
    """

    category: str           # 所属类别，见 CATEGORIES
    query_template: str     # Twitter 高级搜索查询字符串
    min_faves: int          # 互动量最低门槛（粗筛噪音）
    priority: int           # 1=高优先级, 2=普通
    id: int | None = None   # DB id，None 表示来自默认列表
    enabled: bool = True


def build_twitter_query(kq: KeywordQuery) -> str:
    """拼接完整的 Twitter 搜索查询字符串（含互动量门槛和过滤器）。"""
    return (
        f"{kq.query_template} "
        f"min_faves:{kq.min_faves} "
        f"-filter:retweets lang:en"
    )


# ── 默认查询列表 ──
# 5 类别 × 2 条 = 10 条，覆盖 AI 模型/应用/基础设施/行业动态/监管政策
DEFAULT_QUERIES: tuple[KeywordQuery, ...] = (
    # model_release — 模型发布（新模型/重大更新/开源发布）
    KeywordQuery(
        category="model_release",
        query_template='("AI model" OR LLM OR "language model") (release OR launch OR announce OR "open source" OR "open weight")',
        min_faves=300,
        priority=1,
    ),
    KeywordQuery(
        category="model_release",
        query_template='(GPT OR Claude OR Gemini OR Llama OR Mistral OR Qwen OR DeepSeek) (new OR update OR upgrade OR "now available")',
        min_faves=300,
        priority=1,
    ),
    # ai_app — AI 应用（Agent 框架/编程工具/产品发布）
    KeywordQuery(
        category="ai_app",
        query_template='("AI agent" OR "AI agents") (launch OR demo OR product OR framework OR "open source" OR building)',
        min_faves=500,
        priority=1,
    ),
    KeywordQuery(
        category="ai_app",
        query_template='("AI coding" OR Copilot OR Cursor OR "code assistant" OR "AI IDE") (update OR launch OR "new feature" OR announce)',
        min_faves=500,
        priority=2,
    ),
    # infra — 基础设施（芯片/算力/数据中心）
    KeywordQuery(
        category="infra",
        query_template='(NVIDIA OR "AI chip" OR GPU OR TPU OR "AI hardware") (announce OR launch OR earnings OR ban OR export)',
        min_faves=400,
        priority=1,
    ),
    KeywordQuery(
        category="infra",
        query_template='("data center" OR "cloud AI" OR "AI infrastructure") (build OR invest OR expand OR billion)',
        min_faves=400,
        priority=2,
    ),
    # industry — 行业动态（融资/并购/战略）
    KeywordQuery(
        category="industry",
        query_template='("AI startup" OR "AI company") (funding OR acquisition OR valuation OR IPO OR Series)',
        min_faves=500,
        priority=1,
    ),
    KeywordQuery(
        category="industry",
        query_template='(OpenAI OR Anthropic OR "Google DeepMind" OR "Meta AI" OR xAI) (CEO OR hire OR strategy OR partnership OR deal)',
        min_faves=500,
        priority=2,
    ),
    # regulation — 监管政策（AI 立法/安全/治理）
    KeywordQuery(
        category="regulation",
        query_template='("AI regulation" OR "AI safety" OR "AI policy" OR "AI act" OR "AI governance")',
        min_faves=300,
        priority=1,
    ),
    KeywordQuery(
        category="regulation",
        query_template='(AI OR "artificial intelligence") (congress OR senate OR "executive order" OR EU OR legislation OR ban)',
        min_faves=300,
        priority=2,
    ),
)


async def get_active_queries(repo: "TweetRepository") -> list[KeywordQuery]:
    """从 DB 加载已启用的查询，若 DB 为空则返回 DEFAULT_QUERIES。

    首次运行时自动将 DEFAULT_QUERIES 写入 DB。
    """
    db_rows = await repo.list_keyword_queries(enabled_only=True)
    if db_rows:
        return [
            KeywordQuery(
                id=row["id"],
                category=row["category"],
                query_template=row["query_template"],
                min_faves=row["min_faves"],
                priority=row["priority"],
                enabled=bool(row["enabled"]),
            )
            for row in db_rows
        ]

    # DB 为空：将默认列表写入并返回
    for kq in DEFAULT_QUERIES:
        await repo.add_keyword_query(
            category=kq.category,
            query_template=kq.query_template,
            min_faves=kq.min_faves,
            priority=kq.priority,
        )
    return list(DEFAULT_QUERIES)
