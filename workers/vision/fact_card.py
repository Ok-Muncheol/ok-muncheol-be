"""A: VLM 출력과 탐지 결과를 교차 확인해 미확정 사실 카드를 만든다."""

from app.schemas.facts import FactCard


async def extract_fact_card(
    case_id: str, video_path: str, detections: dict[str, object]
) -> FactCard:
    """모델 원값·확신도·증거·불명/충돌 보존. 담당자 확인 전에는 draft다."""
    raise NotImplementedError("A: VLM 구조화 출력·교차 확인")
