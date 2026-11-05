"""C: 동일 질의/필터로 기준선·개선안을 비교한다. 미라벨 양식은 채점하지 않는다."""

from evals.retrieval.dataset import RetrievalExample


async def evaluate(examples: list[RetrievalExample]) -> dict[str, object]:
    """검수 라벨·정답 ID/적용판·분할 누수 검사 후 Top-1·Recall@3·시간/비용 측정.

    unlabelled·unassigned는 거절한다. 정답 없음은 검수한 음성 사례만 허용한다.
    """
    raise NotImplementedError("C: 평가셋 검증·코퍼스별 지표·검색 전략 비교")
