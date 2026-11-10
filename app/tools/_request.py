"""Validate the three corpus tool requests before handing them to D's search."""

from datetime import date
from typing import Literal

from app.schemas.retrieval import SearchQuery, SearchRequest

Corpus = Literal["fault_standards", "dispute_cases", "policy_terms"]
COMMON_FILTERS = frozenset({"document_id", "edition"})
CORPUS_FILTERS = {
    "fault_standards": frozenset({"chart_id"}),
    "dispute_cases": frozenset({"chart_id", "case_id"}),
    "policy_terms": frozenset(
        {"coverage", "clause_id", "article_number", "rider_number", "schedule"}
    ),
}


def prepare_request(query: SearchQuery, corpus: Corpus) -> SearchRequest:
    if not isinstance(query, dict):
        raise TypeError("SearchQuery must be a mapping")
    text = query.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Search text is required")
    top_k = query.get("top_k", 10)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
        raise ValueError("top_k must be an integer from 1 to 100")
    accident_date = query.get("accident_date", "")
    if not isinstance(accident_date, str):
        raise ValueError("accident_date must be an ISO date or empty")
    if accident_date:
        try:
            parsed = date.fromisoformat(accident_date)
        except ValueError as exc:
            raise ValueError("accident_date must be YYYY-MM-DD") from exc
        if parsed.isoformat() != accident_date:
            raise ValueError("accident_date must be YYYY-MM-DD")
    filters = query.get("filters", {})
    if not isinstance(filters, dict):
        raise ValueError("filters must be a mapping")
    unknown = filters.keys() - COMMON_FILTERS - CORPUS_FILTERS[corpus]
    if unknown:
        raise ValueError(f"Unsupported {corpus} filters: {', '.join(sorted(unknown))}")
    if any(not isinstance(value, str) or not value.strip() for value in filters.values()):
        raise ValueError("Filter values must be non-empty strings")
    # Make a fresh request: the caller's query and filters must not be mutated.
    return {
        "corpus": corpus,
        "text": text.strip(),
        "accident_date": accident_date,
        "filters": filters.copy(),
        "top_k": top_k,
    }
