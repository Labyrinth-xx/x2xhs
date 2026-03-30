"""推文评分器 — 调用廉价 LLM 判断推文是否值得写成帖子。"""
from __future__ import annotations

import json
import logging
import re

from openai import AsyncOpenAI

from config import FilterConfig
from processor.prompts import build_keyword_scorer_prompt, build_scorer_prompt

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class TweetScorer:
    def __init__(self, api_key: str, config: FilterConfig) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
        )
        self._config = config

    # 各维度权重
    _WEIGHTS = {"info_diff": 0.35, "depth": 0.25, "angle": 0.20, "viral": 0.20}

    async def score(
        self,
        handle: str,
        content: str,
        feedback_lines: list[str] | None = None,
    ) -> tuple[float, str, dict] | None:
        """评分一条推文，返回 (score, reason, detail) 或 None（失败时跳过，下次重试）。"""
        prompt = build_scorer_prompt(handle, content, feedback_lines)
        try:
            response = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=512,
            )
            text = (response.choices[0].message.content or "").strip()
            # Strip <think> blocks from reasoning models
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return self._parse_score(text)
        except Exception as exc:
            logger.warning("评分失败 @%s: %s — 下次重试", handle, exc)
            return None

    # 关键词推文 5 维度权重（与账号推文 4 维度不同）
    _KEYWORD_WEIGHTS = {
        "newsworthiness": 0.30,
        "info_diff": 0.25,
        "source_authority": 0.20,
        "depth": 0.15,
        "viral": 0.10,
    }

    async def score_keyword_tweet(
        self,
        handle: str,
        content: str,
        category: str,
    ) -> tuple[float, str, dict] | None:
        """用关键词专用 5 维度评分推文，返回 (score, reason, detail) 或 None。

        prompt 内容由 Opus 实现（build_keyword_scorer_prompt）。
        """
        prompt = build_keyword_scorer_prompt(handle, content, category)
        try:
            response = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=512,
            )
            text = (response.choices[0].message.content or "").strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return self._parse_keyword_score(text)
        except Exception as exc:
            logger.warning("关键词评分失败 @%s: %s — 跳过", handle, exc)
            return None

    @classmethod
    def _parse_keyword_score(cls, text: str) -> tuple[float, str, dict]:
        """从 LLM 返回的 JSON 中提取 5 维度分数并计算加权综合分。"""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"关键词评分返回中未找到 JSON: {text[:200]}")
        data = json.loads(match.group())
        detail: dict = {}
        for key in cls._KEYWORD_WEIGHTS:
            val = round(float(data.get(key, 0)), 1)
            if not 1 <= val <= 10:
                raise ValueError(f"维度 {key} 超出范围 1-10: {val}")
            detail[key] = val
        weighted = sum(detail[k] * w for k, w in cls._KEYWORD_WEIGHTS.items())
        score = round(weighted, 1)
        reason = str(data.get("reason", ""))
        preview_title = str(data.get("preview_title", "")).strip()
        if preview_title:
            detail["preview_title"] = preview_title
        return score, reason, detail

    @classmethod
    def _parse_score(cls, text: str) -> tuple[float, str, dict]:
        """从 LLM 返回的 JSON 中提取分维度分数并计算加权综合分。"""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"评分返回中未找到 JSON: {text[:200]}")
        data = json.loads(match.group())
        detail: dict = {}
        for key in cls._WEIGHTS:
            val = round(float(data.get(key, 0)), 1)
            if not 1 <= val <= 10:
                raise ValueError(f"维度 {key} 超出范围 1-10: {val}")
            detail[key] = val
        weighted = sum(detail[k] * w for k, w in cls._WEIGHTS.items())
        score = round(weighted, 1)
        reason = str(data.get("reason", ""))
        preview_title = str(data.get("preview_title", "")).strip()
        if preview_title:
            detail["preview_title"] = preview_title
        return score, reason, detail
