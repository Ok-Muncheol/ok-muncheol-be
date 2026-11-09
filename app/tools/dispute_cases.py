"""C: dispute_cases 전용 검색 도구. D의 공통 검색 함수를 호출한다."""

from app.retrieval import common
from app.schemas.retrieval import EvidenceHit, SearchQuery
from app.tools._evidence import validate_hits
from app.tools._request import prepare_request


async def search_dispute_cases(query: SearchQuery) -> list[EvidenceHit]:
    """Search case cards; chart_id selects a documented crosswalk link.

    A linked chart is a retrieval reference, not proof the chart fits new accident facts.
    Supporting chunks share the case parent_id with their fact-card chunk.
    """
    request = prepare_request(query, "dispute_cases")
    return validate_hits(request, await common.search(request))
