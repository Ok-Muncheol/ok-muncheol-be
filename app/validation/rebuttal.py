"""B: 검증 ② 반박 검토. 재실행 횟수·경로는 graph.py가 통제한다."""

from app.schemas.judgment import JudgmentResult, ValidationCheck


async def review_rebuttal(result: JudgmentResult) -> ValidationCheck:
    """불명 조건·선택/제외 이유·차순위 도표를 검토한다.

    실패하면 rebuttal_kind를 반드시 채운다. wrong_chart(차순위 후보가 더 맞음)만
    graph.py가 재실행하고, missing_fact(구분 조건 누락)·contradiction(근거와 카드 모순)은
    담당자 질문으로 넘긴다.
    """
    raise NotImplementedError("B: 반박 검토·확인 필요 쟁점")
