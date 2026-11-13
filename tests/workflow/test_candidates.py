"""C 검색 결과·레코드 → 도표 후보 변환. 검수 전 레코드는 선택 대상이 아니다."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.retrieval import common
from app.tools import _evidence
from app.workflow.candidates import (
    CHART_SEARCH_TOP_K,
    build_search_query,
    candidates_from_hits,
    confirmed_values,
    retrieve_from_corpus,
)
from tests.test_search_tools import SyntheticStore, hit_for
from tests.workflow.synthetic import DOCUMENT, card, field, hit


@pytest.fixture
def db_index():
    """C의 합성 DB 색인(SyntheticStore)을 등록한다. 테스트가 끝나면 해제한다."""
    store = SyntheticStore()
    _evidence.configure_evidence_store(lambda: store)
    yield store
    _evidence.configure_evidence_store(None)


def test_default_retriever_reads_chart_records_from_db_index(monkeypatch, db_index):
    hits = [hit_for(db_index, "fault_standards"), hit_for(db_index, "fault_standards", "fs-guide")]
    search = AsyncMock(return_value=hits)
    monkeypatch.setattr(common, "search", search)
    [cand] = asyncio.run(retrieve_from_corpus(card()))
    # 수정요소 해설 청크는 부모 레코드가 없어 후보가 되지 않는다
    assert cand["chart_id"] == "합1-1"
    assert cand["parent_id"] == "synthetic-fault_standards:합1-1"
    # 합성 레코드는 검수 상태가 아니므로 선택·계산 대상이 아니다
    assert cand["calculation_ready"] is False
    request = search.await_args.args[0]
    assert request["corpus"] == "fault_standards"
    assert request["text"] == "신호 교차로 A 녹색 직진 B 적색 좌회전"


def test_default_retriever_searches_deep_enough_for_reviewed_charts(monkeypatch):
    """검수 도표가 상위 10개 밖이어도 후보가 되게 깊게 검색한다(시범 실행에서 차4-2가 28위)."""
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(common, "search", search)
    asyncio.run(retrieve_from_corpus(card()))
    assert search.await_args.args[0]["top_k"] == CHART_SEARCH_TOP_K == 50


def test_default_retriever_needs_no_index_when_nothing_found(monkeypatch):
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[]))
    assert asyncio.run(retrieve_from_corpus(card())) == []


def test_default_retriever_fails_closed_without_db_index(monkeypatch):
    found = [hit_for(SyntheticStore(), "fault_standards")]
    monkeypatch.setattr(common, "search", AsyncMock(return_value=found))
    with pytest.raises(RuntimeError, match="not configured"):
        asyncio.run(retrieve_from_corpus(card()))


def record(chart_id: str, **extra: object) -> dict:
    return {
        "parent_id": f"{DOCUMENT['document_id']}:{chart_id}", "chart_id": chart_id,
        "document": DOCUMENT, "title": f"synthetic {chart_id}", "page_start": 10, "page_end": 12,
        "review_status": "unreviewed", "calculation_ready": False, **extra,
    }


def test_confirmed_values_only_returns_human_confirmed_scope_fields():
    c = card()
    c["fields"]["movement_B"] = field("left_turn", confirmed=False)
    c["fields"]["party_count"] = field(2)
    values = confirmed_values(c)
    assert "movement_B" not in values and "party_count" not in values
    assert values["night"] == "false" and values["signal_B"] == "red"


def test_search_query_uses_korean_words_without_attribute_filters():
    query = build_search_query(card())
    assert query["text"] == "신호 교차로 A 녹색 직진 B 적색 좌회전"
    assert query["filters"] == {} and query["accident_date"] == "2026-10-01"


def test_search_query_sends_an_empty_date_when_the_card_has_none():
    # 검색 도구는 YYYY-MM-DD나 빈 값만 받는다. None을 넘기면 판단 전체가 실패했다
    assert build_search_query(card(accident_date=None))["accident_date"] == ""


def test_unreviewed_record_becomes_candidate_that_cannot_be_selected():
    parent = f"{DOCUMENT['document_id']}:X-1"
    records = {parent: record("X-1", scope_attributes={"movement_A": "straight"})}
    [cand] = candidates_from_hits([hit(parent, 10, 0.7)], records)
    assert cand["calculation_ready"] is False
    assert cand["attributes"] == {} and cand["base_variants"] == []
    assert cand["reasons"] == ["검수 전 도표 레코드: 선택·계산에 쓰지 않음"]


def test_reviewed_record_groups_hits_and_keeps_best_score():
    parent = f"{DOCUMENT['document_id']}:X-2"
    records = {
        parent: record(
            "X-2", review_status="reviewed", calculation_ready=True,
            scope_attributes={"movement_A": "straight"},
            scope_variants=[{"variant": None, "base_A": 30, "base_B": 70, "conditions": {}}],
        ),
        "synthetic-standards:modifier-guide:1": None,
    }
    hits = [hit(parent, 10, 0.4), hit(parent, 11, 0.9),
            hit("synthetic-standards:modifier-guide:1", 1, 1.0)]
    [cand] = candidates_from_hits(hits, {k: v for k, v in records.items() if v})
    assert cand["calculation_ready"] is True
    assert cand["score"] == 0.9 and len(cand["hits"]) == 2
    assert cand["attributes"] == {"movement_A": "straight"}


def test_candidate_reads_edition_and_period_from_validated_hits():
    """레코드 안 문서 사본이 파싱 시점 값(시행일 빈 값)이어도 검색 결과의 문서 값을 쓴다."""
    parent = f"{DOCUMENT['document_id']}:X-3"
    stale = {**DOCUMENT, "edition": "parsed-copy", "effective_from": ""}
    [cand] = candidates_from_hits([hit(parent, 10, 0.5)], {parent: record("X-3", document=stale)})
    assert (cand["edition"], cand["effective_from"]) == ("synthetic-1", "2020-01-01")


def test_candidate_rejects_hits_from_another_document():
    parent = f"{DOCUMENT['document_id']}:X-4"
    other = {**DOCUMENT, "document_id": "other-standards"}
    with pytest.raises(ValueError, match="원문 문서가 다르다"):
        candidates_from_hits([hit(parent, 10, 0.5)], {parent: record("X-4", document=other)})


def test_candidate_reads_published_month_from_validated_hits():
    """발행월도 검색 결과의 문서 값에서 읽는다. 이전 색인처럼 키가 없으면 None이다."""
    parent = f"{DOCUMENT['document_id']}:X-5"
    with_month = hit(parent, 10, 0.5)
    with_month["chunk"]["document"] = {**DOCUMENT, "published_month": "2023-06"}
    [cand] = candidates_from_hits([with_month], {parent: record("X-5")})
    assert cand["published_month"] == "2023-06"
    [old] = candidates_from_hits([hit(parent, 10, 0.5)], {parent: record("X-5")})
    assert old["published_month"] is None
