"""B: 검증 ② 반박 검토. 재실행 횟수·경로는 graph.py가 통제한다."""

from app.schemas.judgment import JudgmentResult, ValidationCheck


async def review_rebuttal(result: JudgmentResult) -> ValidationCheck:
    """불명 조건·선택/제외 이유·차순위 도표를 검토한다."""
    raise NotImplementedError("B: 반박 검토·확인 필요 쟁점")
