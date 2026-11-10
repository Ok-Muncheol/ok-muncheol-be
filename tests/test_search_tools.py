import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.retrieval import common
from app.tools._evidence import FOLDERS, ROOT, _load
from app.tools.dispute_cases import search_dispute_cases
from app.tools.fault_standards import search_fault_standards
from app.tools.policy_terms import search_policy_terms

# 코퍼스 원문은 저장소에 두지 않는다. data/private/에 받아 둔 환경에서만 아래 테스트를 돌린다.
needs_corpus = pytest.mark.skipif(
    not all((ROOT / name / "processed" / "chunks.jsonl").is_file() for name in FOLDERS.values()),
    reason="data/private/ 코퍼스 파일이 없다",
)


@pytest.mark.parametrize(
    ("tool", "corpus", "filters"),
    [
        (search_fault_standards, "fault_standards", {"chart_id": "차1-1"}),
        (search_dispute_cases, "dispute_cases", {"chart_id": "차1-1"}),
        (
            search_policy_terms,
            "policy_terms",
            {"edition": "2026.11.11", "coverage": "own_damage"},
        ),
    ],
)
def test_tool_routes_own_corpus_without_mutating_input(monkeypatch, tool, corpus, filters):
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(common, "search", search)
    query = {
        "text": "  사고 근거  ",
        "accident_date": "",
        "filters": filters.copy(),
        "top_k": 5,
        "corpus": "wrong_corpus",
    }
    assert asyncio.run(tool(query)) == []
    request = search.await_args.args[0]
    assert request == {
        "corpus": corpus,
        "text": "사고 근거",
        "accident_date": "",
        "filters": filters,
        "top_k": 5,
    }
    assert query["text"] == "  사고 근거  "
    assert query["filters"] is not request["filters"]


def test_unreviewed_chart_attributes_cannot_be_hard_filters(monkeypatch):
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(common, "search", search)
    with pytest.raises(ValueError, match="Unsupported"):
        asyncio.run(
            search_fault_standards(
                {
                    "text": "직진 사고",
                    "accident_date": "",
                    "filters": {"signal_state_A": "red"},
                    "top_k": 10,
                }
            )
        )
    search.assert_not_awaited()


def test_policy_date_never_replaces_contract_edition(monkeypatch):
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(common, "search", search)
    asyncio.run(
        search_policy_terms(
            {"text": "자차 면책", "accident_date": "2026-11-11", "filters": {}, "top_k": 5}
        )
    )
    assert search.await_args.args[0]["filters"] == {}


def test_rejects_invalid_request_before_common_search(monkeypatch):
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(common, "search", search)
    with pytest.raises(ValueError, match="top_k"):
        asyncio.run(
            search_dispute_cases({"text": "사실", "accident_date": "", "filters": {}, "top_k": 0})
        )
    search.assert_not_awaited()


@needs_corpus
@pytest.mark.parametrize(
    ("tool", "corpus", "filters"),
    [
        (search_fault_standards, "fault_standards", {}),
        (search_dispute_cases, "dispute_cases", {"chart_id": "차1-1"}),
        (search_policy_terms, "policy_terms", {"edition": "2026.11.11"}),
    ],
)
def test_real_corpus_hit_links_to_structured_record_and_pdf(monkeypatch, tool, corpus, filters):
    chunks = _load(corpus)[0].values()
    chunk = next(
        row
        for row in chunks
        if all(
            row["document"].get(key) == value or row["metadata"].get(key) == value
            for key, value in filters.items()
        )
    )
    hit = {"chunk": chunk, "score": 0.8, "retrieval_version": "test"}
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit]))
    result = asyncio.run(
        tool({"text": "근거", "accident_date": "", "filters": filters, "top_k": 1})
    )
    assert result == [hit]


@needs_corpus
def test_rejects_page_or_text_not_in_index(monkeypatch):
    original = next(iter(_load("fault_standards")[0].values()))
    bad = deepcopy(original)
    bad["page_start"] += 1
    hit = {"chunk": bad, "score": 0.8, "retrieval_version": "test"}
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit]))
    with pytest.raises(ValueError, match="indexed source"):
        asyncio.run(
            search_fault_standards({"text": "근거", "accident_date": "", "filters": {}, "top_k": 1})
        )


@needs_corpus
def test_rejects_result_outside_requested_chart(monkeypatch):
    case = next(iter(_load("dispute_cases")[0].values()))
    hit = {"chunk": case, "score": 0.8, "retrieval_version": "test"}
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit]))
    with pytest.raises(ValueError, match="requested filters"):
        asyncio.run(
            search_dispute_cases(
                {
                    "text": "근거",
                    "accident_date": "",
                    "filters": {"chart_id": "차999-1"},
                    "top_k": 1,
                }
            )
        )
