"""B: 검증 ② 반박 검토. 재실행 횟수·경로는 graph.py가 통제한다.

LLM은 "이 도표 선택이 틀렸다는 반박이 성립하는가"만 답한다. 비율·금액은 묻지 않는다.
응답은 JSON 스키마로 고정하고, 모양이 어긋나면 추측하지 않고 오류로 올린다(실행 실패).
판정은 값 하나(`verdict`: 반박 없음 또는 반박 종류)로 받는다. 예전 형식(`holds` 참거짓 + `kind`)은
모델이 `holds`를 "선택이 성립한다"로 읽고 반박 없음 이유와 함께 `holds: true, kind: null`을 내서
판단이 실패했다(2026-11-12, 진입 순서 `*`인 도표).
운영 모델은 gpt-4o-mini다(호출 1~2초). 이 모델이 과실비율을 따져 맞는 도표를 '모순'으로 돌리거나
고른 도표 자신을 `wrong_chart` 후보로 적는 일이 있었다(11/12 시범 실행 58건 중 3건).
그래서 프롬프트에 판정마다 칸 대조 규칙을 적고, 차순위 후보가 아닌 `wrong_chart`는 실행을 멈추지
않고 담당자 확인으로 넘긴다. '모순'·'사실 누락' 주장은 코드가 칸마다 대조해 확인될 때만 따른다
(`claim_confirmed`). 판단 그래프는 '*'가 아닌 조건이 모두 확인된 사실과 같은 도표만 고른다.
그래서 확인되지 않는 주장은 검증 사유에 경고로만 남긴다.
호출 1회 상한(45초)·재시도(타임아웃·네트워크·5xx만 1회)는 app/llm.py 정책이다 — 공식 판단과
질의응답의 가정 실행이 같은 검토기를 쓴다.
"""

import json
import os
from collections.abc import Awaitable, Callable
from typing import Literal, TypedDict, get_args

from app import llm as llm_policy
from app.schemas.chart_scope import ANY_VALUE, FIELD_OPTIONS
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
NO_REBUTTAL = "none"
# LLM이 고르는 판정: 반박 없음 또는 반박 종류 하나. 둘이 따로 있으면 서로 어긋날 수 있다
VERDICTS: tuple[str, ...] = (NO_REBUTTAL, *KINDS)

SYSTEM_PROMPT = (
    "당신은 자동차 사고 과실비율 인정기준 검토자다. 담당자가 확인한 사실 카드와 선택된 도표, "
    "차순위 후보를 보고 '이 도표 선택에 반박이 성립하는가'만 판단한다.\n"
    "판단은 선택한 도표의 조건과 확인한 사실을 칸마다 대조하는 것뿐이다. 과실비율이 타당한지, "
    "누구 과실이 더 큰지는 판단하지 않는다.\n"
    "verdict를 하나 고른다.\n"
    "- none: 반박 없음. 선택한 도표의 조건이 확인한 사실과 맞는다\n"
    "- contradiction: selected_chart의 attributes·variant.conditions 중 '*'가 아닌 값이 "
    "confirmed_facts의 같은 칸 값과 다르다\n"
    "- missing_fact: '*'가 아닌 그 조건 칸이 confirmed_facts에 없다\n"
    "- wrong_chart: alternative_charts 중 하나가 확인한 사실에 더 맞는다. preferred_chart_id에 "
    "그 후보 ID를 적는다. alternative_charts가 비었거나 선택한 도표 자신이면 고르지 않는다\n"
    "칸 이름이 정확히 같은 칸끼리만 대조한다. signal_A·signal_B는 교차로에 들어갈 때 신호, "
    "collision_signal_A·collision_signal_B는 충돌할 때 신호라 서로 다른 칸이다. 한 차의 두 신호 "
    "값이 달라도 모순이 아니다\n"
    "도표 조건 값 '*'는 그 도표가 그 칸으로 갈리지 않는다는 뜻이라 어떤 사실 값과도 맞는다. "
    "'*'인 칸은 반박 사유가 아니다. unknown_facts는 담당자가 모른다고 확인한 칸이고, "
    "party_note가 있으면 그 안내대로 A/B를 읽는다. 서술(narrative)은 참고만 한다.\n"
    "카드에 없는 사실을 가정하지 말고, 과실비율 숫자를 제안하지 말라. JSON만 출력한다."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    # 이유를 판정보다 먼저 쓰게 한다. 구조화 출력은 이 순서로 생성되므로, 판정이 먼저면 작은
    # 모델(gpt-4o-mini)이 대조하기 전에 판정을 정하고 "일치함"이라 쓰면서 모순을 낸다(11/13 시범)
    "properties": {
        "reasons": {
            "type": "array", "items": {"type": "string"},
            "description": "칸마다 대조한 결과. verdict보다 먼저 쓴다",
        },
        "verdict": {
            "type": "string", "enum": list(VERDICTS),
            "description": "none = 반박 없음(선택한 도표가 맞다). 나머지는 반박 종류",
        },
        "preferred_chart_id": {
            "type": ["string", "null"],
            "description": "verdict가 wrong_chart일 때만 차순위 후보 ID, 아니면 null",
        },
    },
    "required": ["reasons", "verdict", "preferred_chart_id"],
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
    """LLM 응답을 검증한다. 모양이 틀리면 ValueError(추측으로 메우지 않는다).

    판정은 `verdict` 하나다. `none`이면 반박 없음, 그 밖은 반박 종류다. 예전 형식(`holds`·`kind`)은
    받지 않는다. 반환값은 판단 그래프가 쓰는 모양(`holds`·`kind`) 그대로다.
    `wrong_chart`가 차순위 후보가 아닌 도표(고른 도표 자신 등)를 가리키면 다시 고를 수 없으므로
    `preferred_chart_id`를 비워 담당자 확인으로 넘긴다(실행은 멈추지 않는다).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"반박 검토 응답이 JSON이 아니다: {exc}") from exc
    kind = data.get("verdict") if isinstance(data, dict) else None
    if kind not in VERDICTS:
        raise ValueError(f"반박 검토 응답의 verdict가 올바르지 않다: {kind!r}")
    reasons = data.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise ValueError("반박 검토 응답의 reasons가 문자열 목록이 아니다")
    preferred = data.get("preferred_chart_id")
    if kind == NO_REBUTTAL:
        return {"holds": False, "kind": None, "preferred_chart_id": None, "reasons": reasons}
    if not reasons:
        raise ValueError("반박이 성립하는데 이유가 없다")
    if kind == "wrong_chart":
        if preferred not in alternatives:
            shown = preferred or "없음"
            note = f"반박 검토가 차순위 후보가 아닌 도표({shown})를 골라 다시 고르지 않았다"
            return {"holds": True, "kind": "wrong_chart", "preferred_chart_id": None,
                    "reasons": [*reasons, note]}
    else:
        preferred = None
    return {"holds": True, "kind": kind, "preferred_chart_id": preferred, "reasons": reasons}


async def openai_reviewer(system: str, user: str) -> str:
    """기본 검토기. OPENAI_API_KEY·OPENAI_MODEL을 쓰고 JSON 스키마 출력을 강제한다."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise ReviewerNotConfigured("OPENAI_API_KEY가 없어 반박 검토를 실행할 수 없다")
    model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"

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


CLAIM_NAMES = {"contradiction": "모순", "missing_fact": "사실 누락"}
UNCONFIRMED_CLAIM_NOTE = (
    "반박 검토의 '{name}' 주장은 도표 조건과 확인한 사실을 칸마다 대조해 확인되지 않아 "
    "반영하지 않았다"
)


def claim_confirmed(
    kind: str, card: FactCard, selection: ChartSelection, candidates: list[ChartCandidate]
) -> bool | None:
    """'모순'·'사실 누락' 주장이 칸 대조로 확인되는가. 선택 도표가 후보에 없으면 None(따른다).

    대조하는 칸은 선택 도표의 속성과 고른 변형의 조건 중 '*'가 아닌 것, 값은 판단이 읽은 확인한 사실
    (`confirmed_values`, v1 2절 맞바꿈 반영)이다. 모순 = 같은 칸 값이 다르다.
    사실 누락 = 그 칸이 없다.
    """
    chosen = next((c for c in candidates if c["chart_id"] == selection["chart_id"]), None)
    if chosen is None:
        return None
    conditions = {**chosen["attributes"], **(selection["variant"].get("conditions") or {})}
    concrete = {k: v for k, v in conditions.items() if v != ANY_VALUE}
    values = confirmed_values(card)
    if kind == "contradiction":
        return any(k in values and values[k] != v for k, v in concrete.items())
    if kind == "missing_fact":
        return any(k not in values for k in concrete)
    return None


async def review_rebuttal(
    card: FactCard,
    selection: ChartSelection,
    candidates: list[ChartCandidate],
    reviewer: Reviewer | None = None,
) -> tuple[ValidationCheck, str | None]:
    """(검증 결과, wrong_chart일 때 재실행할 도표 ID).

    실패하면 rebuttal_kind를 반드시 채운다. wrong_chart(차순위 후보가 더 맞음)만
    graph.py가 재실행하고, missing_fact(구분 조건 누락)·contradiction(근거와 카드 모순)은
    담당자 질문으로 넘긴다. 단 이 두 주장은 칸 대조(`claim_confirmed`)로 확인되지 않으면 따르지
    않고, 통과시키되 사유 첫 줄에 경고를 남긴다(보고서 1번).
    """
    review = reviewer or openai_reviewer
    raw = await review(SYSTEM_PROMPT, review_payload(card, selection, candidates))
    verdict = parse_verdict(raw, selection["alternatives"])
    kind = verdict["kind"]
    if kind in CLAIM_NAMES and claim_confirmed(kind, card, selection, candidates) is False:
        note = UNCONFIRMED_CLAIM_NOTE.format(name=CLAIM_NAMES[kind])
        return {"layer": "rebuttal", "passed": True, "reasons": [note, *verdict["reasons"]],
                "rebuttal_kind": None}, None
    check: ValidationCheck = {
        "layer": "rebuttal", "passed": not verdict["holds"],
        # 반박 없음이면 모델이 판정 전에 쓴 칸 대조(reasons)는 싣지 않는다. 통과 사유가 길어진다
        "reasons": verdict["reasons"] if verdict["holds"] else [],
        "rebuttal_kind": verdict["kind"],
    }
    return check, verdict["preferred_chart_id"]
