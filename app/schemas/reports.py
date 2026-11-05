"""D: 금액은 원 단위 정수. 미확정을 0원으로 채우지 않는다."""

from typing import Literal, TypedDict

from app.schemas.common import VersionRef
from app.schemas.judgment import JudgmentResult


class CalculationResult(TypedDict):
    versions: VersionRef
    status: Literal["estimated", "needs_documents", "confirmed"]
    currency: Literal["KRW"]
    recognized_damage: int | None
    insurance_payment: int | None
    deductible: int | None
    rule_version: str
    source_document_ids: list[str]
    reasons: list[str]


class RecoveryAmounts(TypedDict):
    planned: int | None
    claimed: int | None
    recovered: int | None
    customer_deductible_review: int | None


class ReviewReport(TypedDict):
    versions: VersionRef
    judgment: JudgmentResult
    sections: list[dict[str, object]]
    calculation: CalculationResult | None
    recovery: RecoveryAmounts | None
    approval_status: Literal["draft", "approved"]
    approved_by: str | None
    approved_at: str | None
