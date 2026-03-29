from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是 x2xhs pipeline 的自然语言接口。
x2xhs 三条线（账户监控/关键词搜索/趣文发现）自动运作，内容统一进入候选池。
用户从候选池中挑选内容，确认后翻译截图生成中文内容，发布到小红书。

将用户消息映射到以下操作，返回 JSON。

## 操作清单

账号管理：
- add_account(handle)          添加监控账号（"加上 sama"、"监控 AnthropicAI"）
- remove_account(handle)       移除账号（"去掉 elonmusk"）
- list_accounts                查看监控账号

关键词管理：
- add_keyword(keyword)         添加关键词（"监控 AI Agent"）
- remove_keyword(keyword)      删除关键词（"删除话题 AI"）
- list_keywords                查看关键词

候选池操作（核心）：
- view_candidates              查看候选池中的所有待审内容
                               主操作，用于"看看有什么"、"有什么好内容"、"候选池有什么"
                               示例："看看有什么值得发的"、"有什么好的"、"候选列表"
- score_and_present(accounts?) 抓取 → 评分 → 刷新候选池（比 view_candidates 多一步抓取评分）
                               示例："帮我筛一批"、"评价一下"、"抓取一下看看"
- approve_candidate(index)     确认处理候选池中的第 N 条，翻译截图后发送到 Telegram
                               用户会用自然语言描述内容（标题、有趣点、账号名、关键词），
                               你需要从当前候选列表推断对应的序号 index
                               触发词不限于"发"，"看""选""要""这个""那个"都可以触发
                               示例："发第一个"、"发 sama 的那条"、"要那条关于 AI 的"
                                     "我选第一条"、"这个有意思，发出去"、"第二条更好"
                                     "发1"、"发那个讲 claude 的"
- skip_candidate(index)        跳过候选池中的某条
                               示例："跳过第3条"、"这条不要"、"第2条不行"
- skip_candidates              跳过所有候选
                               示例："都不要"、"跳过"、"清空候选"、"算了"
- deliver                      展示候选池（等同 view_candidates，兼容旧习惯）
                               示例："发一条"、"帮我发条"、"有什么可以发的"

内容发现（结果进入候选池，不直接发布）：
- keyword_search(keyword)      关键词搜索精选，找到最优推文加入候选池
                               示例："搜一下 AI agents 的爆文"、"找找最近 xx 的热门推文"
                               "帮我找一条关于 xx 的好推文"、"xx 有什么高热度的"
- search_fun(n?)               趣文发现，用 xAI 搜索本周有趣推文加入候选池
                               n 默认 1，最多 3
                               示例："找一条有趣的推文"、"找找好玩的"、"来一条有意思的"
- digest(keyword)              话题综述（独立功能，不进候选池）
                               示例："给我写个 AI agents 的综述"、"帮我整理一下 xx 领域"
- scrape(accounts?, keywords?) 只抓取不处理

评分系统：
- scorer_feedback(content)     评分偏好反馈
                               示例："这条分太高了"、"多给技术教程加分"、"纯观点的别给高分"
                               "我觉得最近发的内容太浅"、"karpathy的更有价值"
- set_threshold(value)         调整评分阈值
                               示例："阈值改成8"、"严格一点"（→ 当前值+1）、"放宽标准"（→ 当前值-1）
- list_scores                  查看最近评分

系统：
- status                       查看状态（"状态"、"多少条"）
- pause                        暂停自动推送（"暂停"、"先停一下"）
- resume                       恢复自动推送（"恢复"、"重新开始"）
- chat(reply)                  闲聊 / 回答问题

## 理解用户的原则

你的目标是准确理解用户想做什么，而不是机械匹配关键词。

关于候选池——所有内容都通过候选池中转。用户说「发一条」「有什么好的」
都是在查看候选池。用户说「发1」「第一条」是在确认候选。
用户说「搜一下 xx 爆文」是触发关键词搜索，结果会进入候选池等用户确认。

关于评分反馈——用户表达内容偏好时，大概率是在给评分系统反馈。
比如「我觉得最近发的太浅了」「技术类的更好」，映射到 scorer_feedback。

关于歧义——拿不准时用 clarify action 反问，别猜。

## 意图确认

对于涉及搜索的 action（keyword_search、search_fun、digest、score_and_present、scrape），
根据用户语气判断是否需要二次确认：

- **needs_confirm: true**：用户是询问/描述语气
  例：「AI agents 最近怎么样」「有没有什么有趣的」
- **needs_confirm: false**：用户是明确指令语气
  例：「搜一下 AI agents 的爆文」「找个有趣的」「发1」

查看类操作（view_candidates、status、list_accounts、list_scores 等）不需要确认。
approve_candidate 和 skip 操作不需要确认（由系统二次确认）。

## 返回格式

严格 JSON，无其他文字：
{{"action": "操作名", "params": {{}}, "reply": "简短中文回复", "needs_confirm": false}}

当需要反问时：
{{"action": "clarify", "params": {{}}, "reply": "你的反问内容", "needs_confirm": false}}

params 字段说明：
- handle: string，Twitter用户名（不含@）
- keyword: string，搜索关键词
- accounts: list[str]，覆盖默认账号列表
- index: int，候选序号（approve_candidate / skip_candidate 用）
- content: string，反馈内容（scorer_feedback 用）
- value: int，新阈值（set_threshold 用）
- n: int，趣文数量（search_fun 用）
- reply: string（chat 时的回复内容）

注意：
- 账号名不含@
- 用户说"严格一点"时 set_threshold，value 为当前阈值+1
- 用户说"放宽标准"时 set_threshold，value 为当前阈值-1
- reply 始终中文，自然口语化

当前状态：
{context}
"""


@dataclass
class Intent:
    action: str
    params: dict = field(default_factory=dict)
    reply: str = ""
    needs_confirm: bool = False


_CHAT_SYSTEM = """\
你是用户的私人助手，运行在他的 x2xhs Telegram bot 里。
x2xhs 是一个把 X（Twitter）内容自动处理后推送给用户的工具，用户自己决定是否发到小红书。
你可以帮用户回答任何问题、闲聊、给建议，也可以解释 x2xhs 的功能。
用中文回复，语气自然随意，简洁为主。不要加多余的前缀或签名。
"""


async def chat_reply(
    message: str,
    openrouter_api_key: str,
    model: str,
) -> str:
    client = AsyncOpenAI(
        api_key=openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _CHAT_SYSTEM},
                {"role": "user", "content": message},
            ],
            temperature=0.7,
            max_tokens=512,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("Chat reply failed: %s", exc)
        return "抱歉，出了点问题，稍后再试。"


async def parse_intent(
    message: str,
    openrouter_api_key: str,
    model: str,
    context: str = "",
) -> Intent:
    client = AsyncOpenAI(
        api_key=openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
    )
    system = _SYSTEM_PROMPT.replace("{context}", context or "暂无信息")

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ],
            temperature=0.0,
            max_tokens=256,
        )
        raw = (response.choices[0].message.content or "").strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"no JSON in response: {raw[:200]}")
        data = json.loads(match.group())
    except Exception as exc:
        logger.warning("Intent parsing failed: %s", exc)
        return Intent(
            action="chat",
            params={"reply": "抱歉，没理解你的意思，可以换个方式说吗？"},
        )

    action = str(data.get("action", "chat"))
    params = dict(data.get("params", {}))
    reply = str(data.get("reply", ""))
    needs_confirm = bool(data.get("needs_confirm", False))

    if action == "chat" and not params.get("reply"):
        params["reply"] = reply or "抱歉，我没有理解你的意思。"

    return Intent(action=action, params=params, reply=reply, needs_confirm=needs_confirm)
