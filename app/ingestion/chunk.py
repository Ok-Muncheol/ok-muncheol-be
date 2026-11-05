"""C: 코퍼스별 구조 청킹. 도표/조항/사건 경계를 임의로 자르지 않는다."""

from app.schemas.retrieval import ParsedBlock, SearchChunk


def chunk_blocks(blocks: list[ParsedBlock]) -> list[SearchChunk]:
    """부모 ID·원문 페이지·적용기간·예외/수정요소 연결을 유지한다."""
    raise NotImplementedError("C: 도표·결정례·약관별 청킹/구조 검수")
