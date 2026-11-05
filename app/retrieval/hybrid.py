"""D: 코퍼스별 검색 전략. 하이브리드·재순위 채택 여부는 C 평가로 결정."""

from app.schemas.retrieval import EvidenceHit, SearchRequest


async def retrieve(request: SearchRequest, query_vector: list[float]) -> list[EvidenceHit]:
    """도표는 속성/유효판 우선, 결정례는 chart_id/사실, 약관은 담보/조항.

    Kiwi BM25·trigram·벡터 비교, 선택적 RRF·재순위. pg ts_rank는 BM25가 아니다.
    """
    raise NotImplementedError("D: 필터 후 검색·선택적 결합·근거 정규화")
