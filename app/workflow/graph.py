"""B: 고정 판단 그래프(LangGraph). 확정 카드 → 범위 → 후보 → 질문/재개 → 선택 → 검증 3겹 → 판정.

- 공식 판단은 이 그래프만 만든다. LLM은 반박 검토(②)에서만 쓰고 비율은 코드로 계산한다.
- 구분 질문은 `interrupt`로 멈춘다. 담당자가 값을 답하면 A의 카드 수정/확정 경로로 새 카드 버전이
  생기고 새 실행이 시작된다(같은 실행을 잇지 않는다). "모른다"고 답하면 같은 실행을 재개해
  보류로 내린다.
- 재개 값은 API 계층이 먼저 검증한다. 재개 뒤 노드가 예외를 내면 LangGraph가 같은 재개 값으로
  다시 실패하므로, 노드 안에서는 담당자 입력으로 예외를 내지 않는다.
- 반박이 wrong_chart면 차순위 도표로 MAX_REBUTTAL_RETRIES 만큼만 재실행한다.
  missing_fact·contradiction과 상한을 넘긴 wrong_chart는 재실행 없이 needs_confirmation으로 내린다.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.schemas.chart_scope import (
    ANY_VALUE,
    EXCLUDED_MODIFIERS,
    FIELD_OPTIONS,
    IN_SCOPE_ACCIDENT_TYPES,
    IN_SCOPE_LOCATION_TYPES,
    MANUAL_ONLY_MODIFIERS,
    REASON_FIELD_NOT_CONFIRMED,
    REASON_NO_REVIEWED_CHART,
    REASON_NO_SHAPE,
    REASON_OUT_OF_SCOPE,
    REASON_SWAP_HINT,
    REQUIRED_CONFIRMED_FIELDS,
    unconfirmed_fields,
)
from app.schemas.facts import FactCard
from app.schemas.judgment import (
    ChartAlternative,
    ConfirmationQuestion,
    JudgmentResult,
    ModifierDecision,
    QuestionAnswer,
    UnknownScenario,
    ValidationCheck,
)
from app.validation.evidence import check_evidence
from app.validation.numerical import calculation_lines, check_numerical
from app.validation.rebuttal import Reviewer, review_rebuttal
from app.workflow.candidates import (
    ChartCandidate,
    ChartVariant,
    confirmed_values,
    korean_value,
    retrieve_from_corpus,
)
from app.workflow.card_fields import (
    UNKNOWN,
    assumed_fields,
    card_field_key,
    chart_fields,
    chart_key,
    chart_view,
    confirmed_unknown_fields,
    is_swapped,
    other_side,
    question_options,
    to_card_terms,
)
from app.workflow.state import ChartSelection, JudgmentState, Outcome

STAGES = (
    "require_confirmed_card",
    "check_chart_scope",
    "retrieve_chart_candidates",
    "ask_or_resume",
    "select_chart_and_modifiers",
    "check_evidence",
    "review_rebuttal",
    "calculate_and_validate_fault",
    "build_judgment_result",
)
MAX_REBUTTAL_RETRIES = 1
# 보고서 3번·보류 사유에 늘어놓는 제외 후보 수. 검색 후보가 넓어서(candidates.CHART_SEARCH_TOP_K)
# 검수 전 도표가 20개 넘게 섞이므로 검색 점수가 높은 앞쪽만 보여 준다
MAX_LISTED_EXCLUDED = 5
MAX_LISTED_UNREVIEWED = 3
# 보류 사유 끝의 도표 A/B 맞바꿈 대조(swap_hints)에 적는 도표 수(남는 도표·가까운 도표 각각)
MAX_LISTED_SWAP = 3
# 실행 입력 버전(judgment_runs). 같은 카드·인정기준·규칙 버전이면 다시 판단하지 않는다.
# 인정기준은 data/corpora/fault_standards/manifest.json의 적용판이다.
# 규칙 버전은 이 그래프·검증 규칙을 바꿀 때 올린다
CRITERIA_VERSION = "fault_standards:2023.6"
# 11/16.1: 교차로 밖 장소(일반 도로·고속도로·주차장·회전교차로)와 그 도표 칸을 범위에 넣었다
# 11/16.2: 표시하지 않은 촬영 차 이륜차·긴급자동차를 '아니오'로 읽는다
# (card_fields.EGO_ASSUMED_FALSE)
# 11/16.3: 도표 후보 검색 깊이 50 → 100(candidates.CHART_SEARCH_TOP_K, 5차 검수 시범 실행)
# 11/16.4: 교차로 밖 같은 방향 직진·정차 두 차의 검색어에 '추돌'(candidates._REAR_END_MOVES)
# 11/16.5: 진로 변경 순서 칸(lane_change_timing, 차43-3·43-5)
# 11/16.6: 고를 도표가 없으면 도표 A/B를 맞바꿔 대조한 결과를 보류 사유 끝에 붙인다(swap_hints)
# 11/16.7: 도표 A/B 당사자를 판단이 정한다(choose_orientation, 판단 시작 때 더 맞는 방향으로 맞바꿈)
# 11/17 모르는 칸 경우별 결과(#277·#281·unknown_scenarios 고침)는 올리지 않았다. 올리면 이미 판단한
# 카드를 그대로 다시 눌렀을 때 반박 검토(LLM)까지 새로 돌아 판정이 달라질 수 있다. 그래서 이미
# 판정이 난 카드는 칸을 고쳐 새 카드 버전으로 판단해야 새 사유가 나온다
RULE_VERSION = "judgment-rules-2026.11.16.7"
QUESTION_NAMESPACE = uuid.UUID("5d1f6c4e-1b2a-4a3c-9e8d-700000000001")

# 질문 문구. 카드 쪽 칸 이름으로 찾는다(v1 맞바꿈이면 담당자가 고칠 카드 칸의 A/B로 묻는다)
FIELD_LABELS = {
    "accident_type": "사고 당사자 유형", "location_type": "사고 장소",
    "road_priority": "도로 관계(동일폭·대로·일시정지가 어느 차 쪽인지)",
    "movement_A": "A 차량의 진행 방향", "movement_B": "B 차량의 진행 방향",
    # 진입 시 신호(signal_*)와 충돌 순간 신호(collision_signal_*)를 담당자가 헷갈리지 않게 적는다
    "signal_A": "A 차량의 진입 시 신호", "signal_B": "B 차량의 진입 시 신호",
    "collision_signal_A": "A 차량의 충돌 순간 신호",
    "collision_signal_B": "B 차량의 충돌 순간 신호",
    "lane_marking_A": "A 차량이 있던 차로의 노면표시(방향 화살표)",
    "lane_marking_B": "B 차량이 있던 차로의 노면표시(방향 화살표)",
    "unprotected_left_sign": "비보호 좌회전 표지 여부",
    "relative_position": "상대 위치(B가 가는 방향에서 볼 때 A가 있던 쪽)",
    "entry_order": "교차로 진입 순서(A 기준)", "night": "야간 여부",
    # 11/16 교차로 밖 도표(차31~61) 칸
    "two_wheeler_A": "A 차량의 이륜차 여부", "two_wheeler_B": "B 차량의 이륜차 여부",
    "emergency_vehicle_A": "A 차량의 긴급자동차(긴급 운행 중) 여부",
    "emergency_vehicle_B": "B 차량의 긴급자동차(긴급 운행 중) 여부",
    "center_line_crossing_A": "A 차량의 중앙선 침범 여부",
    "center_line_crossing_B": "B 차량의 중앙선 침범 여부",
    "door_open_A": "A 차량의 문 열림 여부", "door_open_B": "B 차량의 문 열림 여부",
    "overtake_line": "추월한 곳의 차선(황색 실선·점선, 흰색 실선, 앞지르기 금지 장소)",
    "u_turn_zone": "유턴 구역(상시 유턴·신호 유턴)",
    "u_turn_timing": "뒤차의 유턴 시점(급 유턴·동시 유턴)",
    "lane_change_timing": "두 차의 진로 변경 순서(양옆 차로 동시·같은 차로 뒤차 먼저·앞차 먼저)",
    "lane_reduction": "차로 감소(합류) 여부", "shoulder": "갓길·도로 가장자리 여부",
    "traffic_jam": "정체 여부", "secondary_collision": "2차 사고(앞차가 사고로 정차 중) 여부",
    "bus_stop": "버스정류장 여부", "safety_zone": "안전지대 통과(벗어나기 전·후)",
    "roundabout_lanes": "회전교차로의 회전 차로 수",
    "roundabout_move_A": "A 차량의 회전교차로 진행(회전 중·진입·진출)",
    "roundabout_move_B": "B 차량의 회전교차로 진행(회전 중·진입·진출)",
    "roundabout_lane_A": "A 차량이 충돌 때 있던 회전 차로",
    "roundabout_lane_B": "B 차량이 충돌 때 있던 회전 차로",
}

Retriever = Callable[[FactCard], Awaitable[list[ChartCandidate]]]


class UnconfirmedCardError(ValueError):
    """확정되지 않은 카드가 그래프에 들어왔다. API가 먼저 막아야 하는 프로그래밍 오류다."""


@dataclass(frozen=True)
class Dependencies:
    """바꿔 끼울 외부 의존성. 기본은 C 검색툴·OpenAI 반박 검토다."""

    retrieve: Retriever = retrieve_from_corpus
    reviewer: Reviewer | None = None


def scope_gate(
    fields: dict[str, dict[str, object]], card: FactCard | None = None
) -> tuple[str | None, list[str]]:
    """(보류 사유, 질문할 칸). 범위 밖이면 보류, 범위 안이면 미확인 칸을 질문으로 넘긴다.

    docs/contracts/mvp-chart-scope.md 범위. 값은 human_confirmed 인 것만 본다.
    카드 칸·값은 도표 선택 칸·값으로 바꿔 본다(`card_fields`, 카드를 주면 v1 2절 맞바꿈 포함).
    당사자 3명 이상(`party_count`)은
    범위 밖이다. 사고 유형·장소를 `불명`으로 확인했으면 다시 묻지 않고 보류한다.
    """
    def confirmed(source: dict, key: str) -> object:
        f = source.get(key, {})
        return f.get("value") if f.get("verification_status") == "human_confirmed" else None

    parties = confirmed(fields, "party_count")
    if isinstance(parties, int) and not isinstance(parties, bool) and parties >= 3:
        return REASON_OUT_OF_SCOPE, []
    chart = chart_view(card) if card is not None else chart_fields(fields)  # type: ignore[arg-type]
    accident_type = confirmed(chart, "accident_type")
    location_type = confirmed(chart, "location_type")
    if accident_type is None or location_type is None:
        return None, unconfirmed_fields(chart, "other")  # type: ignore[arg-type]
    scope = ((accident_type, IN_SCOPE_ACCIDENT_TYPES), (location_type, IN_SCOPE_LOCATION_TYPES))
    if any(value != UNKNOWN and value not in allowed for value, allowed in scope):
        return REASON_OUT_OF_SCOPE, []
    unknown = [key for key, value in (("accident_type", accident_type),
                                      ("location_type", location_type)) if value == UNKNOWN]
    if unknown:
        return REASON_FIELD_NOT_CONFIRMED.format(field_key=", ".join(unknown)), []
    return None, unconfirmed_fields(chart, str(location_type))  # type: ignore[arg-type]


def initial_state(
    card: FactCard, criteria_version: str, rule_version: str, judgment_version: int | None = None
) -> JudgmentState:
    return {
        "case_id": card["case_id"], "card": card, "criteria_version": criteria_version,
        "rule_version": rule_version, "judgment_version": judgment_version,
        "rebuttal_retry_count": 0, "evidence": [], "questions": [], "checks": [],
        # 카드에서 `불명`으로 확인한 칸은 처음부터 '모름' 답처럼 다룬다
        "candidates": [], "unknown_fields": confirmed_unknown_fields(card),
        "excluded_chart_ids": [],
        "selection": None, "outcome": None,
    }


def thread_config(thread_id: str) -> RunnableConfig:
    """체크포인트 스레드 = judgment_runs.id."""
    return {"configurable": {"thread_id": thread_id}}


class RunOutcome(TypedDict):
    """그래프 호출 한 번의 결과. 질문으로 멈췄거나(result 없음) 판정이 났다."""

    status: Literal["waiting_for_confirmation", "completed"]
    stage: str
    rebuttal_retry_count: int
    questions: list[ConfirmationQuestion]
    result: JudgmentResult | None


def run_outcome(values: dict) -> RunOutcome:
    """`ainvoke` 반환값 → RunOutcome. 질문으로 멈추면 LangGraph가 `__interrupt__`를 붙인다."""
    retries = int(values.get("rebuttal_retry_count", 0))
    interrupts = values.get("__interrupt__") or []
    if interrupts:
        return {
            "status": "waiting_for_confirmation", "stage": "ask_or_resume",
            "rebuttal_retry_count": retries, "questions": interrupts[0].value["questions"],
            "result": None,
        }
    result = values.get("result")
    if result is None:
        raise RuntimeError("판단 그래프가 질문도 결과도 없이 끝났다")
    return {
        "status": "completed", "stage": "build_judgment_result",
        "rebuttal_retry_count": retries, "questions": [], "result": result,
    }


def check_attempts(checks: list[ValidationCheck]) -> list[int]:
    """검증 결과마다 시도 번호(0 첫 도표, 1 차순위 재실행) — validation_checks.attempt.

    도표 오선택 반박이 성립하면 그다음 검증부터 다음 시도다. 재실행 상한이 1이라 1을 넘지 않는다.
    """
    attempts: list[int] = []
    attempt = 0
    for check in checks:
        attempts.append(attempt)
        wrong_chart = check.get("rebuttal_kind") == "wrong_chart"
        if check["layer"] == "rebuttal" and not check["passed"] and wrong_chart:
            attempt += 1
    return attempts


def question_id(thread_id: str, field_key: str) -> str:
    """같은 실행·같은 칸이면 재개 뒤에도 같은 ID(judgment_questions.id로 그대로 저장한다)."""
    return str(uuid.uuid5(QUESTION_NAMESPACE, f"{thread_id}:{field_key}"))


def build_question(
    thread_id: str, field_key: str, card: FactCard | None = None
) -> ConfirmationQuestion:
    """field_key는 도표 선택 칸이다. 카드가 v1 형식이면 v1 칸 이름·값으로 묻는다.

    값 답은 질문의 칸 이름·값 그대로 A의 카드 수정으로 가므로 카드와 형식이 같아야 한다.
    question_id는 도표 선택 칸으로 만든다(카드 형식과 상관없이 같은 실행·같은 칸이면 같다).
    문구는 카드 쪽 칸으로 쓴다. v1 맞바꿈이면 도표 `movement_A`를 카드 `movement_B`로 묻는다.
    """
    card_key = card_field_key(field_key, card) if card else field_key
    return {
        "question_id": question_id(thread_id, field_key),
        "field_key": card_key,
        "text": f"{FIELD_LABELS.get(chart_key(card_key), field_key)}을(를) 확인해 주세요.",
        "options": question_options(field_key, card) if card
        else list(FIELD_OPTIONS.get(field_key, ())),
        "allow_unknown": True,
    }


def _eligible(state: JudgmentState) -> list[ChartCandidate]:
    excluded = set(state.get("excluded_chart_ids", []))
    return [
        c for c in state.get("candidates", [])
        if c["calculation_ready"] and c["chart_id"] not in excluded
    ]


def _required_fields(card: FactCard) -> tuple[str, ...]:
    location = confirmed_values(card).get("location_type", "other")
    return REQUIRED_CONFIRMED_FIELDS.get(location, REQUIRED_CONFIRMED_FIELDS["other"])  # type: ignore[arg-type]


def _concrete(conditions: dict[str, str]) -> dict[str, str]:
    """`*`(무관)을 뺀, 실제로 도표를 가르는 칸과 값."""
    return {k: v for k, v in conditions.items() if v != ANY_VALUE}


def _needed_fields(candidate: ChartCandidate, values: dict[str, str]) -> list[str]:
    """이 도표를 고르는 데 값이 필요한 칸(속성·변형 조건에서 `*` 제외). 나온 순서를 지킨다.

    변형 조건은 카드와 아직 어긋나지 않는 변형(`_variant_fits`)의 것만 센다. 이미 어긋난 변형은
    더 묻는다고 다시 고를 수 있게 되지 않는다. 예: 차2-2는 녹색 좌회전 변형만 비보호 표지로
    갈리므로, 적색 좌회전 카드에는 표지를 묻지 않는다.
    """
    keys = list(_concrete(candidate["attributes"]))
    for variant in candidate["base_variants"]:
        if _variant_fits(variant, values):
            keys += [k for k in _concrete(variant["conditions"]) if k not in keys]
    return keys


def _variant_fits(variant: ChartVariant, values: dict[str, str]) -> bool:
    """확인한 값과 어긋나지 않는 변형인가. 아직 확인하지 않은 칸은 어긋남으로 보지 않는다."""
    return all(values.get(k, v) == v for k, v in _concrete(variant["conditions"]).items())


def _matched_fields(candidate: ChartCandidate, values: dict[str, str]) -> list[str]:
    """확인한 값과 일치하는 칸(`*` 제외). 변형 조건은 카드와 맞는 변형 중 가장 많이 맞는 것을
    센다."""
    matched = [k for k in _concrete(candidate["attributes"]) if k in values]
    best: list[str] = []
    for variant in candidate["base_variants"]:
        if _variant_fits(variant, values):
            hits = [k for k in _concrete(variant["conditions"]) if k in values and k not in matched]
            if len(hits) > len(best):
                best = hits
    return matched + best


def _mismatch(candidate: ChartCandidate, values: dict[str, str]) -> list[str]:
    """확인한 값과 어긋난 도표 속성 칸(`*` 제외). 장소를 변형마다 적은 도표는 장소도 본다."""
    attributes = _concrete(candidate["attributes"])
    mismatch = [k for k, v in attributes.items() if k in values and values[k] != v]
    if "location_type" not in attributes and _other_places(candidate, values):
        mismatch.append("location_type")
    return mismatch


def _differs(candidate: ChartCandidate, values: dict[str, str]) -> list[str]:
    """카드와 다른 도표 칸 전부: 속성 + 가장 덜 어긋나는 기본 과실 변형의 조건(`*` 제외).

    이 칸들을 고치면 카드로 고를 수 있다(모름·미정의 칸은 `_ruled_out`이 따로 본다).
    """
    mismatch = _mismatch(candidate, values)
    conflicts = [[k for k, x in _concrete(v["conditions"]).items()
                  if k in values and values[k] != x and k not in mismatch]
                 for v in candidate["base_variants"]]
    return mismatch + min(conflicts, key=len, default=[])


def _ruled_out(
    candidate: ChartCandidate, values: dict[str, str], unknown: set[str], required: set[str],
) -> str | None:
    """확인한 값이나 '모른다' 답 때문에 이미 고를 수 없는 후보면 그 이유, 아직 고를 수 있으면 None.

    `*`은 어떤 값과도 맞는다. 속성과 기본 과실 변형을 모두 대조한다. 더 묻는다고 다시 고를 수 있게
    되지 않으므로 질문(`open_fields`)과 순위(`_rank`)가 같은 판정을 쓴다.
    """
    # 장소처럼 카드와 이미 어긋난 칸을 먼저 본다. 다른 장소 도표는 그 장소의 필수 칸이 없어도
    # "미정의"가 아니라 "불일치"로 보고하는 편이 정확하다
    mismatch = _mismatch(candidate, values)
    if mismatch:
        return f"카드와 불일치 {mismatch}"
    keys = set(candidate["attributes"])
    for variant in candidate["base_variants"]:
        keys |= set(variant["conditions"])
    if not required <= keys:
        return f"구분 칸 {sorted(required - keys)} 미정의"
    attributes = _concrete(candidate["attributes"])
    unverifiable = [k for k in attributes if k not in values and k in unknown]
    if unverifiable:
        return f"확인 불가 칸 {unverifiable}"
    fitting = [v for v in candidate["base_variants"] if _variant_fits(v, values)]
    if not fitting:
        conflict = sorted({k for v in candidate["base_variants"]
                           for k, x in _concrete(v["conditions"]).items()
                           if k in values and values[k] != x})
        return f"카드와 맞는 기본 과실 변형 없음 {conflict}"
    blocked = [{k for k in _concrete(v["conditions"]) if k not in values and k in unknown}
               for v in fitting]
    if all(blocked):
        return f"확인 불가 칸 {sorted(set().union(*blocked))}"
    return None


def _other_places(candidate: ChartCandidate, values: dict[str, str]) -> bool:
    """장소를 변형마다 적은 도표(일반 도로·고속도로 공통 등)가 카드 장소와 맞는 변형이 없는가.

    11/16 검수 규칙: 장소가 여럿인 도표는 장소마다 같은 비율 변형으로 나눈다. 장소를 적지 않은
    변형이나 `*`가 있으면 장소로는 가르지 않는다.
    """
    place = values.get("location_type")
    places = {v["conditions"].get("location_type") for v in candidate["base_variants"]}
    return (place is not None and bool(places) and not places & {None, ANY_VALUE}
            and place not in places)


def open_fields(state: JudgmentState) -> list[str]:
    """아직 묻지 않은 구분 칸: 범위 필수 칸 + 아직 고를 수 있는 후보 도표가 가르는 칸.

    확인됐거나 '모른다'면 뺀다. 카드와 이미 어긋난 후보(장소·신호·진행 방향이 다른 도표)가 가르는
    칸은 묻지 않는다. 남은 후보가 모두 `*`(무관)로 둔 필수 칸도 묻지 않는다.
    수정요소는 자동 적용하지 않으므로 수정요소 조건 칸도 묻지 않는다.
    """
    card = state["card"]
    values = confirmed_values(card)
    unknown = set(state.get("unknown_fields", []))
    required = set(_required_fields(card))
    needed = [_needed_fields(c, values) for c in _eligible(state)
              if _ruled_out(c, values, unknown, required) is None]
    ordered = [k for k in _required_fields(card) if not needed or any(k in n for n in needed)]
    for keys in needed:
        ordered += keys
    result: list[str] = []
    for key in ordered:
        if key in FIELD_OPTIONS and key not in values and key not in unknown and key not in result:
            result.append(key)
    return result


def _rank(state: JudgmentState) -> tuple[list[ChartCandidate], list[tuple[str, str]]]:
    """확인한 칸과 모순 없는 검수 후보를 일치 칸 수·검색 점수로 정렬한다.

    `*`은 어떤 값과도 맞는다. 속성과 기본 과실 변형을 모두 대조해(`_ruled_out`), 카드로 고를 수
    있는 변형이 없는 도표는 순위에 올리지 않는다(1순위가 변형에서 막혀 차순위를 보지 못하는 일을
    막는다). (순위, [(도표, 제외 이유)]). 제외 목록은 검색 점수 순이다.
    """
    card = state["card"]
    values = confirmed_values(card)
    unknown = set(state.get("unknown_fields", []))
    required = set(_required_fields(card))
    ranked: list[tuple[int, float, ChartCandidate]] = []
    skipped: list[tuple[str, str]] = []
    for candidate in _eligible(state):
        why = _ruled_out(candidate, values, unknown, required)
        if why:
            skipped.append((candidate["chart_id"], why))
            continue
        ranked.append((len(_matched_fields(candidate, values)), candidate["score"], candidate))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    ordered = [c for _, _, c in ranked]
    preferred = state.get("preferred_chart_id")
    if preferred and any(c["chart_id"] == preferred for c in ordered):
        ordered.sort(key=lambda c: c["chart_id"] != preferred)
    return ordered, skipped


def _flipped_card(card: FactCard) -> FactCard | None:
    """도표 A/B 당사자를 맞바꾼 카드 사본. 맞바꿔도 읽는 방향이 같으면 None.

    카드 v1이 아니거나 촬영 차를 모르면 `is_swapped`가 늘 거짓이라 맞바꿔 읽을 수 없다.
    """
    mapping = card["party_mapping"]
    flipped: FactCard = {**card, "party_mapping": {
        **mapping, "chart_party_A_id": mapping["chart_party_B_id"],
        "chart_party_B_id": mapping["chart_party_A_id"],
    }}
    return flipped if is_swapped(flipped) != is_swapped(card) else None


def _orientation_score(card: FactCard, candidates: list[ChartCandidate]) -> tuple[int, int]:
    """그 방향(도표 A/B 매핑)으로 카드를 읽었을 때 도표가 얼마나 맞는가. 클수록 낫다.

    고를 수 있는 도표가 남으면 (2, 1순위 도표의 일치 칸 수), 없으면 (1, -가장 가까운 도표의 다른
    칸 수), 견줄 도표가 없으면 (0, 0). 가까운 도표는 장소가 다른 도표를 빼고 센다(`swap_hints`와
    같다).
    """
    state: JudgmentState = initial_state(card, "", "")
    state["candidates"] = candidates
    values = confirmed_values(card)
    ranked, _ = _rank(state)
    if ranked:
        return 2, len(_matched_fields(ranked[0], values))
    near = [len(left) for c in _eligible(state)
            if "location_type" not in (left := _differs(c, values))]
    return (1, -min(near)) if near else (0, 0)


async def choose_orientation(card: FactCard, retrieve: Retriever) -> FactCard:
    """도표 A/B 당사자를 판단이 정한다. 맞바꾼 쪽이 더 맞으면 맞바꾼 카드 사본, 아니면 그대로 준다.

    도표마다 A의 역할이 달라(예: 진로변경 도표는 뒤에서 직진한 차가 A) 도표를 모르는 담당자가 맞히기
    어렵다. 그래서 두 방향 모두 후보를 검색해 대조하고 더 맞는 쪽을 쓴다(`_orientation_score`).
    점수가 같으면 지금 방향을 둔다. 맞바꿔 읽을 수 없는 카드(v1이 아니거나 촬영 차를 모름)와 범위
    밖 카드는 검색하지 않고 그대로 돌려준다. 저장은 호출한 쪽(판단 서비스)이 한다.
    """
    flipped = _flipped_card(card)
    if flipped is None or scope_gate(card["fields"], card)[0]:  # type: ignore[arg-type]
        return card
    now = _orientation_score(card, await retrieve(card))
    then = _orientation_score(flipped, await retrieve(flipped))
    return flipped if then > now else card


def _covers_moves(candidate: ChartCandidate, move_a: str, move_b: str) -> bool:
    """도표 A가 move_a, 도표 B가 move_b인 사고를 다루는 변형이 있는가(`*`·안 적은 칸은 무엇이든)."""
    attributes = _concrete(candidate["attributes"])
    for variant in candidate["base_variants"] or [{"conditions": {}}]:
        moves = {**attributes, **_concrete(variant["conditions"])}  # type: ignore[arg-type]
        if moves.get("movement_A", move_a) == move_a and moves.get("movement_B", move_b) == move_b:
            return True
    return False


def no_shape_lines(state: JudgmentState) -> list[str]:
    """고를 도표가 없을 때, 카드의 두 차 움직임 조합을 다루는 후보 도표가 없으면 그 안내 한 줄.

    "카드와 불일치" 줄만 보면 칸을 고치면 될 것처럼 읽힌다. 움직임 조합부터 맞는 도표가 없으면
    (예: 후진 대 정차) 인정기준에 그 사고 모양이 없을 가능성이 높으므로 먼저 알린다. 도표 A/B는
    어느 쪽이든 본다(맞바꾼 조합 포함). 두 움직임 중 확인하지 않은 것이 있으면 말하지 않는다.
    후보는 검색된 도표뿐이라 "인정기준에 없다"고 단정하지 않는다.
    """
    values = confirmed_values(state["card"])
    move_a, move_b = values.get("movement_A"), values.get("movement_B")
    candidates = _eligible(state)
    if not move_a or not move_b or not candidates:
        return []
    if any(_covers_moves(c, move_a, move_b) or _covers_moves(c, move_b, move_a)
           for c in candidates):
        return []
    return [REASON_NO_SHAPE.format(pair=f"{korean_value(move_a)} 대 {korean_value(move_b)}")]


def _titled(candidate: ChartCandidate) -> str:
    title = candidate.get("title")
    return f"{candidate['chart_id']} 「{title}」" if title else candidate["chart_id"]


def swap_hints(state: JudgmentState) -> list[str]:
    """고를 도표가 없을 때 도표 A/B 당사자를 맞바꿔 다시 대조한 결과. 보류 사유 끝에 붙인다.

    도표 A는 담당자가 정한다(카드 v1 2절). 그런데 도표가 정해지기 전에는 어느 차가 A인지 알기
    어렵다(예: 진로변경 도표는 뒤에서 직진한 차가 A다). 그래서 맞바꾸면 남는 후보 도표와, 맞바꾸면
    카드와 덜 어긋나는 도표 중 가장 가까운 것을 다른 칸과 함께 알린다. 판정은 바꾸지 않는다.
    - 가까운 도표는 맞바꿔서 다른 칸이 줄어드는 도표다. 장소가 다른 도표는 뺀다(장소는 쪽이 없어
      맞바꿔도 그대로다).
    - 칸 이름은 다른 사유 줄처럼 지금 방향의 도표 선택 칸으로 적는다(FE가 매핑으로 카드 칸 이름을
      붙인다).
    """
    card = state["card"]
    flipped = _flipped_card(card)
    if flipped is None:
        return []
    unknown = [other_side(k) for k in state.get("unknown_fields", [])]
    remaining, _ = _rank({**state, "card": flipped, "unknown_fields": unknown})
    lines: list[str] = []
    if remaining:
        charts = ", ".join(_titled(c) for c in remaining[:MAX_LISTED_SWAP])
        lines.append(f"{REASON_SWAP_HINT} 남는 도표: {charts}")
    values, flipped_values = confirmed_values(card), confirmed_values(flipped)
    kept = {c["chart_id"] for c in remaining}
    closer: list[tuple[int, float, ChartCandidate, list[str]]] = []
    for candidate in _eligible(state):
        left = _differs(candidate, flipped_values)
        if (candidate["chart_id"] in kept or not left or "location_type" in left
                or len(left) >= len(_differs(candidate, values))):
            continue
        closer.append((len(left), -candidate["score"], candidate, left))
    fewest = min((item[0] for item in closer), default=0)
    nearest = sorted((item for item in closer if item[0] == fewest), key=lambda item: item[1])
    lines += [f"{REASON_SWAP_HINT} {_titled(c)}: 카드와 불일치 {[other_side(k) for k in left]}"
              for _, _, c, left in nearest[:MAX_LISTED_SWAP]]
    return lines


def _pick_variant(candidate: ChartCandidate, values: dict[str, str]) -> tuple[object, list[str]]:
    """조건이 모두 확인된 변형을 고른다(`*`은 무관). 없으면 (None, 확인해야 할 칸).

    확인해야 할 칸은 카드와 아직 어긋나지 않는 변형의 칸만 센다(`_needed_fields`와 같다).
    """
    missing: list[str] = []
    for variant in candidate["base_variants"]:
        conditions = _concrete(variant["conditions"])
        if all(values.get(k) == v for k, v in conditions.items()):
            return variant, []
        if _variant_fits(variant, values):
            missing += [k for k in conditions if k not in values and k not in missing]
    return None, missing


def _unresolved_modifiers(candidate: ChartCandidate) -> list[tuple[object, str]]:
    """MVP는 수정요소를 자동 적용하지 않는다(`chart_scope` 주석). 모두 담당자 판단으로 넘긴다."""
    unresolved: list[tuple[object, str]] = []
    for modifier in candidate["modifiers"]:
        if modifier["code"] in EXCLUDED_MODIFIERS or modifier["code"] in MANUAL_ONLY_MODIFIERS:
            unresolved.append((modifier, "자동 적용 제외(담당자 입력 전용)"))
        else:
            unresolved.append((modifier, "자동 적용하지 않음(담당자 판단)"))
    return unresolved


def modifier_decisions(
    candidate: ChartCandidate, unresolved: list[tuple[object, str]],
) -> list[ModifierDecision]:
    """보고서 5번. 도표 레코드 순서대로, 가감치가 있는 요소만 담는다.

    자동 적용하지 않으므로 모두 불명(담당자 판단)이다. 가감치가 없는(비적용) 요소는 이 도표의
    대상이 아니다.
    """
    why_by_id = {m["modifier_id"]: why for m, why in unresolved}  # type: ignore[index]
    decisions: list[ModifierDecision] = []
    for modifier in candidate["modifiers"]:
        if modifier["delta"] is None:
            continue
        decisions.append({
            "modifier_id": modifier["modifier_id"], "name": modifier["name"],
            "applies_to": modifier["party"], "delta": modifier["delta"], "decision": "불명",
            "evidence_fields": list(dict.fromkeys(modifier["conditions"] or {})),  # type: ignore[typeddict-item]
            "reason": why_by_id.get(modifier["modifier_id"], "판정하지 않음"),
        })
    return decisions


def alternative_reasons(
    state: JudgmentState, best: ChartCandidate, others: list[ChartCandidate],
    skipped: list[tuple[str, str]], values: dict[str, str],
) -> list[ChartAlternative]:
    """보고서 3번 차순위 후보.

    반박으로 뺀 도표 → 차순위 → 카드와 맞지 않는 후보(앞 MAX_LISTED_EXCLUDED개) → 검수 전
    레코드(앞 MAX_LISTED_UNREVIEWED개) 순이다. 제외 후보는 검색 점수 순으로 자른다.
    """
    out: list[ChartAlternative] = []
    rebutted = [ch for ch in state["checks"] if ch["layer"] == "rebuttal" and not ch["passed"]
                and ch.get("rebuttal_kind") == "wrong_chart"]
    for chart_id, check in zip(state.get("excluded_chart_ids", []), rebutted, strict=False):
        reason = "; ".join(check["reasons"]) or "도표 오선택"
        out.append({"chart_id": chart_id, "reason": f"반박 검토로 제외 — {reason}"})
    best_matched = len(_matched_fields(best, values))
    for candidate in others:
        matched = len(_matched_fields(candidate, values))
        if matched < best_matched:
            why = f"확인한 칸 {matched}개 일치(선택 도표 {best_matched}개)"
        elif matched == best_matched:
            why = "확인한 칸 일치 수가 같고 검색 순위가 낮음"
        else:
            why = "반박 검토가 고른 도표를 우선함"
        out.append({"chart_id": candidate["chart_id"], "reason": f"차순위 — {why}"})
    out += [{"chart_id": chart_id, "reason": f"제외 — {why}"}
            for chart_id, why in skipped[:MAX_LISTED_EXCLUDED]]
    unreviewed = [c for c in state.get("candidates", []) if not c["calculation_ready"]]
    out += [{"chart_id": c["chart_id"], "reason": "제외 — 검수 전 도표 레코드"}
            for c in unreviewed[:MAX_LISTED_UNREVIEWED]]
    return out


# 모르는 칸 경우별 결과: 결과에 이만큼까지 담고, 사유에는 앞 몇 줄만 적는다
MAX_SCENARIOS = 60
MAX_LISTED_SCENARIOS = 8
# 안내 문장에 이름을 적는 모르는 칸 수. 넘으면 "외 N칸"으로 줄인다
MAX_LISTED_UNKNOWN = 4
# 경우별 결과 안내 문장. FE는 결과의 scenarios를 표로 그리고, 이 문장부터 끝까지의 글 줄은 그리지
# 않는다("모른다고 답한 칸("으로 찾는다) — 이 줄들 뒤에 다른 사유를 붙이지 않는다
# "어느 값이어도 같다"고 적지 않는다 — 경우는 도표가 있는 값뿐이라, 경우가 하나(예: 차로 감소 '예'일
# 때만 차43-1)여도 비율은 하나다. 그 밖의 값은 고를 도표가 없다(2026-11-17 운영 사건)
SCENARIO_SAME = (
    "모른다고 답한 칸({labels})이 아래 값이면 고를 도표가 있고, 그 경우는 모두 기본 과실이 "
    "A {a} : B {b}예요(검색된 검수 도표 기준, 수정요소 전). 경우별 도표:"
)
SCENARIO_DIFFERENT = (
    "모른다고 답한 칸({labels})은 값마다 기본 과실이 달라요. 아래에서 맞는 경우를 확인해 주세요"
    "(수정요소 전):"
)
# 범위를 정하는 칸이라 경우별로 넣어 보지 않는다(차대차·장소는 범위 게이트가 본다)
_NO_SCENARIO_KEYS = frozenset({"accident_type", "location_type"})
_FIELD_ORDER = {key: i for i, key in enumerate(FIELD_OPTIONS)}

# 사유에 적는 값 낱말(도표 A/B 기준). 검색 낱말(candidates.korean_value)은 원문 제목 표현이라
# 좌우(둘 다 '측면')·대로 쪽(둘 다 '대로 소로')을 가르지 않고, 11/13·11/16 칸의 값은 없는 것이 많다.
# 칸마다 뜻이 다른 값(동시 = 동시 진입·동시 유턴·양옆 동시)도 있어 칸별로 둔다
_LANE_MARKING_LABELS = {
    "left_only": "좌회전 전용", "straight_left": "직진·좌회전", "straight_only": "직진 전용",
    "straight_right": "직진·우회전", "right_only": "우회전 전용", "none": "표시 없음",
}
_ROUNDABOUT_MOVE_LABELS = {"circulating": "회전 중", "entering": "진입", "exiting": "진출"}
_ROUNDABOUT_LANE_LABELS = {"inner": "안쪽(회전 1차로)", "outer": "바깥쪽(회전 2차로 이상)"}
VALUE_LABELS: dict[str, dict[str, str]] = {
    "movement_A": {"other": "그 밖"}, "movement_B": {"other": "그 밖"},
    "road_priority": {
        "major_A": "A 쪽이 대로", "major_B": "B 쪽이 대로", "stop_A": "A 쪽 일시정지",
        "stop_B": "B 쪽 일시정지", "other": "한쪽만 신호등", "none": "해당 없음",
    },
    "relative_position": {
        "left": "왼쪽", "right": "오른쪽", "opposite": "맞은편",
        "same_direction_front": "같은 방향 앞", "same_direction_rear": "같은 방향 뒤",
    },
    "entry_order": {"none": "해당 없음"},
    "lane_marking_A": _LANE_MARKING_LABELS, "lane_marking_B": _LANE_MARKING_LABELS,
    "overtake_line": {
        "yellow_solid": "황색 실선", "yellow_dashed": "황색 점선", "white_solid": "흰색 실선",
        "no_passing_zone": "앞지르기 금지 장소", "none": "차선 없음",
    },
    "u_turn_zone": {"always": "상시 유턴", "signal": "신호 유턴", "none": "유턴 구역 아님"},
    "u_turn_timing": {"sudden": "급 유턴", "simultaneous": "동시 유턴"},
    "lane_change_timing": {
        "simultaneous": "양옆 차로 동시", "follower_first": "같은 차로 뒤차 먼저",
        "leader_first": "같은 차로 앞차 먼저",
    },
    "safety_zone": {
        "before_leaving": "벗어나기 전", "after_leaving": "벗어난 뒤", "none": "안전지대 없음",
    },
    "roundabout_lanes": {"one": "1차로", "two_plus": "2차로 이상"},
    "roundabout_move_A": _ROUNDABOUT_MOVE_LABELS, "roundabout_move_B": _ROUNDABOUT_MOVE_LABELS,
    "roundabout_lane_A": _ROUNDABOUT_LANE_LABELS, "roundabout_lane_B": _ROUNDABOUT_LANE_LABELS,
}


def value_label(key: str, value: str) -> str:
    """도표 선택 칸의 값을 사유에 적을 낱말로. 참거짓 칸은 예·아니오다."""
    if value in VALUE_LABELS.get(key, {}):
        return VALUE_LABELS[key][value]
    if FIELD_OPTIONS.get(key) == ("true", "false"):
        return {"true": "예", "false": "아니오"}.get(value, value)
    return korean_value(value)


def unknown_scenarios(state: JudgmentState) -> list[UnknownScenario]:
    """담당자가 '모른다'고 한 칸 때문에 도표를 못 고를 때, 그 칸이 어떤 값이면 어느 도표인지.

    담당자에게 경우별 기본 과실을 보여 주려는 것이다. 판단은 이 값으로 도표를 고르지 않는다(어느
    경우인지 확인해 카드에 넣으면 그 값으로 다시 판단한다).
    - 카드에서 확인한 값과 어긋나지 않는 검수 후보마다(검색 점수 순) 그 도표·변형이 요구하는 값 중
      아직 확인하지 못한 것만 한 경우로 적는다. 그 도표가 쓰지 않는 칸은 넣지 않는다.
      모르는 칸 전부의 값 조합을 넣어 보면 칸이 많을 때 앞쪽 조합이 모두 같은 도표가 된다
      (2026-11-17 운영: 모르는 칸 15개, 조합 40개가 모두 차44-1 20:80이고 한 줄이 400자를 넘었다.
      그 40개만 보면 "어느 값이어도 20:80"인데 실제로는 50:50·0:100·100:0 도표가 더 있었다).
    - 그 값을 카드에 넣었을 때 판단이 고를 도표는 판단과 같은 순위(`_ruled_out` → 일치 칸 수 →
      검색 점수)와 `_pick_variant`로 다시 구한다. 남은 모르는 칸은 모르는 채로 둔다.
    - '모른다'고 한 칸이 하나도 걸리지 않은 도표는 경우가 아니다(모름 때문에 빠진 도표가 아니다).
    - 경우 하나는 칸마다 값 하나다. 값만 다른 경우를 한 줄로 묶는 것은 보여 주는 쪽이 한다
      (`scenario_lines`, FE 표). 후보는 검색된 도표뿐이라 모든 경우는 아니다.
    같은 도표의 경우는 이어서 나오고, 도표 순서는 처음 나온 순서(검색 점수 순)다.
    """
    card = state["card"]
    values = confirmed_values(card)
    unknown = set(state.get("unknown_fields", [])) - _NO_SCENARIO_KEYS
    if not unknown:
        return []
    required = set(_required_fields(card))
    eligible = _eligible(state)
    tried: set[tuple[tuple[str, str], ...]] = set()
    out: list[UnknownScenario] = []
    for candidate in eligible:
        if _ruled_out(candidate, values, set(), required) is not None:
            continue  # 모르는 칸과 상관없이 카드와 이미 어긋난 도표
        attributes = _concrete(candidate["attributes"])
        for variant in candidate["base_variants"]:
            if not _variant_fits(variant, values):
                continue
            needed = {**attributes, **_concrete(variant["conditions"])}
            guess = {k: needed[k] for k in sorted(needed, key=lambda k: _FIELD_ORDER.get(k, 99))
                     if k not in values}
            key = tuple(guess.items())
            if key in tried or not guess.keys() & unknown or guess.keys() & _NO_SCENARIO_KEYS:
                continue
            tried.add(key)
            trial = {**values, **guess}
            ranked = sorted(
                (c for c in eligible
                 if _ruled_out(c, trial, unknown - guess.keys(), required) is None),
                key=lambda c: (-len(_matched_fields(c, trial)), -c["score"]),
            )
            chosen = _pick_variant(ranked[0], trial)[0] if ranked else None
            if chosen is None:
                continue
            best = ranked[0]
            out.append({
                "fields": guess,
                "card_fields": dict(to_card_terms(k, v, card) for k, v in guess.items()),
                "chart_id": best["chart_id"], "title": best.get("title") or "",
                "variant": chosen["variant"], "base_A": chosen["base_A"],  # type: ignore[index]
                "base_B": chosen["base_B"],  # type: ignore[index]
            })
    first_seen: dict[str, int] = {}
    for i, scenario in enumerate(out):
        first_seen.setdefault(scenario["chart_id"], i)
    out.sort(key=lambda scenario: first_seen[scenario["chart_id"]])
    return out


Box = dict[str, tuple[str, ...]]


def _merged(boxes: list[Box]) -> list[Box]:
    """한 칸의 값만 다른 경우를 값 목록 하나로 합친다. 더 합칠 것이 없을 때까지 되풀이한다.

    같은 도표·변형·비율로 가는 경우끼리만 넘긴다. 예: 상대 위치가 왼쪽·오른쪽으로만 갈린 두 경우는
    '왼쪽 또는 오른쪽' 한 줄이 된다. 다른 칸이 모두 같을 때만 합치므로, 합친 값은 어느 조합이든
    그 도표다. FE 표도 같은 규칙으로 묶는다(src/lib/facts/card.ts scenarioRows).
    """
    boxes = [dict(box) for box in boxes]
    again = True
    while again:
        again = False
        for i, first in enumerate(boxes):
            for j in range(i + 1, len(boxes)):
                second = boxes[j]
                if first.keys() != second.keys():
                    continue
                differs = [k for k in first if set(first[k]) != set(second[k])]
                if len(differs) == 1:
                    key = differs[0]
                    first[key] = tuple(dict.fromkeys(first[key] + second[key]))
                    del boxes[j]
                    again = True
                    break
            if again:
                break
    return boxes


def _in_option_order(key: str, values: tuple[str, ...]) -> list[str]:
    options = FIELD_OPTIONS.get(key, ())
    return sorted(values, key=lambda v: options.index(v) if v in options else len(options))


def scenario_lines(scenarios: list[UnknownScenario]) -> list[str]:
    """사유에 붙일 경우별 결과 줄(앞 MAX_LISTED_SCENARIOS개). 칸·값·비율은 도표 A/B 기준이다.

    도표·변형·비율이 같고 한 칸의 값만 다른 경우는 한 줄로 묶어 적는다(`_merged`).
    """
    groups: dict[tuple[str, str, str | None, int, int], list[Box]] = {}
    for s in scenarios:
        chart = f"{s['chart_id']} 「{s['title']}」" if s.get("title") else s["chart_id"]
        groups.setdefault(
            (s["chart_id"], chart, s["variant"], s["base_A"], s["base_B"]), []
        ).append({k: (v,) for k, v in s["fields"].items()})
    lines = []
    for (_, chart, variant, base_a, base_b), boxes in groups.items():
        for box in _merged(boxes):
            guess = ", ".join(
                f"{FIELD_LABELS.get(k, k)} "
                f"{' 또는 '.join(value_label(k, v) for v in _in_option_order(k, vs))}"
                for k, vs in box.items()
            )
            label = f" ({variant})" if variant else ""
            lines.append(f"{guess} → {chart}{label} A {base_a} : B {base_b}")
    return _listed(lines, MAX_LISTED_SCENARIOS, "경우")


def scenario_outcome(scenarios: list[UnknownScenario], unknown: list[str]) -> dict:
    """모르는 칸의 경우별 결과로 판정을 낸다. 상태는 늘 추가 확인 필요(needs_confirmation)다.

    모든 경우의 기본 과실이 같으면 그 비율을 결과에 넣는다(`scenario_fault`). 다르면 비율은 비우고
    경우별 줄을 보여 담당자가 맞는 경우를 확인하게 한다. 경우는 검색된 검수 도표 기준이고, 도표가
    있는 값만 경우가 된다(모르는 칸의 모든 값을 덮는다는 뜻이 아니다).
    안내 문장에는 '모른다'고 한 칸(`unknown`) 중 경우에 걸린 칸만 도표 선택 칸 순서로 적는다.
    """
    used = {k for s in scenarios for k in s["fields"]}
    keys = sorted(used & set(unknown), key=lambda k: _FIELD_ORDER.get(k, 99))
    names = [FIELD_LABELS.get(k, k) for k in keys]
    rest = len(names) - MAX_LISTED_UNKNOWN
    labels = ", ".join(names[:MAX_LISTED_UNKNOWN]) + (f" 외 {rest}칸" if rest > 0 else "")
    ratios = {(s["base_A"], s["base_B"]) for s in scenarios}
    update: dict = {"scenarios": scenarios[:MAX_SCENARIOS]}
    if len(ratios) == 1:
        a, b = next(iter(ratios))
        head = SCENARIO_SAME.format(labels=labels, a=a, b=b)
        update["scenario_fault"] = (a, b)
    else:
        head = SCENARIO_DIFFERENT.format(labels=labels)
    update["outcome"] = _outcome("needs_confirmation", [head, *scenario_lines(scenarios)])
    return update


def ai_values(card: FactCard) -> dict[str, str]:
    """카드 칸의 AI 원값(model_value)을 도표 선택 칸·값으로. 담당자 입력 칸처럼 원값이 없으면 뺀다.

    '모른다'고 답한 칸도 영상 AI가 처음 채운 값은 남아 있다. 경우를 고를 때 그 값을 쓴다.
    """
    fake = {**card, "fields": {
        k: {**f, "value": f.get("model_value"), "verification_status": "human_confirmed"}
        for k, f in card["fields"].items() if f.get("model_value") is not None
    }}
    return confirmed_values(fake)  # type: ignore[arg-type]


def scenario_choice(
    scenarios: list[UnknownScenario], ai: dict[str, str]
) -> tuple[UnknownScenario, str] | None:
    """판단을 이어 갈 경우와 그 까닭("same"·"ai"). 고를 수 없으면 None(추가 확인 필요).

    - 모든 경우의 기본 과실이 같으면 그 비율로 이어 간다. 경우가 여럿이면 AI 원값과 맞는 칸이
      많은 경우, 같으면 앞(검색 점수 순)의 경우.
    - 다르면 경우가 요구하는 값 중 AI 원값이 있는 칸은 모두 맞고, 맞는 칸이 하나 이상인 경우만
      고른다(맞는 칸이 많은 경우, 같으면 앞의 경우). 없으면 None.
    """
    def fit(s: UnknownScenario) -> int | None:
        known = [k for k in s["fields"] if k in ai]
        if any(ai[k] != s["fields"][k] for k in known):
            return None
        return len(known)

    if not scenarios:
        return None
    same = len({(s["base_A"], s["base_B"]) for s in scenarios}) == 1
    scored = [(fit(s), i, s) for i, s in enumerate(scenarios)]
    if same:
        best = max(scored, key=lambda x: (x[0] or 0, -x[1]))
        return best[2], "same"
    matching = [(n, i, s) for n, i, s in scored if n]
    if not matching:
        return None
    return max(matching, key=lambda x: (x[0], -x[1]))[2], "ai"


def scenario_reasons(
    choice: UnknownScenario, why: str, scenarios: list[UnknownScenario], unknown: list[str]
) -> list[str]:
    """이어 간 판단의 사유 첫 줄들 — 모르는 칸을 무엇으로 보았는지와 다른 경우."""
    keys = sorted(choice["fields"].keys() & set(unknown), key=lambda k: _FIELD_ORDER.get(k, 99))
    labels = ", ".join(FIELD_LABELS.get(k, k) for k in keys) or "모르는 칸"
    guess = ", ".join(
        f"{FIELD_LABELS.get(k, k)} {korean_value(v)}" for k, v in choice["fields"].items()
    )
    if why == "same":
        head = (f"모른다고 답한 칸({labels})은 어느 값이어도 기본 과실이 "
                f"A {choice['base_A']} : B {choice['base_B']}로 같아 "
                f"{choice['chart_id']}로 판단했어요(검색된 검수 도표 기준)")
        return [head]
    others = [s for s in scenarios if s is not choice]
    head = (f"모른다고 답한 칸({labels})은 AI 추정값({guess})으로 가정해 {choice['chart_id']}로 "
            "판단했어요. 사실이 다르면 카드를 고쳐 다시 판단해 주세요")
    return [head, *(["다른 경우:", *scenario_lines(others)] if others else [])]


def _outcome(status: str, reasons: list[str]) -> Outcome:
    return {"status": status, "reasons": reasons}  # type: ignore[typeddict-item]


def _listed(lines: list[str], limit: int, what: str) -> list[str]:
    """사유 줄을 앞 limit개만 남기고 나머지는 개수 한 줄로 줄인다."""
    rest = len(lines) - limit
    return lines[:limit] + ([f"그 밖에 {what} {rest}개"] if rest > 0 else [])


def build_graph(
    deps: Dependencies | None = None, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    """C 검색 도구·검증 3겹 연결. 체크포인터가 없으면 메모리(테스트·로컬)."""
    deps = deps or Dependencies()

    def require_confirmed_card(state: JudgmentState) -> dict:
        card = state["card"]
        if card["status"] != "confirmed":
            raise UnconfirmedCardError("확정된 사실 카드만 판단할 수 있다")
        mapping = card["party_mapping"]
        a, b = mapping["chart_party_A_id"], mapping["chart_party_B_id"]
        update: dict = {"stage": "require_confirmed_card"}
        if not a or not b or a == b:
            update["outcome"] = _outcome(
                "needs_confirmation",
                ["당사자 매핑 미확정: 도표 A/B에 대응하는 당사자를 확인해야 한다"],
            )
        return update

    def check_chart_scope(state: JudgmentState) -> dict:
        reason, _ = scope_gate(state["card"]["fields"], state["card"])  # type: ignore[arg-type]
        update: dict = {"stage": "check_chart_scope"}
        if reason:
            update["outcome"] = _outcome("deferred", [reason])
        return update

    async def retrieve_chart_candidates(state: JudgmentState) -> dict:
        candidates = await deps.retrieve(state["card"])
        update: dict = {
            "stage": "retrieve_chart_candidates", "candidates": candidates,
            "evidence": [hit for c in candidates for hit in c["hits"]],
        }
        if not any(c["calculation_ready"] for c in candidates):
            lines = [f"{c['chart_id']}: {r}" for c in candidates for r in c.get("reasons", [])]
            reasons = [REASON_NO_REVIEWED_CHART,
                       *_listed(lines, MAX_LISTED_UNREVIEWED, "검수 전 도표")]
            update["outcome"] = _outcome("deferred", reasons)
        return update

    def ask_or_resume(state: JudgmentState, config: RunnableConfig) -> dict:
        fields = open_fields(state)
        if not fields:
            return {"stage": "ask_or_resume", "questions": []}
        thread_id = str(config.get("configurable", {}).get("thread_id") or state["case_id"])
        questions = [build_question(thread_id, key, state["card"]) for key in fields]
        answers: list[QuestionAnswer] = interrupt({"questions": questions})
        unknown_ids = {
            a["question_id"] for a in answers
            if isinstance(a, dict) and a.get("unknown") and a.get("value") is None
        }
        unknown = list(state.get("unknown_fields", []))
        # 질문의 칸 이름은 카드 형식이다(한국어 안이면 accident_object, v1 맞바꿈이면 B 쪽 칸).
        # 그래프는 도표 선택 칸으로 센다
        card = state["card"]
        unknown += [chart_key(q["field_key"], card) for q in questions
                    if q["question_id"] in unknown_ids
                    and chart_key(q["field_key"], card) not in unknown]
        return {"stage": "ask_or_resume", "questions": questions, "unknown_fields": unknown}

    def select_chart_and_modifiers(state: JudgmentState) -> dict:
        card = state["card"]
        values = confirmed_values(card)
        # 담당자가 "모른다"고 한 범위 필수 칸. 그 칸이 `*`(무관)인 도표만 _rank를 통과한다
        unknown = [k for k in _required_fields(card) if k in state.get("unknown_fields", [])]
        update: dict = {"stage": "select_chart_and_modifiers", "selection": None}
        ranked, skipped = _rank(state)

        def continue_with_scenario(scenarios: list[UnknownScenario]) -> dict:
            """모르는 칸의 경우 하나로 판단을 이어 간다(scenario_choice). 못 고르면 확인 필요."""
            picked = scenario_choice(scenarios, ai_values(card))
            if picked is None:
                return {**update, **scenario_outcome(scenarios, state["unknown_fields"])}
            choice, why = picked
            trial = {**values, **choice["fields"]}
            best_ = next(c for c in _eligible(state) if c["chart_id"] == choice["chart_id"])
            variant_ = _pick_variant(best_, trial)[0]
            if variant_ is None:  # 경우를 만든 순위와 같아 생기지 않지만, 생기면 확인으로
                return {**update, **scenario_outcome(scenarios, state["unknown_fields"])}
            unresolved_ = _unresolved_modifiers(best_)
            matched_ = _matched_fields(best_, values)
            others = [c for c in ranked if c["chart_id"] != best_["chart_id"]]
            selection_: ChartSelection = {
                "chart_id": best_["chart_id"], "parent_id": best_["parent_id"],
                "edition": best_["edition"], "variant": variant_,  # type: ignore[typeddict-item]
                "applied_modifiers": [], "unresolved_modifiers": unresolved_,  # type: ignore[typeddict-item]
                "alternatives": [c["chart_id"] for c in others],
                "reasons": [
                    *scenario_reasons(choice, why, scenarios, state["unknown_fields"]),
                    f"{best_['chart_id']} 선택: 확인한 칸 {len(matched_)}개 일치"
                    f" ({', '.join(matched_)})",
                ],
                "modifier_decisions": modifier_decisions(best_, unresolved_),
                "alternative_reasons": alternative_reasons(state, best_, others, skipped, values),
            }
            return {**update, "selection": selection_, "selected_chart_id": best_["chart_id"],
                    "scenarios": scenarios}

        if not ranked:
            excluded = _listed([f"{chart_id}: {why}" for chart_id, why in skipped],
                               MAX_LISTED_EXCLUDED, "제외한 도표")
            # 모른다고 답한 칸 때문에 막혔으면 경우 중 하나로 이어 가거나(같은 비율·AI 추정값) 확인
            scenarios = unknown_scenarios(state)
            if scenarios:
                return continue_with_scenario(scenarios)
            update["outcome"] = _outcome(
                "deferred", [REASON_FIELD_NOT_CONFIRMED.format(field_key=k) for k in unknown]
                or [REASON_NO_REVIEWED_CHART, *no_shape_lines(state), *excluded,
                    *swap_hints(state)]
            )
            return update
        best = ranked[0]
        variant, missing = _pick_variant(best, values)
        if variant is None and (scenarios := unknown_scenarios(state)):
            return continue_with_scenario(scenarios)
        if variant is None:
            update["outcome"] = _outcome(
                "deferred", [REASON_FIELD_NOT_CONFIRMED.format(field_key=k) for k in missing]
                or [f"{best['chart_id']}: 기본 과실 변형 조건 불일치"]
            )
            return update
        unresolved = _unresolved_modifiers(best)
        matched = _matched_fields(best, values)
        # '아니오'로 본 촬영 차 칸 중 후보 도표가 실제로 가르는 칸만 이유에 적는다(보고서에 보임)
        assumed = [k for k in assumed_fields(card)
                   if any(k in _concrete(c["attributes"])
                          or any(k in _concrete(v["conditions"]) for v in c["base_variants"])
                          for c in _eligible(state))]
        selection: ChartSelection = {
            "chart_id": best["chart_id"], "parent_id": best["parent_id"],
            "edition": best["edition"], "variant": variant,  # type: ignore[typeddict-item]
            # 수정요소 가감은 담당자가 보고서 단계에서 정한다. 그래프는 기본 과실만 계산한다
            "applied_modifiers": [], "unresolved_modifiers": unresolved,  # type: ignore[typeddict-item]
            "alternatives": [c["chart_id"] for c in ranked[1:]],
            "reasons": [
                f"{best['chart_id']} 선택: 확인한 칸 {len(matched)}개 일치 ({', '.join(matched)})",
                *(f"{k}: 확인 불가 — {best['chart_id']} 선택에는 쓰지 않음" for k in unknown),
                *(f"{k}: 담당자 표시 없음 — 촬영 차(가입자 차)라 '아니오'로 봄" for k in assumed),
            ],
            "modifier_decisions": modifier_decisions(best, unresolved),
            "alternative_reasons": alternative_reasons(state, best, ranked[1:], skipped, values),
        }
        update["selection"] = selection
        update["selected_chart_id"] = best["chart_id"]
        return update

    def check_evidence_node(state: JudgmentState) -> dict:
        selection = state["selection"]
        assert selection is not None
        accident_date = state["card"]["accident_date"]
        check = check_evidence(selection, state.get("candidates", []), accident_date)
        update: dict = {"stage": "check_evidence", "checks": [*state["checks"], check]}
        if not check["passed"]:
            update["outcome"] = _outcome("deferred", [REASON_NO_REVIEWED_CHART, *check["reasons"]])
        return update

    async def review_rebuttal_node(state: JudgmentState) -> dict:
        selection = state["selection"]
        assert selection is not None
        check, preferred = await review_rebuttal(
            state["card"], selection, state.get("candidates", []), deps.reviewer
        )
        update: dict = {"stage": "review_rebuttal", "checks": [*state["checks"], check]}
        if check["passed"]:
            return update
        kind = check.get("rebuttal_kind")
        retries = state["rebuttal_retry_count"]
        if kind == "wrong_chart" and retries < MAX_REBUTTAL_RETRIES and preferred:
            update["rebuttal_retry_count"] = retries + 1
            excluded = state.get("excluded_chart_ids", [])
            update["excluded_chart_ids"] = [*excluded, selection["chart_id"]]
            update["preferred_chart_id"] = preferred
            return update
        label = {
            "wrong_chart": "두 번째 도표 오선택 반박: 담당자 확인 필요",
            "missing_fact": "도표를 가르는 사실이 카드에 없음",
            "contradiction": "카드 사실과 도표 조건이 모순",
        }.get(kind or "", "반박 성립")
        if kind == "wrong_chart" and not preferred:
            # 검토기가 차순위 후보가 아닌 도표를 가리켰다(parse_verdict). 다시 고를 수 없다
            label = "도표 오선택 반박인데 고를 차순위 후보가 없음: 담당자 확인 필요"
        update["outcome"] = _outcome("needs_confirmation", [label, *check["reasons"]])
        return update

    def calculate_and_validate_fault(state: JudgmentState) -> dict:
        selection = state["selection"]
        assert selection is not None
        variant = selection["variant"]
        base = (variant["base_A"], variant["base_B"])
        modifiers = selection["applied_modifiers"]
        lines, fault = calculation_lines(base[0], base[1], modifiers)  # type: ignore[arg-type]
        provisional = _result(state, "draft_ready", selection["reasons"], fault, lines, [])
        check = check_numerical(provisional, base, modifiers)  # type: ignore[arg-type]
        update: dict = {
            "stage": "calculate_and_validate_fault", "checks": [*state["checks"], check],
            "fault": fault, "calculation_log": lines,
        }
        if not check["passed"]:
            update["outcome"] = _outcome("deferred", ["수치 검증 실패", *check["reasons"]])
        return update

    def build_judgment_result(state: JudgmentState) -> dict:
        outcome = state.get("outcome")
        status = outcome["status"] if outcome else "draft_ready"
        selection = state.get("selection")
        reasons = outcome["reasons"] if outcome else (selection["reasons"] if selection else [])
        fault = state.get("fault") if status == "draft_ready" else None
        if status == "needs_confirmation" and state.get("scenario_fault"):
            fault = state["scenario_fault"]  # 모든 경우의 기본 과실이 같음(추가 확인 필요)
        log = state.get("calculation_log", []) if status == "draft_ready" else []
        questions = [
            build_question(state["case_id"], k, state["card"]) for k in open_fields(state)
        ] if status == "needs_confirmation" else []
        result = _result(state, status, reasons, fault, log, questions)
        return {"stage": "build_judgment_result", "result": result}

    def _result(
        state: JudgmentState, status: str, reasons: list[str], fault: tuple[int, int] | None,
        log: list[str], questions: list[ConfirmationQuestion],
    ) -> JudgmentResult:
        selection = state.get("selection")
        keep_chart = selection is not None and status != "deferred"
        chart_hits = [
            hit for c in state.get("candidates", [])
            if selection and c["chart_id"] == selection["chart_id"] for hit in c["hits"]
        ]
        return {
            "versions": {
                "case_id": state["case_id"], "fact_card_version": state["card"]["version"],
                "judgment_version": state.get("judgment_version"), "report_version": None,
            },
            "status": status,  # type: ignore[typeddict-item]
            "chart_id": selection["chart_id"] if keep_chart and selection else None,
            "edition": selection["edition"] if keep_chart and selection else None,
            "fault_A": fault[0] if fault else None, "fault_B": fault[1] if fault else None,
            "calculation_log": list(log), "evidence": chart_hits if keep_chart else [],
            "checks": list(state["checks"]), "questions": questions, "reasons": list(reasons),
            "base_fault_A": selection["variant"]["base_A"] if keep_chart and selection
            else (fault[0] if fault and state.get("scenario_fault") else None),
            "modifiers": list(selection["modifier_decisions"]) if keep_chart and selection else [],
            "alternatives": (
                list(selection["alternative_reasons"]) if keep_chart and selection else []
            ),
            **({"scenarios": list(state.get("scenarios") or [])} if state.get("scenarios") else {}),
        }

    def _after(next_node: str):
        return lambda state: "build_judgment_result" if state.get("outcome") else next_node

    def _after_ask(state: JudgmentState) -> str:
        return "ask_or_resume" if open_fields(state) else "select_chart_and_modifiers"

    def _after_rebuttal(state: JudgmentState) -> str:
        """판정이 났으면 결과로, 반박이 성립했는데 판정이 없으면 차순위 재실행, 아니면 계산."""
        if state.get("outcome"):
            return "build_judgment_result"
        if not state["checks"][-1]["passed"]:
            return "select_chart_and_modifiers"
        return "calculate_and_validate_fault"

    graph = StateGraph(JudgmentState)
    graph.add_node("require_confirmed_card", require_confirmed_card)
    graph.add_node("check_chart_scope", check_chart_scope)
    graph.add_node("retrieve_chart_candidates", retrieve_chart_candidates)
    graph.add_node("ask_or_resume", ask_or_resume)
    graph.add_node("select_chart_and_modifiers", select_chart_and_modifiers)
    graph.add_node("check_evidence", check_evidence_node)
    graph.add_node("review_rebuttal", review_rebuttal_node)
    graph.add_node("calculate_and_validate_fault", calculate_and_validate_fault)
    graph.add_node("build_judgment_result", build_judgment_result)
    graph.add_edge(START, "require_confirmed_card")
    graph.add_conditional_edges("require_confirmed_card", _after("check_chart_scope"))
    graph.add_conditional_edges("check_chart_scope", _after("retrieve_chart_candidates"))
    graph.add_conditional_edges("retrieve_chart_candidates", _after("ask_or_resume"))
    graph.add_conditional_edges("ask_or_resume", _after_ask)
    graph.add_conditional_edges("select_chart_and_modifiers", _after("check_evidence"))
    graph.add_conditional_edges("check_evidence", _after("review_rebuttal"))
    graph.add_conditional_edges("review_rebuttal", _after_rebuttal)
    graph.add_conditional_edges("calculate_and_validate_fault", _after("build_judgment_result"))
    graph.add_edge("build_judgment_result", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())
