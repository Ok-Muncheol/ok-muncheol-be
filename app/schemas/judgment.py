"""B: 판단·질문·검증 3겹 계약. 수치는 검수한 구조 레코드에서 읽는다."""

from typing import Literal, TypedDict

from app.schemas.common import JudgmentStatus, VersionRef
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


class ValidationCheck(TypedDict):
    layer: Literal["evidence", "rebuttal", "numerical"]
    passed: bool
    reasons: list[str]


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
