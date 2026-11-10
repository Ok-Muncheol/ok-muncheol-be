"""C 검색 결과·레코드 → 도표 후보 변환. 검수 전 레코드는 선택 대상이 아니다."""

from app.workflow.candidates import build_search_query, candidates_from_hits, confirmed_values
from tests.workflow.synthetic import DOCUMENT, card, field, hit


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
