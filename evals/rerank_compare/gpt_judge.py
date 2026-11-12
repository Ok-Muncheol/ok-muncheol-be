"""D: gpt-4o-mini 판단 점수기(② DecisionRAG + GPT, ④에도 같은 공통 함수).

입력·인자는 common.gpt_request(temperature 0, max_tokens 1, logprobs·top_logprobs), 점수는
common.yes_probability(첫 답 토큰 top_logprobs의 "Yes" 확률, No로 나누지 않음).
글은 하나씩 따로 요청한다(①과 같은 조건). 동시 요청 수만 CONCURRENCY로 제한한다
(속도용, 판단은 서로 독립).
키는 OpenAI 클라이언트가 환경 변수(.env의 OPENAI_API_KEY)에서 읽는다. 키는 출력·저장하지 않는다.
API 오류는 감싸지 않고 멈춘다(점수 0으로 메우지 않는다). 오류 메시지에는 예외 종류만 남긴다.
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path

from evals.rerank_compare.common import GPT_MODEL, gpt_request, yes_probability

ROOT = Path(__file__).resolve().parents[2]
CONCURRENCY = 16
# 2026-11-11 developers.openai.com/api/docs/models/gpt-4o-mini 확인(100만 토큰당 달러)
PRICE_PER_MILLION = {"input": 0.15, "cached_input": 0.075, "output": 0.60}


class GptJudgeError(RuntimeError):
    """API 요청 실패나 logprobs 없는 응답. 원본 예외는 연결하지 않는다(키가 섞일 수 있음)."""


def cost_usd(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    price = PRICE_PER_MILLION
    fresh = input_tokens - cached_tokens
    total = fresh * price["input"] + cached_tokens * price["cached_input"]
    return (total + output_tokens * price["output"]) / 1_000_000


class GptJudge:
    model_name = GPT_MODEL

    def __init__(self, *, client=None, concurrency: int = CONCURRENCY) -> None:
        if client is None:
            from dotenv import load_dotenv
            from openai import AsyncOpenAI

            load_dotenv(ROOT / ".env", override=False)
            client = AsyncOpenAI(timeout=60, max_retries=4)
        self.client = client
        self.concurrency = concurrency
        # 실행 전체 누계(비용 계산용)
        self.requests = 0
        self.input_tokens = 0
        self.cached_tokens = 0
        self.output_tokens = 0

    def usage(self) -> dict:
        """결과 JSON params.usage에 남길 실제 사용량과 비용."""
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": cost_usd(self.input_tokens, self.cached_tokens, self.output_tokens),
        }

    def describe(self) -> dict:
        return {
            "model": GPT_MODEL,
            "request": {k: v for k, v in gpt_request("", "").items() if k != "messages"},
            "concurrency": self.concurrency,
            "score": "yes_probability(top_logprobs) — 'Yes' 확률, No로 정규화하지 않음",
            "price_per_million_usd": PRICE_PER_MILLION,
            "price_checked": "2026-11-11 developers.openai.com gpt-4o-mini 모델 페이지",
        }

    async def _one(self, gate: asyncio.Semaphore, question: str, text: str):
        async with gate:
            failure = None
            try:
                response = await self.client.chat.completions.create(**gpt_request(question, text))
            except Exception as exc:  # 메시지에 키 일부가 섞일 수 있어 종류만 남긴다
                failure = type(exc).__name__
            if failure:
                raise GptJudgeError(f"GPT 판단 요청 실패: {failure}")
        choice = response.choices[0]
        content = getattr(choice.logprobs, "content", None) if choice.logprobs else None
        if not content:
            raise GptJudgeError("logprobs가 없는 응답")
        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        return yes_probability(content[0].top_logprobs), usage.prompt_tokens, cached, (
            usage.completion_tokens
        )

    async def score(
        self, question: str, texts: Sequence[str]
    ) -> tuple[list[float], int, int]:
        gate = asyncio.Semaphore(self.concurrency)
        results = await asyncio.gather(*(self._one(gate, question, t) for t in texts))
        input_tokens = sum(r[1] for r in results)
        output_tokens = sum(r[3] for r in results)
        self.requests += len(results)
        self.input_tokens += input_tokens
        self.cached_tokens += sum(r[2] for r in results)
        self.output_tokens += output_tokens
        return [r[0] for r in results], input_tokens, output_tokens
