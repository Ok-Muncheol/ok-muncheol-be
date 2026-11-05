"""D: C의 세 도구가 사용하는 공통 온라인 검색 진입점."""

from app.schemas.retrieval import EvidenceHit, SearchRequest


async def search(request: SearchRequest) -> list[EvidenceHit]:
    """코퍼스·적용기간·속성 필터 후 검색, 출처/페이지를 보존해 반환.

    약관은 계약 적용판을 filters에 별도로 전달한다. 사고 일자로 대체하지 않는다.
    필터를 자동 완화하거나 다른 코퍼스로 폴백하지 않는다.
    """
    raise NotImplementedError("D: DB·필터·질의 임베딩·공통 검색 연결")
