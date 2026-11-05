"""D: 승인 과실·검수 약관/계약/한도/면책/자기부담금으로 보험금 산정."""

from app.schemas.reports import CalculationResult, ReviewReport


def calculate_insurance(
    approved_report: ReviewReport, documents: list[dict[str, object]]
) -> CalculationResult:
    """승인 및 입력 버전 검사. 견적 추가는 이 단계부터 새 버전으로 재산정."""
    raise NotImplementedError("D: 실무 검수한 보험금 산식·근거·미확정 상태")
