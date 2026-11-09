"""C: 오프라인 문서 임베딩. D의 질의 임베딩과 모델/차원을 맞춘다."""

import math

from app.schemas.retrieval import EmbeddedChunk, SearchChunk


async def embed_chunks(
    chunks: list[SearchChunk], model: str, dimensions: int, version: str
) -> list[EmbeddedChunk]:
    """배치·중복 방지·재시도, 원문 hash 및 모델/차원/버전 기록."""
    from openai import AsyncOpenAI

    if not model or not version or dimensions < 1:
        raise ValueError("모델·차원·버전을 지정하세요.")
    if not chunks:
        return []
    if any(not c["text"].strip() for c in chunks):
        raise ValueError("빈 청크는 임베딩할 수 없습니다.")
    unique = list(dict.fromkeys(c["text"] for c in chunks))
    vectors = {}
    async with AsyncOpenAI(timeout=90, max_retries=4) as client:
        for start in range(0, len(unique), 32):
            texts = unique[start : start + 32]
            response = await client.embeddings.create(
                model=model, dimensions=dimensions, input=texts, encoding_format="float"
            )
            rows = sorted(response.data, key=lambda row: row.index)
            if [r.index for r in rows] != list(range(len(texts))):
                raise ValueError("임베딩 응답 개수/인덱스가 올바르지 않습니다.")
            for text, row in zip(texts, rows, strict=False):
                validate_vector(row.embedding, dimensions)
                vectors[text] = row.embedding
    return [
        {
            "chunk": c,
            "embedding": vectors[c["text"]],
            "embedding_model": model,
            "embedding_dimensions": dimensions,
            "embedding_version": version,
        }
        for c in chunks
    ]


def validate_vector(vector: list[float], dimensions: int) -> None:
    if len(vector) != dimensions or not all(
        isinstance(v, (int, float)) and math.isfinite(v) for v in vector
    ):
        raise ValueError("벡터 차원 또는 값이 올바르지 않습니다.")
    if not any(vector):
        raise ValueError("영벡터가 반환되었습니다.")
