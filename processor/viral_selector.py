from __future__ import annotations

import logging

from openai import AsyncOpenAI

from config import ProcessorConfig
from scraper.models import RawTweet

logger = logging.getLogger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class ViralSelector:
    """用现有翻译模型（via OpenRouter）从候选推文中选出最适合发布的一条。

    判断标准：信息量、中国读者感兴趣程度、观点独特性，而非单纯热度数字。
    """

    def __init__(self, config: ProcessorConfig) -> None:
        self._config = config
        self._client = AsyncOpenAI(
            api_key=config.openrouter_api_key,
            base_url=_OPENROUTER_BASE_URL,
        )

    async def select_best_tweet(
        self, keyword: str, candidates: list[RawTweet]
    ) -> RawTweet:
        """从候选列表中选出最有价值的推文，返回该推文对象。

        如果模型解析失败，降级返回第一条候选。
        """
        if len(candidates) == 1:
            return candidates[0]

        tweets_text = "\n\n".join(
            f"[{i + 1}] @{t.handle}\n{t.content[:400]}"
            for i, t in enumerate(candidates)
        )

        prompt = (
            f"以下是关于「{keyword}」的 {len(candidates)} 条英文推文，"
            f"请判断哪一条对中国科技读者最有价值。\n\n"
            f"判断标准（按优先级）：\n"
            f"1. 信息量：有具体数据、独家观点或重要进展\n"
            f"2. 传播价值：中国读者会感兴趣、愿意转发\n"
            f"3. 观点独特性：不是重复已知信息\n\n"
            f"{tweets_text}\n\n"
            f"直接回复序号（如「3」），不需要解释。"
        )

        try:
            response = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
                temperature=0,
            )
            raw = (response.choices[0].message.content or "").strip()
            # 提取第一个数字
            import re
            m = re.search(r"\d+", raw)
            if m:
                idx = int(m.group()) - 1
                if 0 <= idx < len(candidates):
                    logger.info(
                        "ViralSelector 选择了第 %d 条（关键词: %s）", idx + 1, keyword
                    )
                    return candidates[idx]
        except Exception as exc:
            logger.warning("ViralSelector 调用失败，降级返回第一条: %s", exc)

        return candidates[0]
