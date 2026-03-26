"""推文评分器 — 调用廉价 LLM 判断推文是否值得写成帖子。"""
from __future__ import annotations

import json
import logging
import re

from openai import AsyncOpenAI

from config import FilterConfig
from processor.prompts import build_scorer_prompt

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class TweetScorer:
    def __init__(self, api_key: str, config: FilterConfig) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
        )
        self._config = config

    async def score(
        self,
        handle: str,
        content: str,
        feedback_lines: list[str] | None = None,
    ) -> tuple[int, str]:
        """评分一条推文，返回 (score, reason)。"""
        prompt = build_scorer_prompt(handle, content, feedback_lines)
        try:
            response = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=256,
            )
            text = (response.choices[0].message.content or "").strip()
            # Strip <think> blocks from reasoning models
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return self._parse_score(text)
        except Exception as exc:
            logger.warning("评分失败 @%s: %s", handle, exc)
            return 0, f"评分失败: {exc}"

    @staticmethod
    def _parse_score(text: str) -> tuple[int, str]:
        """从 LLM 返回的 JSON 中提取 score 和 reason。"""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"评分返回中未找到 JSON: {text[:200]}")
        data = json.loads(match.group())
        score = int(data.get("score", 0))
        reason = str(data.get("reason", ""))
        if not 1 <= score <= 10:
            raise ValueError(f"评分超出范围 1-10: {score}")
        return score, reason
