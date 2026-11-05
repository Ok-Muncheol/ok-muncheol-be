"""D: 보고서 버전·승인, 1차 확장 산정/구상 API 진입점."""

from app.schemas.reports import CalculationResult, ReviewReport


async def get_report(actor_id: str, case_id: str, version: int) -> ReviewReport:
    raise NotImplementedError("D: 권한 있는 버전별 보고서 조회")


async def approve_report(actor_id: str, case_id: str, version: int) -> ReviewReport:
    """표시한 버전·입력 최신성 검사 후 승인 이력 저장."""
    raise NotImplementedError("D: 승인·이력·오래된 결과 차단")


async def calculate_payment(actor_id: str, case_id: str, version: int) -> CalculationResult:
    raise NotImplementedError("D: 1차 확장 승인 과실·서류 버전 기반 산정")


async def export_claim(actor_id: str, case_id: str, version: int) -> dict[str, object]:
    raise NotImplementedError("D: 1차 확장 승인·권한 검사 후 PDF/JSON 내보내기")
