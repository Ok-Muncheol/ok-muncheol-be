"""D: 검증 결과를 템플릿으로 조립. 보고서 작성용 LLM은 추가 호출하지 않는다."""

from app.schemas.judgment import JudgmentResult
from app.schemas.reports import CalculationResult, ReviewReport


def assemble_review(
    judgment: JudgmentResult, calculation: CalculationResult | None = None
) -> ReviewReport:
    """MVP 1~7항목, 확장 8~9항목. 확인/보류 상태에서 수치를 채워 넣지 않는다."""
    raise NotImplementedError("D: 보고서 템플릿·근거/수치/버전 일치 검사")
