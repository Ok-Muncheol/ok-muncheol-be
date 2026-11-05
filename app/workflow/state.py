"""B: 그래프 노드 간 계약. 노드 배선은 graph.py에서 관리한다."""

from typing import NotRequired, TypedDict

from app.schemas.facts import FactCard
from app.schemas.judgment import ConfirmationQuestion, JudgmentResult, ValidationCheck
from app.schemas.retrieval import EvidenceHit


class JudgmentState(TypedDict):
    case_id: str
    card: FactCard
    criteria_version: str
    rule_version: str
    rebuttal_retry_count: int
    evidence: list[EvidenceHit]
    questions: list[ConfirmationQuestion]
    checks: list[ValidationCheck]
    selected_chart_id: NotRequired[str]
    result: NotRequired[JudgmentResult]
