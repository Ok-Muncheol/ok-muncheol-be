"""세 검색 도구 — 합성 DB 견본만 쓴다. 실제 코퍼스 원문·data/private 파일 없이 돈다."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.retrieval import common
from app.tools import _evidence
from app.tools.dispute_cases import search_dispute_cases
from app.tools.fault_standards import search_fault_standards
from app.tools.policy_terms import search_policy_terms

HASH = "0" * 64


def _document(corpus, edition="example-v1"):
    return {
        "document_id": f"synthetic-{corpus}",
        "corpus": corpus,
        "source_uri": f"storage://synthetic/{corpus}.pdf",
        "edition": edition,
        "effective_from": "",
        "effective_to": None,
        "content_hash": HASH,
        "parser_version": "synthetic-v1",
    }


class SyntheticStore:
    """corpus_chunks·corpus_records·corpus_source_pages를 흉내 내는 메모리 저장소."""

    def __init__(self):
        self.chunks, self.parents, self.related, self.pages = {}, {}, {}, set()
        fs, dc, pt = (_document(c) for c in ("fault_standards", "dispute_cases", "policy_terms"))
        pt["edition"] = "2099.01.01"
        for doc in (fs, dc, pt):
            self.pages |= {(doc["document_id"], page) for page in (1, 2, 3)}

        chart = {
            "parent_id": f"{fs['document_id']}:합1-1",
            "document": fs,
            "page_start": 1,
            "page_end": 2,
            "chart_id": "합1-1",
            "modifier_ids": ["합1-1:m1"],
        }
        self._add("fault_standards", chart, {"section": "chart"}, page=2)
        self.related[("fault_standards", "chart", "합1-1")] = chart
        self.related[("fault_standards", "modifier", "합1-1:m1")] = {"chart_id": "합1-1"}
        guide = f"{fs['document_id']}:modifier-guide:1"
        self.chunks[("fault_standards", "fs-guide")] = self._chunk(
            "fs-guide", guide, fs, {"section": "common_modifier_guide"}, page=3
        )

        link = {"legacy_chart_id": "구1", "chart_id": "합1-1", "source_pdf_page": 1}
        case = {
            "parent_id": f"{dc['document_id']}:사례-1",
            "document": dc,
            "page_start": 2,
            "page_end": 3,
            "case_id": "사례-1",
            "chart_id": "합1-1",
            "mapping_evidence": [link],
        }
        self._add("dispute_cases", case, {"case_id": "사례-1", "chart_id": "합1-1"}, page=2)
        self.related[("dispute_cases", "chart_mapping", _evidence.crosswalk_key(link))] = link

        note = {"parent_id": f"{pt['document_id']}:note-1", "document": pt, "page_start": 3,
                "page_end": 3}
        self.parents[("policy_terms", note["parent_id"])] = note
        clause = {
            "parent_id": f"{pt['document_id']}:제1조",
            "document": pt,
            "page_start": 1,
            "page_end": 1,
            "clause_id": "제1조",
            "coverage": "own_damage",
            "side_note_parent_ids": [note["parent_id"]],
        }
        self._add("policy_terms", clause, {"clause_id": "제1조", "coverage": "own_damage"}, page=1)
        schedule_item = {
            "parent_id": f"{pt['document_id']}:payment_standard:별표3:1:from-2023-01-01",
            "document": pt,
            "page_start": 2,
            "page_end": 2,
            "record_type": "payment_standard_item",
            "schedule": "별표3",
        }
        self.parents[("policy_terms", schedule_item["parent_id"])] = schedule_item
        self.chunks[("policy_terms", "pt-schedule")] = self._chunk(
            "pt-schedule",
            schedule_item["parent_id"],
            pt,
            {"clause_type": "payment_standard", "accident_date_from": "2023-01-01"},
            page=2,
        )

    @staticmethod
    def _chunk(chunk_id, parent_id, document, metadata, page):
        return {
            "chunk_id": chunk_id,
            "parent_id": parent_id,
            "document": document,
            "page_start": page,
            "page_end": page,
            "text": "합성 검색 근거",
            "metadata": metadata,
            # DB 행에는 계약 밖 칼럼도 있다. 비교 대상이 아니다.
            "raw_text": "합성 원문",
        }

    def _add(self, corpus, parent, metadata, page):
        self.parents[(corpus, parent["parent_id"])] = parent
        chunk_id = f"{corpus}-chunk"
        self.chunks[(corpus, chunk_id)] = self._chunk(
            chunk_id, parent["parent_id"], parent["document"], metadata, page
        )

    async def get_chunk(self, corpus, chunk_id):
        return self.chunks.get((corpus, chunk_id))

    async def get_parent(self, corpus, parent_id):
        return self.parents.get((corpus, parent_id))

    async def get_source_page(self, document_id, page):
        return {"content_hash": HASH} if (document_id, page) in self.pages else None

    async def get_related(self, corpus, kind, record_id):
        return self.related.get((corpus, kind, record_id))


@pytest.fixture
def store():
    synthetic = SyntheticStore()
    _evidence.configure_evidence_store(lambda: synthetic)
    yield synthetic
    _evidence.configure_evidence_store(None)


def hit_for(store, corpus, chunk_id=None):
    row = store.chunks[(corpus, chunk_id or f"{corpus}-chunk")]
    chunk = {field: deepcopy(row[field]) for field in _evidence.CHUNK_FIELDS}
    return {"chunk": chunk, "score": 0.8, "retrieval_version": "synthetic-v1"}


def query(filters=None, top_k=1):
    return {"text": "근거", "accident_date": "", "filters": filters or {}, "top_k": top_k}


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
    q = {
        "text": "  사고 근거  ",
        "accident_date": "",
        "filters": filters.copy(),
        "top_k": 5,
        "corpus": "wrong_corpus",
    }
    assert asyncio.run(tool(q)) == []
    request = search.await_args.args[0]
    assert request == {
        "corpus": corpus,
        "text": "사고 근거",
        "accident_date": "",
        "filters": filters,
        "top_k": 5,
    }
    assert q["text"] == "  사고 근거  "
    assert q["filters"] is not request["filters"]


def test_unreviewed_chart_attributes_cannot_be_hard_filters(monkeypatch):
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(common, "search", search)
    with pytest.raises(ValueError, match="Unsupported"):
        asyncio.run(search_fault_standards(query({"signal_A": "red"}, top_k=10)))
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
        asyncio.run(search_dispute_cases(query(top_k=0)))
    search.assert_not_awaited()


@pytest.mark.parametrize(
    ("tool", "corpus", "filters"),
    [
        (search_fault_standards, "fault_standards", {"chart_id": "합1-1"}),
        (search_dispute_cases, "dispute_cases", {"chart_id": "합1-1"}),
        (search_policy_terms, "policy_terms", {"edition": "2099.01.01", "coverage": "own_damage"}),
    ],
)
def test_hit_links_to_db_record_and_pdf_page(monkeypatch, store, tool, corpus, filters):
    hit = hit_for(store, corpus)
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit]))
    assert asyncio.run(tool(query(filters))) == [hit]


def test_common_modifier_guide_needs_no_chart_row(monkeypatch, store):
    hit = hit_for(store, "fault_standards", "fs-guide")
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit]))
    assert asyncio.run(search_fault_standards(query())) == [hit]


def test_rejects_page_or_text_not_in_index(monkeypatch, store):
    for field, value in (("page_start", 1), ("text", "바뀐 문장")):
        hit = hit_for(store, "fault_standards")
        hit["chunk"][field] = value
        monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit]))
        with pytest.raises(ValueError, match="indexed source"):
            asyncio.run(search_fault_standards(query()))


def test_rejects_page_missing_from_source_pdf(monkeypatch, store):
    store.pages.discard(("synthetic-policy_terms", 1))
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit_for(store, "policy_terms")]))
    with pytest.raises(ValueError, match="parsed PDF"):
        asyncio.run(search_policy_terms(query()))


def test_rejects_result_outside_requested_chart(monkeypatch, store):
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit_for(store, "dispute_cases")]))
    with pytest.raises(ValueError, match="requested filters"):
        asyncio.run(search_dispute_cases(query({"chart_id": "차999-1"})))


def test_rejects_hit_from_another_corpus(monkeypatch, store):
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit_for(store, "policy_terms")]))
    with pytest.raises(ValueError, match="unknown chunk_id"):
        asyncio.run(search_dispute_cases(query()))


def test_rejects_case_link_missing_from_crosswalk(monkeypatch, store):
    store.related = {k: v for k, v in store.related.items() if k[1] != "chart_mapping"}
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit_for(store, "dispute_cases")]))
    with pytest.raises(ValueError, match="crosswalk"):
        asyncio.run(search_dispute_cases(query()))


def test_schedule_filter_and_accident_window(monkeypatch, store):
    hit = hit_for(store, "policy_terms", "pt-schedule")
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit]))
    q = query({"schedule": "별표3"})
    q["accident_date"] = "2024-03-01"
    assert asyncio.run(search_policy_terms(q)) == [hit]
    # 사고일 범위는 공통 검색이 거른다. 범위 밖 항목이 그대로 오면 도구가 빼지 않고 검증이 막는다
    clause = hit_for(store, "policy_terms")
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit, clause]))
    q["accident_date"] = "2022-06-01"
    q["filters"] = {}
    q["top_k"] = 2
    with pytest.raises(ValueError, match="accident date range"):
        asyncio.run(search_policy_terms(q))


def test_validate_hits_rejects_item_outside_accident_window(store):
    # 공통 검색 등 다른 경로로 들어와도 검증이 막는다(안전장치)
    request = {**query(), "corpus": "policy_terms", "accident_date": "2022-06-01"}
    with pytest.raises(ValueError, match="accident date range"):
        asyncio.run(
            _evidence.validate_hits(request, [hit_for(store, "policy_terms", "pt-schedule")])
        )


def test_rejects_hits_without_db_store(monkeypatch):
    chunk = SyntheticStore().chunks[("dispute_cases", "dispute_cases-chunk")]
    hit = {"chunk": chunk, "score": 0.8, "retrieval_version": "synthetic-v1"}
    monkeypatch.setattr(common, "search", AsyncMock(return_value=[hit]))
    with pytest.raises(RuntimeError, match="not configured"):
        asyncio.run(search_dispute_cases(query()))
