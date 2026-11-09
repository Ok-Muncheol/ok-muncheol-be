"""C: fault_standards 전용 검색 도구. D의 공통 검색 함수를 호출한다."""

from app.retrieval import common
from app.schemas.retrieval import EvidenceHit, SearchQuery
from app.tools._request import prepare_request


async def search_fault_standards(query: SearchQuery) -> list[EvidenceHit]:
    """Search chart and modifier chunks; chart_id is an exact chart lookup.

    Movement and signal fields are unreviewed candidates, so they are not hard filters.
    An unknown effective period must not exclude the document by accident date.
    """
    return await common.search(prepare_request(query, "fault_standards"))
