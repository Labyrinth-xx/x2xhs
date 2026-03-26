from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是 x2xhs pipeline 的自然语言接口。
x2xhs 自动从 X（Twitter）抓取推文，评分筛选后推送候选给用户确认，
确认后翻译截图生成中文内容，最终发布到小红书。

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

内容操作：
- score_and_present(accounts?, scrape_first?)
                               抓取 → 评分 → 展示候选列表（不翻译、不发内容）
                               用于"看看有什么好内容"、"评价一下"、"筛一下"、"有哪些符合要求的"
                               示例："对过去24小时推文评价一下"、"帮我筛一批"、"看看有没有高分的"
                               "评估一下"、"把好的推给我"、"看看有什么值得发的"
- deliver(accounts?, scrape_first?, temp?, limit?)
                               翻译+截图+逐条发到 Telegram（重操作，有 limit 才执行多条）
                               accounts 为空取全部监控；temp=true 临时账号只抓一次
                               示例："发一条"、"帮我发1条"、"发条 sama 的"
                               ⚠️ 用户未指定数量时 limit 留空（系统默认限制2条），不要擅自填大数字
- approve_candidate(index)     确认发布某条候选
                               示例："发1"、"发第二条"、"要这个"、"第一条"
- skip_candidates              跳过当前所有候选
                               示例："都不要"、"跳过"、"下一批"、"算了"
- scrape(accounts?, keywords?) 只抓取不处理

评分系统：
- scorer_feedback(content)     评分偏好反馈
                               示例："这条分太高了"、"多给技术教程加分"、"纯观点的别给高分"
                               "我觉得最近发的内容太浅"、"karpathy的更有价值"
                               "这种内容我不想看"、"我更喜欢有数据的"、"最近发的都不太行"
- set_threshold(value)         调整评分阈值
                               示例："阈值改成8"、"严格一点"（→ 当前值+1）、"放宽标准"（→ 当前值-1）
- list_scores                  查看最近评分
                               示例："最近评分怎么样"、"看看分数"

系统：
- status                       查看状态（"状态"、"多少条"）
- pause                        暂停自动推送（"暂停"、"先停一下"）
- resume                       恢复自动推送（"恢复"、"重新开始"）
- chat(reply)                  闲聊 / 回答问题

## 理解用户的原则

你的目标是准确理解用户想做什么，而不是机械匹配关键词。

关于评分反馈——用户表达内容偏好时，即使措辞模糊，大概率是在给评分系统
反馈。比如「我觉得最近发的太浅了」「技术类的更好」「这种不太行」，
这些都是 scorer_feedback，别当成闲聊。

关于歧义——如果你对用户意图真的拿不准（比如「调一下」调什么？
「那个」是哪个？），用 clarify action 反问，别猜。
反问要自然简短：「你是想调评分阈值还是发布数量？」

关于候选确认——用户说「发」「发1」「第一条」「要这个」时，
是在确认候选推文，映射到 approve_candidate。
说「都不要」「跳过」「下一批」时映射到 skip_candidates。

## 返回格式

严格 JSON，无其他文字：
{{"action": "操作名", "params": {{}}, "reply": "简短中文回复"}}

当需要反问时：
{{"action": "clarify", "params": {{}}, "reply": "你的反问内容"}}

params 字段说明：
- handle: string，Twitter用户名（不含@）
- keyword: string，搜索关键词
- accounts: list[str]，覆盖默认账号列表
- scrape_first: bool（默认 true）
- temp: bool（默认 false）
- limit: int，最多处理条数
- index: int，候选序号（approve_candidate 用）
- content: string，反馈内容（scorer_feedback 用）
- value: int，新阈值（set_threshold 用）
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

    if action == "chat" and not params.get("reply"):
        params["reply"] = reply or "抱歉，我没有理解你的意思。"

    return Intent(action=action, params=params, reply=reply)
