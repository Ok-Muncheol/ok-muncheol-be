"""B(통합): 배정 에이전트. 접수 글을 읽고 지역의 대물·대인 담당자를 고른다.

LLM이 도구를 골라 쓴다(최대 MAX_LLM_CALLS회).
    find_adjusters(region, coverage)  그 지역·분야의 자동 배정 후보와 진행 중 사건 수
    assign(...)                       고른 담당자와 까닭
    leave_unassigned(reason)          배정할 수 없을 때

LLM이 하는 일은 글에서 지역을 판별하는 것과, 후보 가운데 한 사람을 골라 까닭을 쓰는 것이다.
틀리면 안 되는 것은 코드가 지킨다.
- 후보는 코드가 DB에서 읽는다. LLM은 도구가 돌려준 표시(P1·I2…)로만 고를 수 있고,
  돌려준 적 없는 사람은 고를 수 없다.
- 후보가 있는데 미배정으로 두려 하거나, 호출이 실패·시간 초과이거나, 답이 규칙에 맞지 않으면
  규칙 배정(service.decide_by_rule)으로 대신한다. 접수는 에이전트 없이도 늘 배정된다.
- 다친 사람이 있는데 대인 담당자를 고르지 않았으면 같은 지역에서 업무량으로 채운다.
service.Decider 모양이라 접수 등록(service.register)에 그대로 끼운다.
"""

import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypedDict

from psycopg import AsyncConnection

from app import llm as llm_policy
from app.dispatch import repository, service
from app.dispatch.regions import REGIONS
from app.dispatch.service import Decision, Pick

logger = logging.getLogger(__name__)

MAX_LLM_CALLS = 4
REASON_CHARS = 400
HANDLE_PREFIX = {"property": "P", "injury": "I"}

SYSTEM_PROMPT = (
    "당신은 자동차보험 사고 접수를 담당자에게 배정하는 배정 담당이다.\n"
    "접수 글의 사고 장소에서 지역(시·도)을 판별하고, 그 지역 담당자 가운데 한 사람을 고른다.\n"
    "규칙:\n"
    "1. 먼저 find_adjusters로 그 지역의 대물(property) 담당자 후보를 본다. "
    "다친 사람이 1명 이상이면 대인(injury) 담당자 후보도 본다.\n"
    "2. 후보 가운데 진행 중인 사건이 적은 사람을 고른다. "
    "도구가 돌려준 표시(예: P1, I2)로만 고른다.\n"
    "3. assign으로 배정하고, 까닭을 한국어 한두 문장으로 쓴다(판별한 지역, 후보 수, 업무량).\n"
    "4. 사고 장소에서 지역을 알 수 없거나 대물 담당자 후보가 한 명도 없으면 "
    "leave_unassigned로 까닭을 남긴다. 지역을 지어내지 않는다.\n"
    "지역 이름은 도구에 적힌 17개 가운데 하나다. 시·군·구 이름만 있으면 그 시·도로 판별한다. "
    "'광주시'는 경기도 광주시(경기)이고, '광주'·'광주광역시'가 광주다."
)

TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "find_adjusters",
        "description": "그 지역·분야의 자동 배정 후보와 지금 진행 중인 사건 수를 돌려준다.",
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["region", "coverage"],
                       "properties": {
                           "region": {"type": "string", "enum": list(REGIONS)},
                           "coverage": {"type": "string", "enum": ["property", "injury"]}}},
    }},
    {"type": "function", "function": {
        "name": "assign",
        "description": ("고른 담당자에게 배정한다. 대물 담당자는 반드시, "
                        "대인 담당자는 다친 사람이 있을 때 넣는다."),
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["property_adjuster", "property_reason"],
                       "properties": {
                           "property_adjuster": {"type": "string", "description": "예: P1"},
                           "property_reason": {"type": "string"},
                           "injury_adjuster": {"type": "string", "description": "예: I1"},
                           "injury_reason": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "leave_unassigned",
        "description": "지역을 알 수 없거나 대물 담당자 후보가 없어 배정하지 않는다.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["reason"],
                       "properties": {"reason": {"type": "string"}}},
    }},
]


class ToolCall(TypedDict):
    id: str
    name: str
    arguments: str


class LlmMessage(TypedDict):
    content: str | None
    tool_calls: list[ToolCall]


Llm = Callable[[list[dict], list[dict]], Awaitable[LlmMessage]]


class AgentFailed(Exception):  # noqa: N818 — 규칙 배정으로 넘어가는 신호
    """에이전트가 쓸 수 있는 결정을 내지 못했다."""


def model_name() -> str:
    return os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"


async def openai_llm(messages: list[dict], tools: list[dict]) -> LlmMessage:
    """기본 모델(OPENAI_API_KEY·OPENAI_MODEL). 호출 1회 상한·재시도는 app/llm.py 정책이다."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise AgentFailed("OPENAI_API_KEY가 없다")

    async def attempt() -> LlmMessage:
        async with llm_policy.openai_client() as client:
            response = await client.chat.completions.create(
                model=model_name(), messages=messages, tools=tools, tool_choice="required",  # type: ignore[arg-type]
                temperature=0,
            )
        message = response.choices[0].message
        calls: list[ToolCall] = [
            {"id": call.id, "name": call.function.name,
             "arguments": call.function.arguments or "{}"}
            for call in (message.tool_calls or []) if getattr(call, "type", "") == "function"
        ]
        return {"content": message.content, "tool_calls": calls}

    return await llm_policy.with_retry(attempt, name="dispatch")


def _intake_text(intake: dict) -> str:
    return (
        f"사고 장소: {intake['location']}\n"
        f"사고 내용: {intake['description']}\n"
        f"다친 사람 수: {int(intake['injured_count'])}명"
    )


def _reason(value: object, fallback: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return (text or fallback)[:REASON_CHARS]


class _Run:
    """접수 한 건의 에이전트 실행 상태. 도구가 돌려준 후보만 고를 수 있게 기억한다."""

    def __init__(self, conn: AsyncConnection, intake: dict, at: datetime):
        self.conn, self.intake, self.at = conn, intake, at
        self.seen: dict[str, tuple[str, dict]] = {}  # 표시 → (지역, 후보 행)
        self.searched: dict[str, tuple[str, int]] = {}  # 분야 → (마지막에 본 지역, 후보 수)
        self.llm_calls = 0

    async def find(self, args: dict) -> list[dict]:
        region, coverage = args.get("region"), args.get("coverage")
        if region not in REGIONS or coverage not in HANDLE_PREFIX:
            raise AgentFailed(f"도구 인자가 틀리다: {args}")
        rows = await repository.candidates(self.conn, region, coverage, self.at)
        self.searched[coverage] = (region, len(rows))
        out = []
        for row in rows:
            handle = f"{HANDLE_PREFIX[coverage]}{len(self.seen) + 1}"
            self.seen[handle] = (region, row)
            out.append({"adjuster": handle, "name": row["name"],
                        "open_cases": int(row["open_cases"])})
        return out

    def _pick(self, handle: object, coverage: str, reason: object) -> Pick:
        found = self.seen.get(str(handle or "").strip().upper())
        if found is None or not str(handle).strip().upper().startswith(HANDLE_PREFIX[coverage]):
            raise AgentFailed(f"후보에 없는 담당자를 골랐다: {handle}")
        region, row = found
        count = self.searched[coverage][1]
        detail = {"region": region, "candidates": count, "open_cases": int(row["open_cases"]),
                  "model": model_name(), "llm_calls": self.llm_calls}
        label = service.COVERAGE_LABELS[coverage]
        fallback = f"{region} 지역 {label} 담당자 {count}명 가운데 골랐습니다."
        return Pick(str(row["id"]), _reason(reason, fallback), detail)

    async def assign(self, args: dict) -> Decision:
        property_pick = self._pick(args.get("property_adjuster"), "property",
                                   args.get("property_reason"))
        region = property_pick.detail["region"]
        injury_pick, note = None, None
        if int(self.intake["injured_count"]) > 0:
            if args.get("injury_adjuster"):
                injury_pick = self._pick(args["injury_adjuster"], "injury",
                                         args.get("injury_reason"))
                if injury_pick.detail["region"] != region:
                    raise AgentFailed("대물과 대인 담당자의 지역이 다르다")
            else:  # 고르지 않았으면 같은 지역에서 업무량으로 채운다
                injury_pick = await service.pick_by_load(
                    self.conn, self.intake, region, "injury", self.at
                )
                if injury_pick is None:
                    note = f"{region} 지역에 배정할 대인 담당자가 없습니다."
        return Decision("agent", region, property_pick, injury_pick, injury_note=note)

    def unassigned(self, args: dict) -> Decision:
        region, count = self.searched.get("property", (None, 0))
        if count:
            raise AgentFailed("대물 담당자 후보가 있는데 배정하지 않으려 했다")
        reason = _reason(args.get("reason"), "배정할 담당자를 찾지 못했습니다.")
        return Decision("agent", region, None, unassigned_reason=reason)


async def _run(conn: AsyncConnection, intake: dict, at: datetime, llm: Llm) -> Decision:
    run = _Run(conn, intake, at)
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": _intake_text(intake)}]
    for _ in range(MAX_LLM_CALLS):
        reply = await llm(messages, TOOLS)
        run.llm_calls += 1
        calls = reply["tool_calls"]
        if not calls:
            raise AgentFailed("도구를 부르지 않았다")
        messages.append({
            "role": "assistant", "content": reply["content"],
            "tool_calls": [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]}}
                           for c in calls],
        })
        for call in calls:
            try:
                args = json.loads(call["arguments"] or "{}")
            except ValueError as exc:
                raise AgentFailed("도구 인자가 JSON이 아니다") from exc
            if not isinstance(args, dict):
                raise AgentFailed("도구 인자가 객체가 아니다")
            if call["name"] == "assign":
                return await run.assign(args)
            if call["name"] == "leave_unassigned":
                return run.unassigned(args)
            if call["name"] != "find_adjusters":
                raise AgentFailed(f"모르는 도구: {call['name']}")
            found = await run.find(args)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(found, ensure_ascii=False)})
    raise AgentFailed("정해진 횟수 안에 결정을 내지 못했다")


def decider(llm: Llm = openai_llm) -> service.Decider:
    """에이전트로 정하고, 못 정하면 규칙으로 정하는 Decider."""

    async def decide(conn: AsyncConnection, intake: dict, at: datetime) -> Decision:
        try:
            return await _run(conn, intake, at, llm)
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 접수는 규칙으로 배정한다
            logger.warning("배정 에이전트 실패(%s) — 규칙으로 배정한다", type(exc).__name__)
            return await service.decide_by_rule(conn, intake, at)

    return decide


decide = decider()
