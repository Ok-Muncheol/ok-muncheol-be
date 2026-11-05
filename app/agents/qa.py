"""B: 근거 질의응답·what-if. 공식 판단/보고서/승인 상태 쓰기 금지."""

from app.schemas.judgment import JudgmentResult


async def answer_with_evidence(
    question: str, official_result: JudgmentResult, assumptions: dict[str, object] | None = None
) -> dict[str, object]:
    """C 도구·D 산정의 가정 모드 사용. 사실 변경은 A 확인 경로로 안내."""
    raise NotImplementedError("B: 1차 확장 근거 질의응답·독립 가정 실행")
