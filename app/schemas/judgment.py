"""B: 판단·질문·검증 3겹 계약. 수치는 검수한 구조 레코드에서 읽는다."""

from typing import Literal, NotRequired, TypedDict

from app.schemas.common import ApiError, JudgmentStatus, TaskStatus, VersionRef
from app.schemas.retrieval import EvidenceHit


class ConfirmationQuestion(TypedDict):
    question_id: str
    field_key: str
    text: str
    options: list[str]
    allow_unknown: bool


class QuestionAnswer(TypedDict):
    question_id: str
    expected_card_version: int
    value: str | None
    unknown: bool


RebuttalKind = Literal["wrong_chart", "missing_fact", "contradiction"]


class ValidationCheck(TypedDict):
    layer: Literal["evidence", "rebuttal", "numerical"]
    passed: bool
    reasons: list[str]
    # layer == "rebuttal" 이고 passed == False 이면 필수. wrong_chart만 차순위 도표 재실행 대상
    rebuttal_kind: NotRequired[RebuttalKind | None]


ModifierDecisionValue = Literal["적용", "미적용", "불명"]


class ModifierDecision(TypedDict):
    """선택한 도표의 수정요소 판정(보고서 5번). 가감치가 있는 요소만 담는다."""

    modifier_id: str
    name: str
    applies_to: Literal["A", "B"]
    delta: int
    # 적용: 확인한 칸이 조건을 충족 / 미적용: 조건 불일치
    # 불명: 칸 미확인·코드로 판정 불가·담당자 입력 전용
    decision: ModifierDecisionValue
    evidence_fields: list[str]
    reason: NotRequired[str]


class ChartAlternative(TypedDict):
    """고르지 않은 도표 후보와 이유(보고서 3번 차순위 후보)."""

    chart_id: str
    reason: str


class JudgmentResult(TypedDict):
    versions: VersionRef
    status: JudgmentStatus
    chart_id: str | None
    edition: str | None
    fault_A: int | None
    fault_B: int | None
    calculation_log: list[str]
    evidence: list[EvidenceHit]
    checks: list[ValidationCheck]
    questions: list[ConfirmationQuestion]
    reasons: list[str]
    # 보고서 3·4·5번(D app/reports/assembler.py). 도표를 고르지 못한 보류면 null·빈 목록이다.
    # 판단 그래프는 항상 채운다. 이 필드가 없는 예전 결과도 유효하다
    base_fault_A: NotRequired[int | None]
    modifiers: NotRequired[list[ModifierDecision]]
    alternatives: NotRequired[list[ChartAlternative]]


class AnswerView(TypedDict):
    """구분 질문에 단 답. 값 답변은 A의 카드 수정·재확정으로 새 카드 버전이 생겨야 판단에 쓰인다."""

    value: str | None
    unknown: bool
    expected_card_version: int
    # 답을 반영해 생긴 카드 버전. 그 버전의 판단 접수가 이 실행을 대체할 때 채운다(전이면 null)
    resulting_card_version: int | None
    answered_at: str


class QuestionView(ConfirmationQuestion):
    """판단 조회의 구분 질문과 답(아직 안 답했으면 null)."""

    answer: AnswerView | None


class JudgmentRunView(TypedDict):
    """GET /cases/{id}/judgment — 사건의 최신 판단 실행(judgment_runs 행)과 구분 질문·답.

    새로고침·재접속 복원용이다. 작업 상태(status)와 판정(result.status)을 분리한다.
    result는 completed일 때만, error는 failed일 때만 온다. 확인 대기면 질문은 questions에서 읽는다.
    """

    task_id: str
    case_id: str
    judgment_version: int
    fact_card_version: int
    criteria_version: str
    rule_version: str
    status: TaskStatus
    stage: str
    rebuttal_retry_count: int
    result: JudgmentResult | None
    error: ApiError | None
    # 판정이 났으면 그 판단으로 만든 보고서의 최신 버전.
    # 못 만들었으면 null(판단 접수를 다시 보내면 다시 만든다)
    report_version: int | None
    questions: list[QuestionView]
    updated_at: str
