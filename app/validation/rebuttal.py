"""B: 검증 ② 반박 검토. 재실행 횟수·경로는 graph.py가 통제한다.

LLM은 "이 도표 선택이 틀렸다는 반박이 성립하는가"만 답한다. 비율·금액은 묻지 않는다.
응답은 JSON 스키마로 고정하고, 모양이 어긋나면 추측하지 않고 오류로 올린다(실행 실패).
"""

import json
import os
from collections.abc import Awaitable, Callable
from typing import Literal, TypedDict, get_args

from app.schemas.facts import FactCard
from app.schemas.judgment import RebuttalKind, ValidationCheck
from app.workflow.candidates import ChartCandidate
from app.workflow.state import ChartSelection

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


class RebuttalVerdict(TypedDict):
    holds: bool
    kind: Literal["wrong_chart", "missing_fact", "contradiction"] | None
    preferred_chart_id: str | None
    reasons: list[str]


def review_payload(
    card: FactCard, selection: ChartSelection, candidates: list[ChartCandidate]
) -> str:
    """LLM에 주는 사실·선택·후보 요약. 확인한 칸만 사실로 넣는다."""
    confirmed = {
        key: field["value"] for key, field in card["fields"].items()
        if field["verification_status"] == "human_confirmed"
    }
    by_id = {c["chart_id"]: c for c in candidates}
    chosen = by_id.get(selection["chart_id"])
    alternatives = [
        {"chart_id": cid, "title": by_id[cid]["title"], "attributes": by_id[cid]["attributes"]}
        for cid in selection["alternatives"] if cid in by_id
    ]
    return json.dumps(
        {
            "confirmed_facts": confirmed,
            "selected_chart": {
                "chart_id": selection["chart_id"], "edition": selection["edition"],
                "title": chosen["title"] if chosen else None,
                "attributes": chosen["attributes"] if chosen else {},
                "variant": selection["variant"],
                "applied_modifiers": selection["applied_modifiers"],
                "selection_reasons": selection["reasons"],
            },
            "alternative_charts": alternatives,
        },
        ensure_ascii=False,
    )


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
    from openai import AsyncOpenAI

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 없어 반박 검토를 실행할 수 없다")
    model = os.environ.get("OPENAI_MODEL") or "gpt-5"
    async with AsyncOpenAI(timeout=60, max_retries=2) as client:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
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
