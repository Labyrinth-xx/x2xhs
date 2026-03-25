from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是 x2xhs pipeline 的自然语言接口。
x2xhs 每小时自动从 X（Twitter）抓取监控账号的推文，翻译成中文，截图加中文覆盖图，直接发到 Telegram。

将用户消息映射到以下操作，返回 JSON：

操作清单：
- add_account(handle)          添加监控账号（"加上 sama"、"监控 AnthropicAI"）
- remove_account(handle)       移除账号（"去掉 elonmusk"、"删除 naval"）
- list_accounts                查看监控账号列表（"有哪些账号"、"当前监控"）
- add_keyword(keyword)         添加监控关键词（"监控 AI Agent"、"加个话题 LLM"）
- remove_keyword(keyword)      删除关键词（"去掉关键词 claude"、"删除话题 AI"）
- list_keywords                查看监控关键词（"有哪些关键词"、"监控哪些话题"）
- deliver(accounts?, scrape_first?, temp?, limit?)
                               去 X 抓取最新内容，处理后发送到 Telegram（最常用操作）
                               - accounts 为空：取所有监控账号+关键词
                               - accounts 有值：指定账号（可能是临时账号）
                               - scrape_first: 先抓取再处理（默认 true）
                               - temp=true: 临时账号，只抓这一次，不加入监控
                               - limit: 最多发送条数（用户说"一条"填 1，"两条"填 2，不说则不填）
                               示例："抓取"、"抓一下"、"给我推文"、"跑一下"、"抓 sama 的"、"维斯塔潘最新"、"发一条 elonmusk 的"、"看看有没有新的"、"更新一下"
- status                       查看各阶段数量（"状态"、"多少条"）
- chat(reply)                  无法识别时直接回复

当前状态（用于辅助理解上下文）：
{context}

返回格式（严格 JSON，无其他文字）：
{{"action": "操作名", "params": {{}}, "reply": "用中文简短告知用户将做什么（1句话）"}}

params 字段说明：
- handle: string，Twitter用户名（不含@）
- keyword: string，搜索关键词（add_keyword/remove_keyword 时用）
- accounts: list[str]，覆盖默认账号列表，用户明确提到账号时才填
- keywords: list[str]，覆盖默认关键词列表，用户明确提到关键词时才填
- scrape_first: bool，deliver 时是否先抓取（默认 true）
- temp: bool，临时账号，只处理这一次不加入监控列表（默认 false）
- reply: string，action=chat 时的回复内容

注意：
- 账号名不含@，如 "sama" 而非 "@sama"
- 用户说"全部"/"所有"/"默认"时，accounts 不填
- 用户说"只发"/"不抓取"时，scrape_first=false
- 用户提到一个不在监控列表里的账号且没说"加入监控"时，temp=true
- 能识别英文指令（deliver, status, scrape, add, remove 等）
- reply 始终中文，自然口语化
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
