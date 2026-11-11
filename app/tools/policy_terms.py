"""C: policy_terms 전용 검색 도구. D의 공통 검색 함수를 호출한다."""

from app.retrieval import common
from app.schemas.retrieval import EvidenceHit, SearchQuery
from app.tools._evidence import validate_hits
from app.tools._request import prepare_request


async def search_policy_terms(query: SearchQuery) -> list[EvidenceHit]:
    """Search clauses, riders and payment-standard schedules (별표) in the requested edition.

    The contract's edition belongs in filters['edition']; accident_date is not a
    substitute. A coverage label is retrieval relevance, not proof of enrollment.
    filters['schedule'] (예: "별표3") narrows to one schedule. Within an edition a schedule
    item may apply only to some accident dates (metadata accident_date_from/to). The common
    search drops hits outside that window (app/retrieval/index.py); validate_hits rejects any
    that slip through. Calculation values (별표4·5) come from schedule_values records, not chunk
    text.
    """
    request = prepare_request(query, "policy_terms")
    return await validate_hits(request, await common.search(request))
