"""B(통합): 배정 에이전트. 접수 글과 배정 참고 정보를 읽고 지역의 대물·대인 담당자를 고른다.

LLM이 도구를 골라 쓴다(최대 MAX_LLM_CALLS회).
    find_adjusters(region, coverage)  그 지역·분야에서 이 접수를 맡을 수 있는 후보
                                      (진행 중 사건 수·경력·이 사건에 맞는 전문 분야)와
                                      이 사건의 특징(필요한 전문 분야·까다로운 사정)
    assign(coverage, adjuster, reason) 한 분야의 담당자로 고른 사람과 까닭(분야마다 한 번)
    leave_unassigned(reason)          배정할 수 없을 때

LLM이 하는 일은 글에서 지역을 판별하는 것과, 후보 가운데 이 사건에 맞는 한 사람을 골라 까닭을
쓰는 것이다. 사건의 규모·차종·다친 정도·가입 담보·계약자 이력을 후보의 전문 분야·경력·업무량과
견준다. 틀리면 안 되는 것은 코드가 지킨다.
- 사건에 어떤 전문 분야가 필요한지, 까다로운 사건인지는 코드가 판정해 도구 결과에 넣는다
  (service.needed_specialties·complications). LLM이 사건과 상관없는 전문 분야를 까닭으로 들지
  않게 하려는 것이다. 그 사실들을 견주어 한 사람을 고르는 것이 LLM의 몫이다.
- 후보는 코드가 DB에서 읽는다(service.eligible). 부재인 사람과, 고액 사건이면 처리 권한이 없는
  사람은 도구가 돌려주지 않는다. LLM은 도구가 돌려준 표시(P1·I2…)로만 고를 수 있다.
- LLM이 규칙에 어긋나게 답하면(후보가 있는데 미배정, 없는 표시, 대물·대인 지역이 다름) 무엇이
  틀렸는지 도구 결과로 알려 주고 다시 고르게 한다. 정해진 횟수 안에 못 고치거나 호출이
  실패·시간 초과이면 규칙 배정(service.decide_by_rule)으로 대신한다. 접수는 늘 배정된다.
- 다친 사람이 있는데 대인 담당자를 고르지 않았거나 대물과 다른 지역에서 골랐으면 같은 지역에서
  업무량으로 채운다.
- LLM에 보내는 것은 사고 장소·내용과 배정 참고 정보뿐이다. 사람을 특정하는 정보는 접수 건에
  애초에 없다(후보의 이름은 담당자 이름이다).
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
from app.dispatch import regions, service
from app.dispatch.regions import REGIONS
from app.dispatch.service import Decision, Eligible, Pick

logger = logging.getLogger(__name__)

MAX_LLM_CALLS = 6
REASON_CHARS = 400
HANDLE_PREFIX = {"property": "P", "injury": "I"}
SPECIALTY_LABELS = {"imported": "수입차", "serious_injury": "중상해", "multi_vehicle": "다중 추돌"}
VEHICLE_LABELS = {"passenger": "승용차", "imported": "수입차", "truck": "화물차",
                  "motorcycle": "이륜차"}
SEVERITY_LABELS = {"minor": "경상", "serious": "중상", "fatal": "사망"}
PERSONAL_INJURY_LABELS = {"none": "미가입", "self_injury": "자기신체사고",
                          "auto_injury": "자동차상해"}

SYSTEM_PROMPT = (
    "당신은 자동차보험 사고 접수를 담당자에게 배정하는 배정 담당이다.\n"
    "접수의 사고 장소에서 지역(시·도)을 판별하고, 그 지역 담당자 가운데 이 사건에 맞는 한 사람을 "
    "고른다.\n"
    "순서:\n"
    "1. find_adjusters로 그 지역의 대물(property) 담당자 후보를 본다. "
    "다친 사람이 1명 이상이면 대인(injury) 담당자 후보도 본다.\n"
    "2. 분야마다 후보 가운데 한 사람을 골라 assign으로 배정한다(대물 한 번, 다친 사람이 있으면 "
    "대인도 한 번 — 한꺼번에 불러도 된다). 도구가 돌려준 표시(예: P1, I2)로만 고른다.\n"
    "3. 까닭을 한국어 한두 문장으로 쓴다(판별한 지역, 후보 수, 그 사람을 고른 기준). "
    "까닭에는 도구 결과에 있는 사실만 쓴다.\n"
    "반드시 지킬 것:\n"
    "- 도구가 돌려준 후보는 모두 이 접수를 맡을 수 있는 사람이다(부재·고액 사건 권한은 이미 "
    "걸렀다). 후보가 한 명이라도 있으면 반드시 그 가운데 한 명에게 배정한다. 진행 중인 사건이 "
    "많다거나 모두 같다는 것은 배정하지 않을 까닭이 아니다. 같으면 아무나 고른다.\n"
    "- leave_unassigned는 사고 장소에서 지역을 알 수 없거나, 대물 담당자 후보가 한 명도 없을 "
    "때만 쓴다. 지역을 지어내지 않는다.\n"
    "고르는 기준(위에 있을수록 먼저 본다). 도구 결과의 case가 이 사건의 특징이다:\n"
    "- case.needs_specialties가 비어 있지 않으면, matched_specialties가 있는 후보(이 사건에 맞는 "
    "전문 분야가 있는 사람)를 먼저 고른다.\n"
    "- case.complications가 비어 있지 않으면 까다로운 사건이다. career_years(경력 연차)가 큰 "
    "사람을 고르고 까닭에 그 사정을 쓴다.\n"
    "- 그 밖에는 open_cases(진행 중인 사건)가 적은 사람. 이때 까닭에 그 건수를 쓴다.\n"
    "- 다만 전문 분야·경력으로 고른 사람이 다른 후보보다 진행 중인 사건이 5건 넘게 많으면 "
    "업무량이 적은 사람을 고른다.\n"
    "지역 이름은 도구에 적힌 17개 가운데 하나다. 시·군·구 이름만 있으면 그 시·도로 판별한다. "
    "'광주시'는 경기도 광주시(경기)이고, '광주'·'광주광역시'가 광주다."
)

TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "find_adjusters",
        "description": ("그 지역·분야에서 이 접수를 맡을 수 있는 후보를 돌려준다"
                        "(진행 중인 사건 수, 경력 연차, 전문 분야)."),
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["region", "coverage"],
                       "properties": {
                           "region": {"type": "string", "enum": list(REGIONS)},
                           "coverage": {"type": "string", "enum": ["property", "injury"]}}},
    }},
    {"type": "function", "function": {
        "name": "assign",
        "description": ("한 분야의 담당자로 고른 사람에게 배정한다. 대물은 반드시, "
                        "대인은 다친 사람이 있을 때 부른다."),
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["coverage", "adjuster", "reason"],
                       "properties": {
                           "coverage": {"type": "string", "enum": ["property", "injury"]},
                           "adjuster": {"type": "string", "description": "예: P1 또는 I1"},
                           "reason": {"type": "string"}}},
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


class Correction(Exception):  # noqa: N818 — LLM에게 돌려주는 말
    """LLM의 답이 규칙에 어긋난다. 까닭을 도구 결과로 알려 주고 다시 고르게 한다."""


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


def _won(value: object) -> str:
    amount = int(value)  # type: ignore[call-overload]
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:g}억 원"
    return f"{amount // 10_000:,}만 원"


def _intake_text(intake: dict) -> str:
    lines = [
        f"사고 장소: {intake['location']}",
        f"사고 내용: {intake['description']}",
        f"다친 사람 수: {int(intake['injured_count'])}명",
    ]
    details = intake.get("details") or {}
    if details.get("injury_severity") in SEVERITY_LABELS:
        lines.append(f"다친 정도: {SEVERITY_LABELS[details['injury_severity']]}")
    if details.get("vehicle_type") in VEHICLE_LABELS:
        lines.append(f"우리 계약 차량: {VEHICLE_LABELS[details['vehicle_type']]}")
    if details.get("vehicle_count"):
        lines.append(f"관련 차량 수: {int(details['vehicle_count'])}대")
    if details.get("estimated_damage") is not None:
        lines.append(f"예상 손해액: {_won(details['estimated_damage'])}")
    policy = details.get("policy")
    if isinstance(policy, dict):
        lines.append(
            "가입 담보: 대인배상Ⅱ " + ("가입" if policy.get("bodily_injury_ii") else "미가입")
            + (f", 대물 한도 {_won(policy['property_limit'])}"
               if policy.get("property_limit") else "")
            + ", 자기차량손해 " + ("가입" if policy.get("own_damage") else "미가입")
            + ", 자기신체사고·자동차상해 "
            + PERSONAL_INJURY_LABELS.get(str(policy.get("personal_injury")), "미가입")
            + ", 무보험차상해 " + ("가입" if policy.get("uninsured_motorist") else "미가입")
        )
    holder = details.get("policyholder")
    if isinstance(holder, dict):
        lines.append(
            f"계약자 이력: 최근 3년 사고 {int(holder.get('accidents_3y') or 0)}회, "
            f"민원 {int(holder.get('complaints') or 0)}회, "
            + ("장기 고객" if holder.get("long_term") else "장기 고객 아님")
        )
    return "\n".join(lines)


def _reason(value: object, fallback: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return (text or fallback)[:REASON_CHARS]


class _Run:
    """접수 한 건의 에이전트 실행 상태. 도구가 돌려준 후보만 고를 수 있게 기억한다."""

    def __init__(self, conn: AsyncConnection, intake: dict, at: datetime):
        self.conn, self.intake, self.at = conn, intake, at
        self.seen: dict[str, tuple[Eligible, dict]] = {}  # 표시 → (그때의 후보 묶음, 후보 행)
        self.pools: dict[str, Eligible] = {}  # 분야 → 마지막에 본 후보 묶음
        self.picks: dict[str, Pick] = {}  # 분야 → 고른 사람
        self.llm_calls = 0
        self.corrections = 0

    async def find(self, args: dict) -> dict:
        region, coverage = args.get("region"), args.get("coverage")
        if region not in REGIONS or coverage not in HANDLE_PREFIX:
            raise Correction("region은 17개 시·도 가운데 하나, coverage는 property 또는 injury다.")
        pool = await service.eligible(self.conn, self.intake, region, coverage, self.at)
        self.pools[coverage] = pool
        needed = service.needed_specialties(self.intake, coverage)
        hard = service.complications(self.intake)
        out = []
        for row in pool.rows:
            # 표시는 분야마다 1부터 센다(P1·P2…, I1·I2…) — 모델이 짐작한 번호와 어긋나지 않게
            taken = sum(1 for key in self.seen if key.startswith(HANDLE_PREFIX[coverage]))
            handle = f"{HANDLE_PREFIX[coverage]}{taken + 1}"
            self.seen[handle] = (pool, row)
            # 이 사건에 쓸 사실만 보인다 — 상관없는 전문 분야나 경력을 까닭으로 들지 않게.
            # 전문 분야는 사건에 맞는 것만, 경력은 까다로운 사건일 때만 넣는다
            candidate = {"adjuster": handle, "name": row["name"],
                         "open_cases": int(row["open_cases"])}
            if needed:
                candidate["matched_specialties"] = [SPECIALTY_LABELS[s] for s in needed
                                                    if s in row["specialties"]]
            if hard:
                candidate["career_years"] = int(row["career_years"])
            out.append(candidate)
        label = service.COVERAGE_LABELS[coverage]
        result: dict = {
            "case": {"needs_specialties": [SPECIALTY_LABELS[s] for s in needed],
                     "complications": hard},
            "count": len(out), "candidates": out,
        }
        if not out:
            result["note"] = pool.none_reason()
        else:
            result["next"] = f"이 {len(out)}명 가운데 한 명을 {label} 담당자로 assign 한다."
            if pool.restricted:
                result["note"] = "고액 사건이라 처리 권한이 있는 담당자만 보였다."
        return result

    def choose(self, args: dict) -> dict:
        """한 분야의 담당자를 고른다. 같은 분야를 다시 고르면 나중 것이 남는다."""
        coverage = args.get("coverage")
        if coverage not in HANDLE_PREFIX:
            raise Correction("coverage는 property 또는 injury다.")
        label = service.COVERAGE_LABELS[coverage]
        key = str(args.get("adjuster") or "").strip().upper()
        found = self.seen.get(key)
        if found is None or not key.startswith(HANDLE_PREFIX[coverage]):
            options = [k for k in self.seen if k.startswith(HANDLE_PREFIX[coverage])]
            raise Correction(
                f"{label} 담당자 '{args.get('adjuster')}'는 후보에 없다. "
                + (f"고를 수 있는 표시: {', '.join(options)}." if options
                   else f"먼저 find_adjusters로 {label} 담당자 후보를 본다.")
            )
        pool, row = found
        detail = {
            "region": pool.region, "candidates": len(pool.rows),
            "open_cases": int(row["open_cases"]), "career_years": int(row["career_years"]),
            "specialties": list(row["specialties"]), "high_value_only": pool.restricted,
            "needs_specialties": service.needed_specialties(self.intake, coverage),
            "complications": service.complications(self.intake), "model": model_name(),
        }
        fallback = f"{pool.region} 지역 {label} 담당자 {len(pool.rows)}명 가운데 골랐습니다."
        self.picks[coverage] = Pick(str(row["id"]), _reason(args.get("reason"), fallback), detail)
        return {"ok": True, "assigned": key}

    async def finish(self) -> Decision:
        """대물 담당자를 고른 뒤의 결정. 대인 담당자가 빠졌으면 같은 지역에서 업무량으로 채운다."""
        counts = {"llm_calls": self.llm_calls, "corrections": self.corrections}
        chosen = self.picks["property"]
        property_pick = Pick(chosen.user_id, chosen.reason, {**chosen.detail, **counts})
        region = property_pick.detail["region"]
        injury_pick, note = None, None
        if int(self.intake["injured_count"]) > 0:
            picked = self.picks.get("injury")
            if picked is not None and picked.detail["region"] == region:
                injury_pick = Pick(picked.user_id, picked.reason, {**picked.detail, **counts})
            else:  # 고르지 않았거나 다른 지역에서 골랐으면 같은 지역에서 업무량으로 채운다
                injury_pick, pool = await service.pick_by_load(
                    self.conn, self.intake, region, "injury", self.at
                )
                if injury_pick is None:
                    note = pool.none_reason()
        return Decision("agent", region, property_pick, injury_pick, injury_note=note)

    def unassigned(self, args: dict) -> Decision:
        pool = self.pools.get("property")
        if pool is None:  # 후보를 보지도 않았다 — 지역을 정말 알 수 없는 글인지 확인한다
            if regions.detect(self.intake["location"]) is not None:
                raise Correction("사고 장소에 지역이 있다. find_adjusters로 후보를 먼저 본다.")
            reason = _reason(args.get("reason"), "사고 장소에서 지역을 판별하지 못했습니다.")
            return Decision("agent", None, None, unassigned_reason=reason)
        if pool.rows:
            options = [k for k in self.seen if k.startswith(HANDLE_PREFIX["property"])]
            raise Correction(
                "대물 담당자 후보가 있으므로 미배정으로 둘 수 없다. 진행 중인 사건이 많거나 같아도 "
                f"{', '.join(options)} 가운데 한 명을 골라 assign으로 배정한다."
            )
        return Decision("agent", pool.region, None, unassigned_reason=pool.none_reason())


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
            try:
                if call["name"] == "assign":
                    result = run.choose(args)
                elif call["name"] == "leave_unassigned":
                    return run.unassigned(args)
                elif call["name"] == "find_adjusters":
                    result = await run.find(args)
                else:
                    raise AgentFailed(f"모르는 도구: {call['name']}")
            except Correction as exc:
                run.corrections += 1
                result = {"error": str(exc)}
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
        if "property" in run.picks:  # 한 번의 답에서 고른 것을 모두 받은 뒤 끝낸다
            return await run.finish()
    raise AgentFailed("정해진 횟수 안에 결정을 내지 못했다")


def decider(llm: Llm = openai_llm) -> service.Decider:
    """에이전트로 정하고, 못 정하면 규칙으로 정하는 Decider."""

    async def decide(conn: AsyncConnection, intake: dict, at: datetime) -> Decision:
        try:
            return await _run(conn, intake, at, llm)
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 접수는 규칙으로 배정한다
            logger.warning(
                "배정 에이전트 실패(%s: %s) — 규칙으로 배정한다", type(exc).__name__, str(exc)[:120]
            )
            return await service.decide_by_rule(conn, intake, at)

    return decide


decide = decider()
