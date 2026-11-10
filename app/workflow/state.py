"""B: 그래프 노드 간 계약. 노드 배선은 graph.py에서 관리한다."""

from typing import Literal, NotRequired, TypedDict

from app.schemas.common import JudgmentStatus
from app.schemas.facts import FactCard
from app.schemas.judgment import (
    ChartAlternative,
    ConfirmationQuestion,
    JudgmentResult,
    ModifierDecision,
    ValidationCheck,
)
from app.schemas.retrieval import EvidenceHit
from app.workflow.candidates import ChartCandidate, ChartModifier, ChartVariant


class AppliedModifier(TypedDict):
    modifier_id: str
    party: Literal["A", "B"]
    name: str
    code: str
    delta: int


class ChartSelection(TypedDict):
    chart_id: str
    parent_id: str
    edition: str
    variant: ChartVariant
    applied_modifiers: list[AppliedModifier]
    # 적용하지 않은 수정요소와 이유(불명·조건 불일치·담당자 입력 전용)
    unresolved_modifiers: list[tuple[ChartModifier, str]]
    # 차순위 후보(순위순). 반박이 도표 오선택이면 첫 번째로 재실행한다
    alternatives: list[str]
    reasons: list[str]
    # 보고서용: 수정요소 판정(5번)과 고르지 않은 후보·이유(3번)
    modifier_decisions: list[ModifierDecision]
    alternative_reasons: list[ChartAlternative]


class Outcome(TypedDict):
    """도중에 확정된 판정. 설정되면 바로 build_judgment_result로 간다."""

    status: JudgmentStatus
    reasons: list[str]


class JudgmentState(TypedDict):
    case_id: str
    card: FactCard
    criteria_version: str
    rule_version: str
    # judgment_runs.judgment_version. API 계층이 실행을 만들 때 정한다
    judgment_version: NotRequired[int | None]
    rebuttal_retry_count: int
    evidence: list[EvidenceHit]
    questions: list[ConfirmationQuestion]
    checks: list[ValidationCheck]
    stage: NotRequired[str]
    candidates: NotRequired[list[ChartCandidate]]
    # 담당자가 "모른다"고 답한 칸. 다시 묻지 않고 보류 사유로 쓴다
    unknown_fields: NotRequired[list[str]]
    excluded_chart_ids: NotRequired[list[str]]
    # 반박 검토가 더 맞다고 본 차순위 도표. 재실행 때 순위 맨 앞으로 올린다
    preferred_chart_id: NotRequired[str | None]
    selection: NotRequired[ChartSelection | None]
    selected_chart_id: NotRequired[str]
    fault: NotRequired[tuple[int, int]]
    calculation_log: NotRequired[list[str]]
    outcome: NotRequired[Outcome | None]
    result: NotRequired[JudgmentResult]
