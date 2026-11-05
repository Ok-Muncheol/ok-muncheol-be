"""C: fault_standards 전용 검색 도구. D의 공통 검색 함수를 호출한다."""

from app.retrieval.common import search
from app.schemas.retrieval import EvidenceHit, SearchQuery, SearchRequest


async def search_fault_standards(query: SearchQuery) -> list[EvidenceHit]:
    """호출자가 다른 corpus를 끼워 넣어도 이 도구의 범위를 유지한다.

    TODO(C): 코퍼스별 필수 필터/구조 레코드 계약과 원문 연결 검증.
    실제 검색은 D의 common.search 구현 후 동작한다.
    """
    request: SearchRequest = {**query, "corpus": "fault_standards"}
    return await search(request)
