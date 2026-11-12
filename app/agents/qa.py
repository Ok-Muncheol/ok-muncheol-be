"""B: 근거 질의응답·what-if 에이전트(1차 확장). 공식 판단·보고서·확인 완료 상태는 쓰지 않는다.

아키텍처 v7 「질의응답 에이전트」: 질문마다 LLM이 도구를 고른다(최대 MAX_TOOL_CALLS회).
도구는 판단 로그·② 결정례 검색·③ 약관 검색·simulate(가정 실행)다.

- 수치는 판단 로그(공식)와 simulate(가정) 결과에만 있다.
  LLM이 비율·금액을 만들지 않는다(README 핵심 원칙 2).
- simulate는 공식 판단에 쓴 확정 카드의 칸을 가정 값으로 바꿔 같은 판단 그래프를
  메모리에서 다시 실행한다. 저장하지 않고 공식 결과를 바꾸지 않는다.
  실행 함수는 API 계층(app/api/judgments.py)이 넘겨준다.
- 근거는 도구 결과에 붙인 [E1]·[E2]… 표시로만 인용한다. 도구가 돌려준 적 없는 표시는 답에서 지운다.
  인용마다 출처(source)를 단다 — official(공식 판단 로그)·search(검색)·hypothesis(가정 실행).
- 사실이 실제로 다르면 A의 사실 카드 수정·확정 → 판단 다시 요청이 공식 경로다(fact_change_hint).
- 시간 제한은 LLM 호출에만 둔다(app/llm.py — 호출 1회 45초·재시도 1회). 질문 하나의 LLM 호출
  시간 합계는 llm_budget_seconds(120초, cure-agent-be 대화 전체 상한)이고 도구 실행 시간은 세지
  않는다. 넘으면 QaTimedOut이다. 가정 실행은 반박 검토 LLM도 부르므로 질문당 MAX_SIMULATIONS번이다.
"""

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from app import llm as llm_policy
from app.schemas.chart_scope import FIELD_OPTIONS
from app.schemas.judgment import (
    CitationSource,
    JudgmentResult,
    QaAnswer,
    QaCitation,
    QaHypothesis,
    QaToolCall,
)
from app.schemas.retrieval import EvidenceHit, SearchQuery
from app.tools.dispute_cases import search_dispute_cases
from app.tools.policy_terms import search_policy_terms

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 5
MAX_SIMULATIONS = 2  # 질문당 가정 실행. 한 번마다 판단 그래프(반박 검토 LLM 포함)를 다시 돈다
QA_LLM_BUDGET_SECONDS = 120.0  # 질문 하나의 LLM 호출 시간 합계. 도구 실행 시간은 세지 않는다
MAX_CHANGES = 4
SEARCH_TOP_K = 5
TOOL_EXCERPT_CHARS = 400  # LLM이 읽는 근거 원문 길이
CITATION_EXCERPT_CHARS = 160  # 응답 citations의 발췌 길이

FACT_CHANGE_HINT = (
    "가정 결과는 공식 판단·보고서에 반영되지 않습니다. "
    "실제 사실이 다르면 사실 카드를 고쳐 다시 확정한 뒤 판단을 다시 요청하세요."
)

SYSTEM_PROMPT = (
    "당신은 자동차보험 보상 담당자를 돕는 과실비율 검토 질의응답 도우미다. "
    "공식 판단은 고정 워크플로우가 이미 냈다. 당신은 그 판단을 근거로 설명하거나 "
    "가정(what-if) 결과를 도구로 보여 줄 뿐 공식 판단·보고서를 바꾸지 못한다.\n"
    "규칙:\n"
    "1. 사실·수치·근거는 도구 결과에서만 쓴다. 공식 판단은 get_judgment_log, "
    "결정례는 search_dispute_cases, 약관·보상 지침은 search_policy_terms로 찾는다.\n"
    "2. 과실비율 숫자는 get_judgment_log(공식) 또는 simulate(가정) 결과에 있는 값만 말한다. "
    "직접 계산하거나 추정하지 않는다.\n"
    "3. '만약 ~라면' 질문은 simulate로 칸을 바꿔 실행한다. "
    "칸 이름과 값은 simulate 설명의 목록에서만 고른다. "
    "가정 결과는 '가정 결과'라고 밝히고 공식 결과와 구분한다. "
    "수정요소는 자동 적용하지 않으므로 가정해도 바뀌지 않는다.\n"
    "4. 근거를 말할 때는 도구 결과의 ref를 [E1]처럼 문장 끝에 붙인다. "
    "도구 결과에 없는 ref를 만들지 않는다. "
    "source가 hypothesis인 근거는 가정 결과를 설명할 때만 쓴다.\n"
    "5. 도구 결과로 답할 수 없으면 모른다고 말하고 무엇을 확인해야 하는지 알려 준다. "
    "법률 판단처럼 단정하지 않는다.\n"
    "6. 한국어로 10문장 이내로 답한다."
)

_FIELD_CHOICES = "; ".join(f"{key}: {', '.join(values)}" for key, values in FIELD_OPTIONS.items())

TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "get_judgment_log",
        "description": "공식 판단 로그: 판정 상태·도표·적용판·과실비율·기본 과실·"
                       "수정요소 판정·차순위 후보·검증 3겹·판정 이유·계산 로그·"
                       "담당자가 확인한 사실·도표 근거(ref).",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "search_dispute_cases",
        "description": "② 분쟁심의 결정례 검색. 비슷한 사고의 결정 사례와 이유를 찾는다. "
                       "chart_id를 주면 그 도표에 연결된 결정례로 좁힌다.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "찾을 사실·쟁점(한국어)"},
            "chart_id": {"type": "string", "description": "좁힐 도표 ID(선택)"},
        }, "required": ["text"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "search_policy_terms",
        "description": "③ 자동차보험 약관·보상 지침 검색(담보·조항·면책·별표).",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "찾을 내용(한국어)"},
            "coverage": {"type": "string", "description": "담보 이름(선택)"},
            "edition": {"type": "string", "description": "약관 적용판(선택, 모르면 비운다)"},
        }, "required": ["text"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "simulate",
        "description": "가정 실행(what-if). 공식 판단에 쓴 확정 카드의 칸을 가정 값으로 "
                       "바꿔 같은 판단 그래프를 다시 돌린다. "
                       "저장하지 않고 공식 결과를 바꾸지 않는다. "
                       f"한 번에 1~{MAX_CHANGES}칸, 질문당 {MAX_SIMULATIONS}번까지. "
                       f"칸과 값: {_FIELD_CHOICES}",
        "parameters": {"type": "object", "properties": {
            "changes": {"type": "object", "description": "칸 이름 → 가정 값",
                        "additionalProperties": {"type": "string"}},
        }, "required": ["changes"], "additionalProperties": False},
    }},
]


class LlmToolCall(TypedDict):
    id: str
    name: str
    arguments: str  # JSON 문자열


class LlmMessage(TypedDict):
    content: str | None
    tool_calls: list[LlmToolCall]


QaLlm = Callable[[list[dict], list[dict], Literal["auto", "none"]], Awaitable[LlmMessage]]
"""(messages, tools, tool_choice) → 어시스턴트 메시지. 기본은 OpenAI, 테스트는 가짜를 넣는다."""

Searcher = Callable[[SearchQuery], Awaitable[list[EvidenceHit]]]
Simulator = Callable[[dict[str, str]], Awaitable[tuple[QaHypothesis, list[EvidenceHit]]]]


class QaNotConfigured(NotImplementedError):
    """질의응답 모델 키가 없다."""


class QaFailed(RuntimeError):
    """모델이 답을 내지 못했다."""


class QaTimedOut(RuntimeError):
    """LLM 호출이 시간 상한을 넘었다.

    질문 하나의 합계(llm_budget_seconds)를 넘었거나, 호출 1회가 재시도 뒤에도 시간 초과다.
    """


@dataclass(frozen=True)
class QaDependencies:
    """바꿔 끼우는 의존성. 기본은 OpenAI와 C 검색툴이다."""

    llm: QaLlm | None = None
    search_dispute_cases: Searcher = search_dispute_cases
    search_policy_terms: Searcher = search_policy_terms
    llm_budget_seconds: float = QA_LLM_BUDGET_SECONDS


@dataclass
class QaContext:
    """답의 기준이 되는 공식 판단(최신 완료 실행)과 그 판단에 쓴 카드. API 계층이 채운다."""

    case_id: str
    judgment_version: int
    accident_date: str
    result: JudgmentResult
    confirmed_facts: dict[str, str] = field(default_factory=dict)
    simulate: Simulator | None = None  # 카드를 못 읽었으면 None(가정 실행 불가)


def _clip(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


class _Refs:
    """도구가 돌려준 근거에 E1·E2… 표시와 출처를 붙인다. 같은 청크는 같은 표시.

    출처는 처음 돌려준 도구로 정하되, 공식 판단 로그가 돌려준 근거는 언제든 official로 올린다 —
    가정 실행이 공식과 같은 도표를 골랐으면 그 근거는 공식 근거이기도 하다.
    """

    def __init__(self) -> None:
        self.citations: dict[str, QaCitation] = {}
        self._by_chunk: dict[str, str] = {}

    def add(self, hit: object, source: CitationSource) -> dict | None:
        try:
            chunk = hit["chunk"]  # type: ignore[index]
            document = chunk["document"]
            chunk_id = str(chunk["chunk_id"])
            ref = self._by_chunk.get(chunk_id)
            if ref is None:
                ref = f"E{len(self.citations) + 1}"
                self.citations[ref] = {
                    "ref": ref, "chunk_id": chunk_id, "parent_id": str(chunk["parent_id"]),
                    "corpus": document["corpus"], "edition": str(document["edition"]),
                    "page_start": int(chunk["page_start"]), "page_end": int(chunk["page_end"]),
                    "excerpt": _clip(chunk.get("text"), CITATION_EXCERPT_CHARS),
                    "source": source,
                }
                self._by_chunk[chunk_id] = ref
            elif source == "official":
                self.citations[ref]["source"] = "official"
        except (KeyError, TypeError, ValueError):
            return None  # 모양이 어긋난 근거는 인용 대상에서 뺀다
        cite = self.citations[ref]
        return {
            "ref": ref, "source": cite["source"], "corpus": cite["corpus"],
            "edition": cite["edition"], "parent_id": cite["parent_id"],
            "pages": f"{cite['page_start']}-{cite['page_end']}",
            "text": _clip(chunk.get("text"), TOOL_EXCERPT_CHARS),
        }

    def views(self, hits: object, source: CitationSource) -> list[dict]:
        if not isinstance(hits, list):
            return []
        return [view for view in (self.add(hit, source) for hit in hits) if view is not None]


def check_changes(changes: object) -> str | None:
    """simulate 가정 값 검사. 문제가 있으면 모델에 돌려줄 이유, 없으면 None."""
    if not isinstance(changes, dict) or not changes:
        return "changes는 칸 이름 → 값 객체이고 비어 있으면 안 된다"
    if len(changes) > MAX_CHANGES:
        return f"한 번에 {MAX_CHANGES}칸까지만 바꿀 수 있다"
    for key, value in changes.items():
        options = FIELD_OPTIONS.get(key)
        if options is None:
            return f"바꿀 수 없는 칸이다: {key} (가능: {', '.join(FIELD_OPTIONS)})"
        if value not in options:
            return f"{key}의 값은 {', '.join(options)} 중 하나여야 한다: {value!r}"
    return None


def judgment_log(ctx: QaContext, refs: _Refs) -> dict:
    result = ctx.result
    return {
        "judgment_version": ctx.judgment_version, "status": result["status"],
        "chart_id": result.get("chart_id"), "edition": result.get("edition"),
        "fault_A": result.get("fault_A"), "fault_B": result.get("fault_B"),
        "base_fault_A": result.get("base_fault_A"),
        "modifiers": result.get("modifiers", []), "alternatives": result.get("alternatives", []),
        "checks": result.get("checks", []), "reasons": result.get("reasons", []),
        "calculation_log": result.get("calculation_log", []),
        "questions": [{"field_key": q.get("field_key"), "text": q.get("text")}
                      for q in result.get("questions", []) if isinstance(q, dict)],
        "confirmed_facts": ctx.confirmed_facts,
        "evidence": refs.views(result.get("evidence", []), "official"),
    }


class _Session:
    """질문 하나의 도구 실행 상태."""

    def __init__(self, ctx: QaContext, deps: QaDependencies) -> None:
        self.ctx, self.deps = ctx, deps
        self.refs = _Refs()
        self.calls: list[QaToolCall] = []
        self.hypotheticals: list[QaHypothesis] = []
        self.simulated = False
        self.simulations = 0

    async def run(self, call: LlmToolCall) -> dict:
        name = call["name"]
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = None
        if not isinstance(args, dict):
            output, ok = {"error": "도구 인자는 JSON 객체여야 한다"}, False
        elif name == "get_judgment_log":
            output, ok = judgment_log(self.ctx, self.refs), True
        elif name == "search_dispute_cases":
            output, ok = await self._search(self.deps.search_dispute_cases, args, ("chart_id",))
        elif name == "search_policy_terms":
            output, ok = await self._search(
                self.deps.search_policy_terms, args, ("coverage", "edition")
            )
        elif name == "simulate":
            output, ok = await self._simulate(args.get("changes"))
        else:
            output, ok = {"error": f"없는 도구다: {name}"}, False
        self.calls.append({"name": name, "ok": ok})
        return output

    async def _search(
        self, searcher: Searcher, args: dict, filter_keys: tuple[str, ...]
    ) -> tuple[dict, bool]:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"error": "text가 비었다"}, False
        filters: dict[str, object] = {
            key: args[key].strip() for key in filter_keys
            if isinstance(args.get(key), str) and args[key].strip()
        }
        query: SearchQuery = {
            "text": text.strip()[:500], "accident_date": self.ctx.accident_date,
            "filters": filters, "top_k": SEARCH_TOP_K,
        }
        try:
            hits = await searcher(query)
        except NotImplementedError:
            return {"error": "검색 색인이 아직 연결되지 않아 검색할 수 없다"}, False
        except ValueError as exc:
            return {"error": f"검색 요청이 올바르지 않다: {exc}"}, False
        except Exception:
            logger.exception("qa search failed for case %s", self.ctx.case_id)
            return {"error": "검색 중 오류가 났다"}, False
        return {"hits": self.refs.views(hits, "search")}, True

    async def _simulate(self, changes: object) -> tuple[dict, bool]:
        problem = check_changes(changes)
        if problem:
            return {"error": problem}, False
        if self.ctx.simulate is None:
            return {"error": "공식 판단에 쓴 사실 카드를 읽지 못해 가정 실행을 할 수 없다"}, False
        if self.simulations >= MAX_SIMULATIONS:
            return {
                "error": f"가정 실행은 질문당 {MAX_SIMULATIONS}번까지다. 지금까지 결과로 답하라."
            }, False
        self.simulated = True
        self.simulations += 1
        try:
            hypothesis, hits = await self.ctx.simulate(dict(changes))  # type: ignore[arg-type]
        except Exception:
            logger.exception("qa simulate failed for case %s", self.ctx.case_id)
            return {"error": "가정 실행 중 오류가 났다"}, False
        self.hypotheticals.append(hypothesis)
        evidence = self.refs.views(hits, "hypothesis")
        return {**hypothesis, "evidence": evidence}, hypothesis["status"] != "failed"

    def finish(self, text: str) -> QaAnswer:
        used: list[str] = []

        def keep(match: re.Match[str]) -> str:
            refs = [ref.strip() for ref in match.group(1).split(",")]
            known = [ref for ref in refs if ref in self.refs.citations]
            used.extend(ref for ref in known if ref not in used)
            return f"[{', '.join(known)}]" if known else ""

        answer = re.sub(r"\[\s*(E\d+(?:\s*,\s*E\d+)*)\s*\]", keep, text)
        answer = re.sub(r"[ \t]+([.,!?])", r"\1", answer).strip()
        if not answer:
            raise QaFailed("질의응답 모델이 답을 내지 못했다")
        return {
            "case_id": self.ctx.case_id, "judgment_version": self.ctx.judgment_version,
            "answer": answer, "citations": [self.refs.citations[ref] for ref in used],
            "hypotheticals": self.hypotheticals, "tool_calls": self.calls,
            "fact_change_hint": FACT_CHANGE_HINT if self.simulated else None,
        }


def user_prompt(question: str, ctx: QaContext) -> str:
    result = ctx.result
    return (
        f"공식 판단 요약(판단 버전 {ctx.judgment_version}): 판정 {result['status']}, "
        f"도표 {result.get('chart_id') or '없음'}, "
        f"과실 A {result.get('fault_A')} : B {result.get('fault_B')}. "
        f"사고 일자 {ctx.accident_date or '미상'}.\n질문: {question.strip()}"
    )


async def answer_question(question: str, ctx: QaContext, deps: QaDependencies) -> QaAnswer:
    """질문 하나에 답한다. 도구는 최대 MAX_TOOL_CALLS회 실행하고 그 뒤에는 도구 없이 답하게 한다.

    LLM 호출 시간 합계가 deps.llm_budget_seconds를 넘으면 QaTimedOut이다.
    도구 실행 시간은 세지 않는다.
    """
    llm = deps.llm or openai_qa_llm
    session = _Session(ctx, deps)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt(question, ctx)},
    ]
    loop = asyncio.get_running_loop()
    spent = 0.0
    while True:
        choice: Literal["auto", "none"] = "auto" if len(session.calls) < MAX_TOOL_CALLS else "none"
        remaining = deps.llm_budget_seconds - spent
        if remaining <= 0:
            raise QaTimedOut("질문 하나의 LLM 호출 시간 상한을 넘었다")
        started = loop.time()
        try:
            reply = await asyncio.wait_for(llm(messages, TOOLS, choice), timeout=remaining)
        except TimeoutError as exc:
            raise QaTimedOut("질문 하나의 LLM 호출 시간 상한을 넘었다") from exc
        spent += loop.time() - started
        tool_calls = reply.get("tool_calls") or []
        if choice == "none" or not tool_calls:
            return session.finish(reply.get("content") or "")
        messages.append({
            "role": "assistant", "content": reply.get("content"),
            "tool_calls": [
                {"id": call["id"], "type": "function",
                 "function": {"name": call["name"], "arguments": call.get("arguments") or "{}"}}
                for call in tool_calls
            ],
        })
        for call in tool_calls:  # 모델은 모든 tool_call에 응답 메시지가 있어야 다음으로 간다
            if len(session.calls) >= MAX_TOOL_CALLS:
                output: dict = {
                    "error": f"도구는 질문당 {MAX_TOOL_CALLS}회까지다. 지금까지 결과로 답하라."
                }
            else:
                output = await session.run(call)
            messages.append({
                "role": "tool", "tool_call_id": call["id"],
                "content": json.dumps(output, ensure_ascii=False, default=str),
            })


async def openai_qa_llm(
    messages: list[dict], tools: list[dict], tool_choice: Literal["auto", "none"]
) -> LlmMessage:
    """기본 모델. OPENAI_API_KEY·OPENAI_MODEL(반박 검토와 같은 변수)을 쓴다.

    호출 1회 상한·재시도는 app/llm.py 정책이다. 재시도 뒤에도 시간 초과면 QaTimedOut이다.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        raise QaNotConfigured("OPENAI_API_KEY가 없어 질의응답을 실행할 수 없다")
    model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"

    async def attempt() -> LlmMessage:
        async with llm_policy.openai_client() as client:
            response = await client.chat.completions.create(
                model=model, messages=messages, tools=tools, tool_choice=tool_choice,  # type: ignore[arg-type]
            )
        message = response.choices[0].message
        calls: list[LlmToolCall] = [
            {"id": call.id, "name": call.function.name,
             "arguments": call.function.arguments or "{}"}
            for call in (message.tool_calls or []) if getattr(call, "type", "") == "function"
        ]
        return {"content": message.content, "tool_calls": calls}

    try:
        return await llm_policy.with_retry(attempt, name="qa")
    except Exception as exc:
        if llm_policy.is_timeout(exc):
            raise QaTimedOut("질의응답 모델 호출이 시간 상한을 넘었다") from exc
        raise
