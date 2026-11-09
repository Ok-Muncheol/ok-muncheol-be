import asyncio
from unittest.mock import AsyncMock

import pytest

from app.retrieval import common
from app.tools.dispute_cases import search_dispute_cases
from app.tools.fault_standards import search_fault_standards
from app.tools.policy_terms import search_policy_terms


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
