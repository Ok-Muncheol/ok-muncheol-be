"""D: Jev(TypeSafe System One) 공유 문맥 판단 점수기 — ⑤ DecisionRAG + Jev, ⑥ RRF 50 → Jev.

①~④와 같은 점수기 인터페이스 score(질문, 글 목록) -> (점수, 입력 토큰, 출력 토큰)이다.
다른 점은 글을 하나씩 따로 묻지 않고 **한 단계(또는 후보 묶음)의 글을 state 하나에 함께** 넣고
후보마다 noul(예/아니오) 질문을 하나씩 붙이는 것이다(공유 문맥, 논문 방식).

    state     = {"question": 질문, "candidates": {"c000": 글, "c001": 글, ...}}
    questions = {"c000": {"type": "noul",
                          "instructions": {"task": RELEVANCE_INSTRUCTION,
                                           "candidate": "c000"}}, ...}
    점수       = answers["c000"].noul (P(yes), 0~1)

요청 크기: 공식 문서(docs.typesafe.ai/models, 2026-11-12) 한도는 질문당 32k 토큰(state + 질문 하나),
요청당 64k 토큰(state + 질문 전부). 긴 문맥 모드는 TypeSafe-Danger-Zone-Long-Context 헤더를
보낼 때만 켜지고("느리고 덜 정확") **이 코드는 그 헤더를 보내지 않는다**.
state가 Jev 토큰 24k(질문당 한도의 75%)를 넘으면 후보를 번갈아 배정(후보 i → i mod
요청 수)으로 나눠 보낸다. Jev 토큰 수는 공개 토큰화가 없어 o200k 토큰 × JEV_PER_O200K
(평가 문항이 아닌 확인 요청 1회로 잰 값)로 어림한다.
이 기준(24k, 비율)은 평가 결과를 보기 전에 정했다.

키는 .env의 TYPESAFE_API_KEY(요청 헤더에만 쓰고 출력·저장하지 않는다). API 오류는 점수 0으로
메우지 않고 멈춘다. 누적 비용이 max_cost_usd(기본 JEV_MAX_COST_USD 환경 변수 또는 3달러)를 넘을
것 같으면 보내기 전에, 넘으면 받은 직후에 멈춘다.
"""

import asyncio
import json
import math
import os
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from evals.rerank_compare.common import RELEVANCE_INSTRUCTION, TokenEncoder, token_encoder

ROOT = Path(__file__).resolve().parents[2]
API_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"  # 논문 버전. 11/12 확인 요청에서 받아짐(응답 model도 jev-1.13.0)
# 2026-11-12 docs.typesafe.ai/models: 입력 100만 토큰당 $0.042, 출력 무료
PRICE_PER_MILLION_INPUT = 0.042
PER_QUESTION_LIMIT = 32_000  # Jev 토큰(state + 질문 하나)
PER_REQUEST_LIMIT = 64_000  # Jev 토큰(state + 질문 전부)
JEV_TOKEN_BUDGET = 24_000  # 한 요청 state 상한(질문당 한도의 75%)
# 11/12 확인 요청 1회(가짜 질문 + 약관 글 3개, 질문 1개): Jev 2,378 / o200k 1,684
JEV_PER_O200K = 1.412
STATE_BUDGET_O200K = int(JEV_TOKEN_BUDGET / JEV_PER_O200K)  # 16,997
DEFAULT_MAX_COST_USD = 3.0
CONCURRENCY = 4  # 한 단계를 나눈 요청을 동시에 몇 개
MAX_RETRIES = 4  # 429·5xx·연결 오류
TIMEOUT_SECONDS = 120


class JevJudgeError(RuntimeError):
    """API 요청 실패나 형식이 다른 응답. 메시지에 키·요청 글을 넣지 않는다."""


class JevBudgetExceeded(JevJudgeError):
    """누적 비용 상한(max_cost_usd)을 넘는다."""


def cost_usd(input_tokens: int) -> float:
    return input_tokens * PRICE_PER_MILLION_INPUT / 1_000_000


def candidate_ids(n: int) -> list[str]:
    return [f"c{i:03d}" for i in range(n)]


def build_state(question: str, texts: Sequence[str]) -> dict:
    ids = candidate_ids(len(texts))
    return {"question": question, "candidates": dict(zip(ids, texts, strict=True))}


def build_questions(ids: Sequence[str]) -> dict:
    return {
        cid: {"type": "noul", "instructions": {"task": RELEVANCE_INSTRUCTION, "candidate": cid}}
        for cid in ids
    }


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def split_round_robin(
    question: str,
    texts: Sequence[str],
    *,
    budget_o200k: int = STATE_BUDGET_O200K,
    encoder: TokenEncoder | None = None,
) -> list[list[int]]:
    """후보 위치를 요청 묶음으로. 모든 묶음의 state(o200k)가 budget 이하가 되는 가장 적은 수로
    번갈아 배정한다(후보 i → i mod k). 글 하나가 혼자서도 넘으면 멈춘다."""
    enc = encoder or token_encoder()
    n = len(texts)
    if n == 0:
        return []

    def size(indices: Sequence[int]) -> int:
        return len(enc.encode(_json(build_state(question, [texts[i] for i in indices]))))

    k = max(1, math.ceil(size(range(n)) / budget_o200k))
    while k <= n:
        batches = [list(range(start, n, k)) for start in range(k)]
        if all(size(b) <= budget_o200k for b in batches):
            return batches
        k += 1
    raise JevJudgeError(f"글 하나가 요청 상한({budget_o200k} o200k)을 넘습니다")


class JevJudge:
    model_name = JEV_MODEL

    def __init__(
        self,
        *,
        model: str = JEV_MODEL,
        transport=None,
        api_key: str | None = None,
        max_cost_usd: float | None = None,
        budget_o200k: int = STATE_BUDGET_O200K,
        concurrency: int = CONCURRENCY,
        retry_wait_cap: float = 60.0,
        encoder: TokenEncoder | None = None,
    ) -> None:
        import httpx

        if api_key is None:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env", override=False)
            api_key = os.environ.get("TYPESAFE_API_KEY") or ""
        if not api_key:
            raise JevJudgeError("TYPESAFE_API_KEY가 없습니다(.env)")
        if max_cost_usd is None:
            max_cost_usd = float(os.environ.get("JEV_MAX_COST_USD") or DEFAULT_MAX_COST_USD)
        self.model = model
        self.model_name = model
        self.max_cost_usd = max_cost_usd
        self.budget_o200k = budget_o200k
        self.concurrency = concurrency
        self.retry_wait_cap = retry_wait_cap
        self._encoder = encoder
        self._httpx = httpx
        # 키는 클라이언트 헤더에만 둔다(이 객체의 다른 속성·describe·오류에 넣지 않는다)
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )
        # 실행 전체 누계
        self.requests = 0
        self.retries = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        self._reserved = 0.0  # 보낸 뒤 아직 응답이 오지 않은 요청의 어림 비용
        self.models: Counter = Counter()  # 응답 model(실제로 답한 모델)
        self.score_calls = 0
        self.split_calls = 0  # 두 요청 이상으로 나눈 단계 수
        self.requests_per_call: Counter = Counter()
        self.batch_sizes: Counter = Counter()
        self.max_state_bytes = 0
        self.max_state_o200k = 0
        self.max_request_input_tokens = 0
        self.max_estimated_jev_tokens = 0

    @property
    def encoder(self) -> TokenEncoder:
        return self._encoder or token_encoder()

    def usage(self) -> dict:
        """결과 JSON params.usage에 남길 실제 사용량·비용·요청 크기."""
        return {
            "requests": self.requests,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost,
            "max_cost_usd": self.max_cost_usd,
            "response_models": dict(self.models),
            "long_context": False,
            "request_size": {
                "max_state_bytes_utf8": self.max_state_bytes,
                "max_state_o200k": self.max_state_o200k,
                "max_state_estimated_jev_tokens": self.max_estimated_jev_tokens,
                "max_request_input_tokens_reported": self.max_request_input_tokens,
                "per_question_limit": PER_QUESTION_LIMIT,
            },
            "stages_scored": self.score_calls,
            "stages_split": self.split_calls,
            "requests_per_stage": {str(k): v for k, v in sorted(self.requests_per_call.items())},
            "batch_sizes": {str(k): v for k, v in sorted(self.batch_sizes.items())},
        }

    def describe(self) -> dict:
        return {
            "model": self.model,
            "endpoint": API_URL,
            "state": '{"question": 질문, "candidates": {"c000": 글, ...}}',
            "question": '{"type": "noul", "instructions": {"task": RELEVANCE_INSTRUCTION, '
            '"candidate": "c000"}} — 후보마다 하나',
            "score": "answers[후보].noul = P(yes), 정규화·가공 없음",
            "shared_context": "한 단계(또는 후보 묶음)의 글을 state 하나에 함께",
            "long_context": False,
            "split": {
                "rule": "state가 상한을 넘으면 번갈아 배정(후보 i → i mod k), 모든 묶음이 상한 "
                "이하가 되는 가장 작은 k",
                "jev_token_budget": JEV_TOKEN_BUDGET,
                "jev_per_o200k": JEV_PER_O200K,
                "state_budget_o200k": self.budget_o200k,
                "limits_jev_tokens": {
                    "per_question": PER_QUESTION_LIMIT,
                    "per_request": PER_REQUEST_LIMIT,
                },
                "fixed_before_eval": True,
                "ratio_measured": "2026-11-12 확인 요청 1회(평가 문항 아님): "
                "Jev 2,378 / o200k 1,684",
            },
            "concurrency": self.concurrency,
            "max_retries": MAX_RETRIES,
            "price_per_million_input_usd": PRICE_PER_MILLION_INPUT,
            "price_checked": "2026-11-12 docs.typesafe.ai/models (출력 토큰 무료)",
        }

    async def _post(self, body: dict) -> dict:
        httpx = self._httpx
        for attempt in range(MAX_RETRIES + 1):
            failure, wait = None, None
            try:
                response = await self._client.post(API_URL, json=body)
            except httpx.TransportError as exc:  # 메시지에 요청 내용이 섞일 수 있어 종류만
                failure = type(exc).__name__
                response = None
            if response is not None:
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 429 or response.status_code >= 500:
                    failure = f"HTTP {response.status_code}"
                    try:
                        wait = float(response.headers.get("retry-after", ""))
                    except ValueError:
                        wait = None
                else:
                    raise JevJudgeError(f"Jev 요청 실패: {_error_summary(response)}")
            if attempt == MAX_RETRIES:
                break
            self.retries += 1
            await asyncio.sleep(min(self.retry_wait_cap, wait if wait is not None else 2**attempt))
        raise JevJudgeError(f"Jev 요청 실패(재시도 {MAX_RETRIES}회 뒤): {failure}")

    async def _one(
        self, gate: asyncio.Semaphore, question: str, texts: Sequence[str]
    ) -> tuple[list[float], int, int]:
        state = build_state(question, texts)
        ids = list(state["candidates"])
        body = {"model": self.model, "state": state, "questions": build_questions(ids)}
        state_json = _json(state)
        state_o200k = len(self.encoder.encode(state_json))
        estimated = math.ceil(len(self.encoder.encode(_json(body))) * JEV_PER_O200K)
        self.max_state_bytes = max(self.max_state_bytes, len(state_json.encode("utf-8")))
        self.max_state_o200k = max(self.max_state_o200k, state_o200k)
        self.max_estimated_jev_tokens = max(
            self.max_estimated_jev_tokens, math.ceil(state_o200k * JEV_PER_O200K)
        )
        async with gate:
            estimate = cost_usd(estimated)
            if self.cost + self._reserved + estimate > self.max_cost_usd:
                raise JevBudgetExceeded(
                    f"비용 상한 ${self.max_cost_usd:.2f}: 누적 ${self.cost:.4f} + 이 요청 어림 "
                    f"${estimate:.4f}"
                )
            self._reserved += estimate
            try:
                data = await self._post(body)
            finally:
                self._reserved -= estimate
        try:
            answers = data["answers"]
            scores = []
            for cid in ids:
                answer = answers[cid]
                value = float(answer["noul"])
                if answer.get("type") != "noul" or not 0.0 <= value <= 1.0:
                    raise ValueError
                scores.append(value)
            usage = data["usage"]
            input_tokens, output_tokens = int(usage["input_tokens"]), int(usage["output_tokens"])
            model = str(data["model"])
        except (KeyError, TypeError, ValueError):
            raise JevJudgeError("형식이 다른 Jev 응답(answers·usage·model)") from None
        self.requests += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost += cost_usd(input_tokens)
        self.models[model] += 1
        self.batch_sizes[len(texts)] += 1
        self.max_request_input_tokens = max(self.max_request_input_tokens, input_tokens)
        if self.cost > self.max_cost_usd:
            raise JevBudgetExceeded(
                f"비용 상한 ${self.max_cost_usd:.2f}을 넘었습니다: 누적 ${self.cost:.4f}"
            )
        return scores, input_tokens, output_tokens

    async def score(self, question: str, texts: Sequence[str]) -> tuple[list[float], int, int]:
        batches = split_round_robin(
            question, texts, budget_o200k=self.budget_o200k, encoder=self.encoder
        )
        self.score_calls += 1
        self.requests_per_call[len(batches)] += 1
        if len(batches) > 1:
            self.split_calls += 1
        gate = asyncio.Semaphore(self.concurrency)
        results = await asyncio.gather(
            *(self._one(gate, question, [texts[i] for i in batch]) for batch in batches)
        )
        scores = [0.0] * len(texts)
        for batch, (values, _, _) in zip(batches, results, strict=True):
            for i, value in zip(batch, values, strict=True):
                scores[i] = value
        return scores, sum(r[1] for r in results), sum(r[2] for r in results)

    async def aclose(self) -> None:
        await self._client.aclose()


def _error_summary(response) -> str:
    """상태 코드 + 오류 종류. 422의 input 미리보기(요청 글)는 버린다."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(detail, list):
        kinds = sorted({str(d.get("type")) for d in detail if isinstance(d, dict)})
        places = sorted(
            {".".join(map(str, d.get("loc", [])[:3])) for d in detail if isinstance(d, dict)}
        )
        return f"HTTP {response.status_code} {kinds} {places[:5]}"
    if isinstance(detail, str):
        return f"HTTP {response.status_code} {detail[:200]}"
    return f"HTTP {response.status_code}"


def build_rerank(search):
    """⑥ run.py --retriever evals.rerank_compare.jev_judge:build_rerank.

    ③④와 같은 후보(RRF 고유 parent 50개의 모든 청크)·passage_text·MaxP, 점수기만 Jev(공유 문맥).
    """
    from evals.rerank_compare.current_rag_rerank import CurrentRagReranker

    judge = JevJudge()
    retriever = CurrentRagReranker(search, judge)
    retriever.params |= {
        "scorer": judge.describe(),
        "calls_counted": "per chunk(판단 하나당 1회, 실제 요청 수는 usage.requests)",
    }
    retriever.after_run = lambda: {"usage": judge.usage()}  # run.py가 실행 뒤 params에 더한다
    return retriever
