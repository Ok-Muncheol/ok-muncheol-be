"""C: policy_terms 전용 검색 도구. D의 공통 검색 함수를 호출한다."""

from app.retrieval import common
from app.schemas.retrieval import EvidenceHit, SearchQuery
from app.tools._evidence import validate_hits
from app.tools._request import prepare_request


async def search_policy_terms(query: SearchQuery) -> list[EvidenceHit]:
    """Search clauses and riders in the requested edition and coverage.

    The contract's edition belongs in filters['edition']; accident_date is not a
    substitute. A coverage label is retrieval relevance, not proof of enrollment.
    """
    request = prepare_request(query, "policy_terms")
    return validate_hits(request, await common.search(request))
