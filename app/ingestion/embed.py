"""C: 오프라인 문서 임베딩. D의 질의 임베딩과 모델/차원을 맞춘다."""

from app.schemas.retrieval import EmbeddedChunk, SearchChunk


async def embed_chunks(
    chunks: list[SearchChunk], model: str, dimensions: int, version: str
) -> list[EmbeddedChunk]:
    """배치·중복 방지·재시도, 원문 hash 및 모델/차원/버전 기록."""
    raise NotImplementedError("C: 문서 임베딩·색인 산출물")
