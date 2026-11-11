"""B: 판단·질문·검증 3겹 계약. 수치는 검수한 구조 레코드에서 읽는다."""

from typing import Literal, NotRequired, TypedDict

from app.schemas.common import ApiError, JudgmentStatus, TaskStatus, VersionRef
from app.schemas.retrieval import Corpus, EvidenceHit


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


# ── 근거 질의응답·what-if(POST /cases/{id}/qa, 1차 확장) ─────────────────────
QaToolName = Literal["get_judgment_log", "search_dispute_cases", "search_policy_terms", "simulate"]
# 인용 근거를 돌려준 도구: official 공식 판단 로그 / search 결정례·약관 검색 / hypothesis 가정 실행.
# 공식 판단 로그가 돌려준 근거이기도 하면 official이다
CitationSource = Literal["official", "search", "hypothesis"]


class QaCitation(TypedDict):
    """답에 인용한 근거. 도구가 돌려준 근거만 남는다. ref는 답 본문의 [E1] 같은 표시다.

    source가 hypothesis면 가정 실행이 고른 도표의 근거라 공식 판정의 근거가 아니다.
    """

    ref: str
    chunk_id: str
    parent_id: str
    corpus: Corpus
    edition: str
    page_start: int
    page_end: int
    excerpt: str
    source: CitationSource


class QaHypothesis(TypedDict):
    """simulate 가정 실행 결과. 공식 판단·보고서와 분리되고 저장하지 않는다.

    status: completed(가정 판정이 남) / waiting_for_confirmation(가정한 사실로도
    확인할 칸이 남아 멈춤, open_fields) / failed(error).
    """

    changes: dict[str, str]
    status: Literal["completed", "waiting_for_confirmation", "failed"]
    judgment_status: JudgmentStatus | None
    chart_id: str | None
    fault_A: int | None
    fault_B: int | None
    reasons: list[str]
    open_fields: list[str]
    error: ApiError | None


class QaToolCall(TypedDict):
    name: str
    ok: bool


class QaAnswer(TypedDict):
    """POST /cases/{id}/qa 응답. 최신 판정(judgment_version)을 기준으로 답한다.

    공식 판단·보고서·확인 완료 상태는 바꾸지 않는다. 가정 결과는 hypotheticals에만 있다.
    fact_change_hint: 가정 실행을 했으면 공식 반영 경로(사실 카드 수정·확정 → 판단 다시 요청)
    안내, 아니면 null.
    """

    case_id: str
    judgment_version: int
    answer: str
    citations: list[QaCitation]
    hypotheticals: list[QaHypothesis]
    tool_calls: list[QaToolCall]
    fact_change_hint: str | None
