"""B: 검증 ② 반박 검토. 재실행 횟수·경로는 graph.py가 통제한다.

LLM은 "이 도표 선택이 틀렸다는 반박이 성립하는가"만 답한다. 비율·금액은 묻지 않는다.
응답은 JSON 스키마로 고정하고, 모양이 어긋나면 추측하지 않고 오류로 올린다(실행 실패).
호출 1회 상한(45초)·재시도(타임아웃·네트워크·5xx만 1회)는 app/llm.py 정책이다 — 공식 판단과
질의응답의 가정 실행이 같은 검토기를 쓴다.
"""

import json
import os
from collections.abc import Awaitable, Callable
from typing import Literal, TypedDict, get_args

from app import llm as llm_policy
from app.schemas.chart_scope import FIELD_OPTIONS
from app.schemas.facts import FactCard
from app.schemas.judgment import RebuttalKind, ValidationCheck
from app.workflow.candidates import ChartCandidate, confirmed_values
from app.workflow.card_fields import chart_key, confirmed_unknown_fields, is_swapped
from app.workflow.state import ChartSelection

# 도표 A가 카드 B(촬영 차량)인 카드에서 LLM에 덧붙이는 안내(v1 2절)
SWAPPED_NOTE = (
    "이 사건은 도표 A가 카드의 B(촬영 차량)다. confirmed_facts의 도표 선택 칸은 도표 A/B 기준으로 "
    "바꿔 넣었다. 서술(narrative)의 A/B는 카드 기준이라 도표 A/B와 반대다."
)

Reviewer = Callable[[str, str], Awaitable[str]]
"""(system, user) → JSON 문자열. 기본은 OpenAI, 테스트는 가짜를 넣는다."""

KINDS: tuple[str, ...] = get_args(RebuttalKind)

SYSTEM_PROMPT = (
    "당신은 자동차 사고 과실비율 인정기준 검토자다. 담당자가 확인한 사실 카드와 선택된 도표, "
    "차순위 후보를 보고 '이 도표 선택에 반박이 성립하는가'만 판단한다.\n"
    "반박이 성립하면 kind를 하나 고른다: wrong_chart(차순위 후보가 사실에 더 맞음), "
    "missing_fact(도표를 가르는 사실이 카드에 없음), contradiction(카드 사실과 도표 조건이 모순).\n"
    "카드에 없는 사실을 가정하지 말고, 과실비율 숫자를 제안하지 말라. "
    "wrong_chart일 때만 preferred_chart_id에 차순위 후보 ID를 적는다.\n"
    "JSON만 출력한다."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "holds": {"type": "boolean"},
        "kind": {"type": ["string", "null"], "enum": [*KINDS, None]},
        "preferred_chart_id": {"type": ["string", "null"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["holds", "kind", "preferred_chart_id", "reasons"],
}


class ReviewerNotConfigured(NotImplementedError):
    """반박 검토 모델 키가 없다.

    판단 실행은 dependency_not_ready로 실패한다(추측으로 통과시키지 않음).
    """


class RebuttalVerdict(TypedDict):
    holds: bool
    kind: Literal["wrong_chart", "missing_fact", "contradiction"] | None
    preferred_chart_id: str | None
    reasons: list[str]


def review_payload(
    card: FactCard, selection: ChartSelection, candidates: list[ChartCandidate]
) -> str:
    """LLM에 주는 사실·선택·후보 요약. 확인한 칸만 사실로 넣는다.

    도표 선택 칸은 판단이 읽은 값 그대로(`confirmed_values`) 넣는다. 그래서 선택한 도표 속성과
    같은 기준이 된다. v1 2절 맞바꿈(도표 A = 카드 B)이면 카드 칸을 그대로 넣었을 때 신호·진행 방향이
    도표와 반대로 보여 '모순'으로 판정될 수 있었다. 한국어 안 카드도 도표 값(영어)으로 들어간다.
    담당자가 모름으로 확인한 도표 선택 칸은 `unknown_facts`로, 나머지 확인한 칸은 그대로 넣는다.
    """
    facts: dict[str, object] = dict(confirmed_values(card))
    for key, field in card["fields"].items():
        confirmed = field["verification_status"] == "human_confirmed"
        if confirmed and chart_key(key) not in FIELD_OPTIONS:
            facts[key] = field["value"]
    by_id = {c["chart_id"]: c for c in candidates}
    chosen = by_id.get(selection["chart_id"])
    alternatives = [
        {"chart_id": cid, "title": by_id[cid]["title"], "attributes": by_id[cid]["attributes"]}
        for cid in selection["alternatives"] if cid in by_id
    ]
    payload: dict[str, object] = {
        "confirmed_facts": facts,
        "unknown_facts": confirmed_unknown_fields(card),
        "selected_chart": {
            "chart_id": selection["chart_id"], "edition": selection["edition"],
            "title": chosen["title"] if chosen else None,
            "attributes": chosen["attributes"] if chosen else {},
            "variant": selection["variant"],
            "applied_modifiers": selection["applied_modifiers"],
            "selection_reasons": selection["reasons"],
        },
        "alternative_charts": alternatives,
    }
    if is_swapped(card):
        payload["party_note"] = SWAPPED_NOTE
    return json.dumps(payload, ensure_ascii=False)


def parse_verdict(raw: str, alternatives: list[str]) -> RebuttalVerdict:
    """LLM 응답을 검증한다. 모양이 틀리면 ValueError(추측으로 메우지 않는다)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"반박 검토 응답이 JSON이 아니다: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("holds"), bool):
        raise ValueError("반박 검토 응답에 holds(boolean)가 없다")
    reasons = data.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise ValueError("반박 검토 응답의 reasons가 문자열 목록이 아니다")
    kind = data.get("kind")
    preferred = data.get("preferred_chart_id")
    if not data["holds"]:
        return {"holds": False, "kind": None, "preferred_chart_id": None, "reasons": reasons}
    if kind not in KINDS:
        raise ValueError(f"반박이 성립하는데 kind가 올바르지 않다: {kind!r}")
    if not reasons:
        raise ValueError("반박이 성립하는데 이유가 없다")
    if kind == "wrong_chart":
        if preferred not in alternatives:
            raise ValueError(
                f"wrong_chart인데 preferred_chart_id가 차순위 후보가 아니다: {preferred!r}"
            )
    else:
        preferred = None
    return {"holds": True, "kind": kind, "preferred_chart_id": preferred, "reasons": reasons}


async def openai_reviewer(system: str, user: str) -> str:
    """기본 검토기. OPENAI_API_KEY·OPENAI_MODEL을 쓰고 JSON 스키마 출력을 강제한다."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise ReviewerNotConfigured("OPENAI_API_KEY가 없어 반박 검토를 실행할 수 없다")
    model = os.environ.get("OPENAI_MODEL") or "gpt-5"

    async def attempt() -> str:
        async with llm_policy.openai_client() as client:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "rebuttal_verdict", "strict": True,
                                    "schema": RESPONSE_SCHEMA},
                },
            )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("반박 검토 응답이 비었다")
        return content

    return await llm_policy.with_retry(attempt, name="rebuttal")


async def review_rebuttal(
    card: FactCard,
    selection: ChartSelection,
    candidates: list[ChartCandidate],
    reviewer: Reviewer | None = None,
) -> tuple[ValidationCheck, str | None]:
    """(검증 결과, wrong_chart일 때 재실행할 도표 ID).

    실패하면 rebuttal_kind를 반드시 채운다. wrong_chart(차순위 후보가 더 맞음)만
    graph.py가 재실행하고, missing_fact(구분 조건 누락)·contradiction(근거와 카드 모순)은
    담당자 질문으로 넘긴다.
    """
    review = reviewer or openai_reviewer
    raw = await review(SYSTEM_PROMPT, review_payload(card, selection, candidates))
    verdict = parse_verdict(raw, selection["alternatives"])
    check: ValidationCheck = {
        "layer": "rebuttal", "passed": not verdict["holds"], "reasons": verdict["reasons"],
        "rebuttal_kind": verdict["kind"],
    }
    return check, verdict["preferred_chart_id"]
