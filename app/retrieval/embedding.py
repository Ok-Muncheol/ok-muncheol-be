"""D: 온라인 질의 임베딩. 문서 임베딩은 C의 ingestion/embed.py 소유."""


async def embed_query(text: str, model: str, dimensions: int, version: str) -> list[float]:
    """문서 색인의 모델/차원/버전 검사·캐시·실패 정책을 구현한다."""
    raise NotImplementedError("D: 질의 임베딩·문서 색인 호환성")
