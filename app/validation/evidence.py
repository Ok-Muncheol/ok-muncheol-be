"""B: 검증 ① 근거 체크."""

from app.schemas.judgment import JudgmentResult, ValidationCheck


def check_evidence(result: JudgmentResult) -> ValidationCheck:
    """DB의 ID·적용판·원문/페이지·수정요소 연결 대조. 생성 인용 차단."""
    raise NotImplementedError("B: 근거 실재·유효판·인용 검증")
