"""D: 공통 RAG 뼈대. 모든 청크·문서는 합성이다. 외부 패키지·모델·DB 없이 돌아간다.

실행: py -3.12 -m unittest tests.test_d_retrieval
"""

import asyncio
import unittest
from unittest.mock import patch

from app.retrieval.common import (
    EmbeddingMismatch,
    RetrievalNotConfigured,
    configure_retrieval,
    fetch_by_parent,
    search,
)
from app.retrieval.embedding import EmbeddingSpec, FakeEmbedder, embed_query
from app.retrieval.hybrid import CorpusMixError, RetrievalOptions, retrieve
from app.retrieval.index import InMemoryIndex
from app.tools.dispute_cases import search_dispute_cases
from app.tools.fault_standards import search_fault_standards
from app.tools.policy_terms import search_policy_terms
from tests.evidence_d import skip_validation

EMBEDDER = FakeEmbedder()
VECTOR = RetrievalOptions(mode="vector")
ACCIDENT = "2026-10-20"


def document(corpus, edition="synthetic-2023", start="2023-01-01", end=None, doc_id=None):
    return {
        "document_id": doc_id or f"synthetic-{corpus}-{edition}",
        "corpus": corpus,
        "source_uri": f"synthetic://{corpus}/{edition}",
        "edition": edition,
        "effective_from": start,
        "effective_to": end,
        "content_hash": "synthetic-hash",
        "parser_version": "synthetic-parser",
    }


def chunk(chunk_id, text, doc, metadata=None, parent_id=None, page=7):
    return {
        "chunk_id": chunk_id,
        "parent_id": parent_id or f"parent-{chunk_id}",
        "document": doc,
        "page_start": page,
        "page_end": page + 1,
        "text": text,
        "metadata": metadata or {},
    }


def embedded(c, embedder=EMBEDDER):
    return {
        "chunk": c,
        "embedding": asyncio.run(embedder.embed(c["text"])),
        "embedding_model": embedder.model,
        "embedding_dimensions": embedder.dimensions,
        "embedding_version": embedder.version,
    }


def request(corpus, text="신호교차로 직진 충돌", filters=None, top_k=3, accident_date=ACCIDENT):
    return {
        "corpus": corpus,
        "text": text,
        "accident_date": accident_date,
        "filters": filters or {},
        "top_k": top_k,
    }


class CountingEmbedder(FakeEmbedder):
    calls = 0

    async def embed(self, text):
        type(self).calls += 1
        return await super().embed(text)


class RetrievalBase(unittest.TestCase):
    def setUp(self):
        self.fault_old = chunk(
            "f-old",
            "신호교차로 직진 충돌 구판 도표",
            document("fault_standards", "synthetic-2019", "2019-01-01", "2022-12-31"),
        )
        self.fault_new = chunk(
            "f-new",
            "신호교차로 직진 충돌 신판 도표",
            document("fault_standards", "synthetic-2023", "2023-01-01"),
        )
        self.case_after = chunk(
            "d-after",
            "신호교차로 직진 충돌 결정례",
            document("dispute_cases", "synthetic-2027", "2027-03-01"),
            {"chart_id": "chart-23"},
        )
        self.case_other = chunk(
            "d-other",
            "신호교차로 직진 충돌 다른 도표 결정례",
            document("dispute_cases", "synthetic-2023"),
            {"chart_id": "chart-24"},
        )
        self.term_2020 = chunk(
            "t-2020",
            "음주 운전 자기차량손해 면책 조항",
            document("policy_terms", "terms-2020", "2020-01-01", "2021-12-31"),
            {"coverage": "own_damage"},
        )
        self.term_2024 = chunk(
            "t-2024",
            "음주 운전 자기차량손해 면책 조항",
            document("policy_terms", "terms-2024", "2024-01-01"),
            {"coverage": "own_damage"},
        )
        self.term_liability = chunk(
            "t-liab",
            "음주 운전 대인배상 조항",
            document("policy_terms", "terms-2024", "2024-01-01"),
            {"coverage": "liability"},
        )
        self.index = InMemoryIndex(
            [
                embedded(c)
                for c in (
                    self.fault_old,
                    self.fault_new,
                    self.case_after,
                    self.case_other,
                    self.term_2020,
                    self.term_2024,
                    self.term_liability,
                )
            ]
        )
        configure_retrieval(
            index=self.index,
            embedder=EMBEDDER,
            options=VECTOR,
            default_terms_edition=None,
        )

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )

    def ids(self, hits):
        return [h["chunk"]["chunk_id"] for h in hits]

    def search(self, *args, **kw):
        return asyncio.run(search(request(*args, **kw)))


class CorpusScopeTest(RetrievalBase):
    def test_corpora_do_not_mix(self):
        for corpus in ("fault_standards", "dispute_cases"):
            hits = self.search(corpus, top_k=10)
            self.assertTrue(hits)
            self.assertEqual({h["chunk"]["document"]["corpus"] for h in hits}, {corpus})

    def test_no_fallback_to_other_corpus(self):
        hits = self.search(
            "fault_standards", accident_date="2010-01-01"
        )  # 도표는 이 날짜에 유효한 판이 없음
        self.assertEqual(hits, [])

    def test_unknown_corpus_rejected(self):
        with self.assertRaises(ValueError):
            self.search("web_search")

    def test_index_returning_other_corpus_is_an_error(self):
        wrong = self.term_2024

        class BrokenIndex:
            spec = EmbeddingSpec(EMBEDDER.model, EMBEDDER.dimensions, EMBEDDER.version)

            async def search_vector(self, search_filter, query_vector, limit):
                return [(wrong, 0.9)]

        configure_retrieval(index=BrokenIndex())
        with self.assertRaises(CorpusMixError):
            self.search("fault_standards")


class FaultStandardDateTest(RetrievalBase):
    def test_accident_date_selects_edition(self):
        self.assertEqual(
            self.ids(self.search("fault_standards", accident_date="2020-06-01")), ["f-old"]
        )
        self.assertEqual(
            self.ids(self.search("fault_standards", accident_date=ACCIDENT)), ["f-new"]
        )

    def test_boundaries_are_inclusive(self):
        self.assertEqual(
            self.ids(self.search("fault_standards", accident_date="2022-12-31")), ["f-old"]
        )
        self.assertEqual(
            self.ids(self.search("fault_standards", accident_date="2023-01-01")), ["f-new"]
        )

    def test_invalid_accident_date_rejected(self):
        with self.assertRaises(ValueError):
            self.search("fault_standards", accident_date="20번째 날")


class EmptyAccidentDateTest(RetrievalBase):
    def test_empty_accident_date_means_no_date_filter(self):
        hits = self.search("fault_standards", accident_date="", top_k=10)
        self.assertEqual(sorted(self.ids(hits)), ["f-new", "f-old"])  # 개정 전 판도 나옴


class DisputeCaseTest(RetrievalBase):
    def test_decisions_after_accident_are_included(self):
        self.assertIn("d-after", self.ids(self.search("dispute_cases", accident_date="2026-01-01")))

    def test_chart_id_filters_by_metadata(self):
        self.assertEqual(
            self.ids(self.search("dispute_cases", filters={"chart_id": "chart-23"})), ["d-after"]
        )
        self.assertEqual(self.search("dispute_cases", filters={"chart_id": "chart-99"}), [])


class PolicyTermsTest(RetrievalBase):
    def test_terms_edition_used_as_given(self):
        hits = self.search(
            "policy_terms",
            "음주 운전 면책",
            {"edition": "terms-2024", "coverage": "own_damage"},
        )
        self.assertEqual(self.ids(hits), ["t-2024"])

    def test_accident_date_does_not_replace_edition(self):
        # 사고 일자는 terms-2020의 적용 기간 밖이지만, 계약 적용판이 terms-2020이면 그 판을 쓴다
        hits = self.search("policy_terms", "음주 운전 면책", {"edition": "terms-2020"})
        self.assertEqual(self.ids(hits), ["t-2020"])

    def test_missing_or_none_edition_returns_empty_with_warning(self):
        CountingEmbedder.calls = 0
        configure_retrieval(embedder=CountingEmbedder())
        for filters in (
            {"coverage": "own_damage"},
            {"coverage": "own_damage", "edition": None},
        ):
            with self.subTest(filters=filters), self.assertLogs("app.retrieval.hybrid", "WARNING"):
                self.assertEqual(self.search("policy_terms", "음주 운전 면책", filters), [])
        self.assertEqual(CountingEmbedder.calls, 0)  # 임베딩 비용을 쓰지 않음

    def test_metadata_filter_is_not_relaxed(self):
        hits = self.search(
            "policy_terms",
            "음주 운전 면책",
            {"edition": "terms-2024", "coverage": "liability"},
        )
        self.assertEqual(self.ids(hits), ["t-liab"])
        self.assertEqual(
            self.search("policy_terms", "음주", {"edition": "terms-2024", "coverage": "none"}),
            [],
        )

    def test_missing_metadata_key_is_excluded(self):
        self.assertEqual(self.search("fault_standards", filters={"chart_id": "chart-23"}), [])
        self.assertEqual(self.search("fault_standards", filters={"chart_id": None}), [])

    def test_request_is_not_modified(self):
        req = request("policy_terms", filters={"edition": "terms-2024", "coverage": "own_damage"})
        before = {**req, "filters": dict(req["filters"])}
        asyncio.run(search(req))
        self.assertEqual(req, before)


class RealDataRulesTest(RetrievalBase):
    """C의 실제 산출물 모양에 맞춘 규칙(합성 청크로 확인)."""

    def unknown_period(self, chunk_id, corpus, edition="synthetic-unknown", metadata=None):
        doc = document(corpus, edition, start="", doc_id=f"doc-{chunk_id}")
        return chunk(chunk_id, "신호교차로 직진 충돌 기간 모름", doc, metadata)

    def test_unknown_period_passes_date_filter_and_warns_once(self):
        rows = [embedded(self.unknown_period(i, "fault_standards")) for i in ("u1",)]
        rows.append(
            embedded(
                chunk("u2", "같은 문서 다른 청크", rows[0]["chunk"]["document"]),
            )
        )
        with self.assertLogs("app.retrieval.index", "WARNING") as logs:
            self.index.add(rows)
        self.assertEqual(len(logs.records), 1)  # 문서당 한 번
        hits = self.search("fault_standards", accident_date="2010-01-01", top_k=10)
        self.assertEqual(sorted(self.ids(hits)), ["u1", "u2"])  # 기간 아는 문서는 그대로 걸러짐

    def test_bad_nonempty_period_still_rejected(self):
        doc = document("fault_standards", start="2023.6", doc_id="doc-bad")
        with self.assertRaises(ValueError):
            self.index.add([embedded(chunk("bad", "합성", doc))])

    def test_document_id_compares_document_field(self):
        hits = self.search(
            "fault_standards", filters={"document_id": "synthetic-fault_standards-synthetic-2023"}
        )
        self.assertEqual(self.ids(hits), ["f-new"])
        self.assertEqual(self.search("fault_standards", filters={"document_id": "없음"}), [])

    def test_metadata_list_value_passes_when_it_contains_filter(self):
        doc = document("dispute_cases", "synthetic-list", doc_id="doc-list")
        c = chunk(
            "d-list", "신호교차로 직진 충돌", doc, {"chart_id_candidates": ["차1-1", "차2-2"]}
        )
        self.index.add([embedded(c)])
        for value, expected in (
            ("차2-2", ["d-list"]),
            (["차1-1", "차2-2"], ["d-list"]),
            ("차9", []),
        ):
            with self.subTest(value=value):
                hits = self.search("dispute_cases", filters={"chart_id_candidates": value})
                self.assertEqual(self.ids(hits), expected)
        self.assertEqual(self.search("dispute_cases", filters={"chart_id": "차2-2"}), [])  # 키 유지

    def test_filter_list_means_one_of(self):
        filters = {"edition": "terms-2024", "coverage": ["own_damage", "liability"]}
        hits = self.search("policy_terms", "음주 운전 면책", filters, top_k=10)
        self.assertEqual(sorted(self.ids(hits)), ["t-2024", "t-liab"])
        filters["coverage"] = ["liability", "none"]
        hits = self.search("policy_terms", "음주 운전 면책", filters, top_k=10)
        self.assertEqual(self.ids(hits), ["t-liab"])

    def test_empty_filter_list_matches_nothing(self):
        filters = {"edition": "terms-2024", "coverage": []}
        self.assertEqual(self.search("policy_terms", "음주 운전 면책", filters), [])

    def test_filter_list_against_metadata_list(self):
        doc = document("dispute_cases", "synthetic-list", doc_id="doc-list")
        c = chunk(
            "d-list", "신호교차로 직진 충돌", doc, {"chart_id_candidates": ["차1-1", "차2-2"]}
        )
        self.index.add([embedded(c)])
        hits = self.search("dispute_cases", filters={"chart_id_candidates": ["차9", "차2-2"]})
        self.assertEqual(self.ids(hits), ["d-list"])
        self.assertEqual(self.search("dispute_cases", filters={"chart_id_candidates": ["차9"]}), [])

    def test_search_does_not_filter_role_by_itself(self):
        edition = {"edition": "terms-role"}
        doc = document("policy_terms", "terms-role", doc_id="doc-role")
        primary = chunk("t-primary", "음주 운전 면책", doc, {"retrieval_role": "primary_clause"})
        side = chunk(
            "t-side", "음주 운전 면책 부록", doc, {"retrieval_role": "auxiliary_reference"}
        )
        self.index.add([embedded(primary), embedded(side)])
        hits = self.search("policy_terms", "음주 운전 면책", edition, top_k=10)
        self.assertEqual(sorted(self.ids(hits)), ["t-primary", "t-side"])  # 넘기지 않으면 둘 다
        hits = self.search(
            "policy_terms",
            "음주 운전 면책",
            {**edition, "retrieval_role": "primary_clause"},
            top_k=10,
        )
        self.assertEqual(self.ids(hits), ["t-primary"])

    def test_default_terms_edition_used_when_missing(self):
        configure_retrieval(default_terms_edition="terms-2024")
        for filters in ({}, {"edition": None}):
            with self.subTest(filters=filters):
                hits = self.search("policy_terms", "음주 운전 면책", filters, top_k=10)
                self.assertEqual(sorted(self.ids(hits)), ["t-2024", "t-liab"])

    def test_explicit_terms_edition_wins_over_default(self):
        configure_retrieval(default_terms_edition="terms-2024")
        hits = self.search("policy_terms", "음주 운전 면책", {"edition": "terms-2020"})
        self.assertEqual(self.ids(hits), ["t-2020"])

    def test_default_terms_edition_only_applies_to_policy_terms(self):
        configure_retrieval(default_terms_edition="terms-2024")
        self.assertEqual(self.ids(self.search("fault_standards")), ["f-new"])
        with self.assertLogs("app.retrieval.hybrid", "WARNING"):
            self.assertEqual(self.search("fault_standards", filters={"edition": None}), [])


class ResultTest(RetrievalBase):
    def test_top_k_and_order(self):
        hits = self.search(
            "policy_terms",
            "음주 운전 자기차량손해 면책 조항",
            {"edition": "terms-2024"},
            top_k=1,
        )
        self.assertEqual(self.ids(hits), ["t-2024"])
        hits = self.search(
            "policy_terms",
            "음주 운전 자기차량손해 면책 조항",
            {"edition": "terms-2024"},
            top_k=5,
        )
        self.assertEqual(self.ids(hits), ["t-2024", "t-liab"])
        self.assertGreater(hits[0]["score"], hits[1]["score"])

    def test_invalid_top_k_rejected(self):
        for top_k in (0, -1):
            with self.subTest(top_k=top_k), self.assertRaises(ValueError):
                self.search("fault_standards", top_k=top_k)

    def test_source_and_version_preserved(self):
        hit = self.search("fault_standards")[0]
        self.assertEqual(hit["chunk"], self.fault_new)
        self.assertEqual(
            (
                hit["chunk"]["document"]["document_id"],
                hit["chunk"]["page_start"],
                hit["chunk"]["page_end"],
                hit["chunk"]["parent_id"],
            ),
            ("synthetic-fault_standards-synthetic-2023", 7, 8, "parent-f-new"),
        )
        self.assertEqual(hit["retrieval_version"], "vector-v1:fake-hash:test")

    def test_no_match_is_empty_list(self):
        self.assertEqual(self.search("dispute_cases", filters={"chart_id": "없음"}), [])

    def test_empty_index_is_empty_list(self):
        configure_retrieval(index=InMemoryIndex())
        self.assertEqual(self.search("fault_standards"), [])

    def test_blank_query_rejected(self):
        with self.assertRaises(ValueError):
            self.search("fault_standards", text="  ")


class EmbeddingTest(RetrievalBase):
    def test_query_embedder_mismatch_rejected(self):
        for other in (
            FakeEmbedder(model="other"),
            FakeEmbedder(dimensions=32),
            FakeEmbedder(version="v2"),
        ):
            with self.subTest(other=other):
                configure_retrieval(embedder=other)
                with self.assertRaises(EmbeddingMismatch):
                    self.search("fault_standards")

    def test_returned_vector_length_checked(self):
        class ShortEmbedder(FakeEmbedder):
            async def embed(self, text):
                return [1.0]

        configure_retrieval(embedder=ShortEmbedder())
        with self.assertRaises(EmbeddingMismatch):
            self.search("fault_standards")

    def test_index_rejects_mixed_models_and_bad_vectors(self):
        with self.assertRaises(EmbeddingMismatch):
            self.index.add([embedded(self.fault_new, FakeEmbedder(model="other"))])
        bad = {**embedded(self.fault_new), "embedding": [1.0]}
        with self.assertRaises(EmbeddingMismatch):
            InMemoryIndex([bad])

    def test_index_rejects_bad_document_date(self):
        doc = document("fault_standards", start="언젠가")
        with self.assertRaises(ValueError):
            InMemoryIndex([embedded(chunk("bad", "합성", doc))])

    def test_same_chunk_id_replaced(self):
        before = len(self.search("fault_standards", accident_date=ACCIDENT, top_k=10))
        self.index.add([embedded(self.fault_new)])
        self.assertEqual(
            len(self.search("fault_standards", accident_date=ACCIDENT, top_k=10)), before
        )

    def test_fake_embedder_is_deterministic(self):
        spec = (EMBEDDER.model, EMBEDDER.dimensions, EMBEDDER.version)
        self.assertEqual(
            asyncio.run(embed_query("같은 질의", *spec)),
            asyncio.run(embed_query("같은 질의", *spec)),
        )


class FetchByParentTest(RetrievalBase):
    def fetch(self, corpus, parent_ids, **kw):
        return asyncio.run(fetch_by_parent(corpus, parent_ids, **kw))

    def add_pages(self):
        doc = document("policy_terms", "terms-2024", "2024-01-01")
        rows = [chunk("p-b", "둘째 쪽", doc, parent_id="art-23", page=68)]
        rows.append(chunk("p-a", "첫째 쪽", doc, parent_id="art-23", page=67))
        rows.append(chunk("p-c", "다른 조항", doc, parent_id="art-24", page=69))
        self.index.add([embedded(c) for c in rows])

    def test_returns_chunks_in_page_order_with_score_one(self):
        self.add_pages()
        hits = self.fetch("policy_terms", ["art-23"], terms_edition="terms-2024")
        self.assertEqual(self.ids(hits), ["p-a", "p-b"])
        self.assertEqual({h["score"] for h in hits}, {1.0})
        self.assertEqual(hits[0]["retrieval_version"], "direct-v1:fake-hash:test")
        self.assertEqual(hits[0]["chunk"]["page_start"], 67)

    def test_keeps_requested_parent_order_and_removes_duplicates(self):
        self.add_pages()
        hits = self.fetch(
            "policy_terms", ["art-24", "art-23", "art-24"], terms_edition="terms-2024"
        )
        self.assertEqual(self.ids(hits), ["p-c", "p-a", "p-b"])

    def test_missing_parent_is_skipped(self):
        self.add_pages()
        hits = self.fetch("policy_terms", ["없음", "art-24"], terms_edition="terms-2024")
        self.assertEqual(self.ids(hits), ["p-c"])
        self.assertEqual(self.fetch("policy_terms", ["없음"], terms_edition="terms-2024"), [])
        self.assertEqual(self.fetch("policy_terms", [], terms_edition="terms-2024"), [])

    def test_edition_rules_match_search(self):
        self.add_pages()
        self.assertEqual(self.fetch("policy_terms", ["art-23"], terms_edition="terms-2020"), [])
        with self.assertLogs("app.retrieval.hybrid", "WARNING"):
            self.assertEqual(self.fetch("policy_terms", ["art-23"]), [])  # 판도 기본 판도 없음
        configure_retrieval(default_terms_edition="terms-2024")
        self.assertEqual(self.ids(self.fetch("policy_terms", ["art-23"])), ["p-a", "p-b"])
        hits = self.fetch("policy_terms", ["art-23"], terms_edition="terms-2020")  # 명시한 판 우선
        self.assertEqual(hits, [])

    def test_corpus_is_kept(self):
        self.add_pages()
        self.assertEqual(self.fetch("fault_standards", ["art-23"]), [])
        hits = self.fetch("fault_standards", [self.fault_new["parent_id"]])  # 판 없이도 조회
        self.assertEqual(self.ids(hits), ["f-new"])
        with self.assertRaises(ValueError):
            self.fetch("web_search", ["art-23"])

    def test_no_embedder_needed(self):
        self.add_pages()
        configure_retrieval(embedder=None)
        hits = self.fetch("policy_terms", ["art-23"], terms_edition="terms-2024")
        self.assertEqual(self.ids(hits), ["p-a", "p-b"])

    def test_not_configured(self):
        configure_retrieval(index=None)
        with self.assertRaises(RetrievalNotConfigured):
            self.fetch("policy_terms", ["art-23"], terms_edition="terms-2024")

    def test_index_returning_other_corpus_is_an_error(self):
        wrong = self.fault_new

        class BrokenIndex:
            spec = EmbeddingSpec(EMBEDDER.model, EMBEDDER.dimensions, EMBEDDER.version)

            async def fetch_by_parent(self, corpus, parent_ids, *, edition=None):
                return [wrong]

        configure_retrieval(index=BrokenIndex())
        with self.assertRaises(CorpusMixError):
            self.fetch("policy_terms", ["art-23"], terms_edition="terms-2024")

    def test_edition_passed_to_index_for_replacement(self):
        seen = []

        class OtherIndex:
            spec = EmbeddingSpec(EMBEDDER.model, EMBEDDER.dimensions, EMBEDDER.version)

            async def fetch_by_parent(self, corpus, parent_ids, *, edition=None):
                seen.append((corpus, parent_ids, edition))
                return []

        configure_retrieval(index=OtherIndex(), default_terms_edition="terms-2024")
        self.fetch("policy_terms", ["art-23"])
        self.assertEqual(seen, [("policy_terms", ["art-23"], "terms-2024")])


class ConfigurationTest(unittest.TestCase):
    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )

    def test_not_configured_is_not_empty_result(self):
        configure_retrieval(index=None, embedder=None)
        with self.assertRaises(RetrievalNotConfigured):
            asyncio.run(search(request("fault_standards")))
        self.assertTrue(issubclass(RetrievalNotConfigured, NotImplementedError))
        configure_retrieval(index=InMemoryIndex())  # 임베더만 없어도 미연결
        with self.assertRaises(RetrievalNotConfigured):
            asyncio.run(search(request("fault_standards")))

    def test_default_is_rrf_and_rerank_refuses_to_turn_on(self):
        options = RetrievalOptions()
        self.assertEqual((options.mode, options.use_rerank), ("rrf", False))
        with self.assertRaises(NotImplementedError):
            RetrievalOptions(use_rerank=True)

    def test_index_can_be_swapped(self):
        seen = []

        class OtherIndex:
            spec = EmbeddingSpec(EMBEDDER.model, EMBEDDER.dimensions, EMBEDDER.version)

            async def search_vector(self, search_filter, query_vector, limit):
                seen.append(
                    (search_filter.corpus, search_filter.edition, search_filter.metadata, limit)
                )
                return []

        configure_retrieval(index=OtherIndex(), embedder=EMBEDDER, options=VECTOR)
        asyncio.run(
            search(
                request(
                    "policy_terms",
                    filters={"edition": "terms-2024", "coverage": "own_damage"},
                    top_k=2,
                )
            )
        )
        self.assertEqual(seen, [("policy_terms", "terms-2024", {"coverage": "own_damage"}, 2)])

    def test_retrieve_directly_with_missing_edition(self):
        with self.assertLogs("app.retrieval.hybrid", "WARNING"):
            self.assertEqual(
                asyncio.run(retrieve(request("policy_terms"), [1.0], index=InMemoryIndex())), []
            )


class ToolIntegrationTest(RetrievalBase):
    """C의 세 도구가 D의 common.search를 통해 동작하는지. 합성 청크라 결과 검증만 끈다.
    실제 validate_hits까지 거치는 시험은 test_d_retrieval_real_data.py에 있다."""

    def setUp(self):
        super().setUp()
        for tool in ("policy_terms", "fault_standards", "dispute_cases"):
            patcher = patch(f"app.tools.{tool}.validate_hits", skip_validation)
            patcher.start()
            self.addCleanup(patcher.stop)

    def query(self, filters=None, **kw):
        return {
            "text": "음주 운전 면책",
            "accident_date": ACCIDENT,
            "filters": filters or {},
            "top_k": 3,
            **kw,
        }

    def test_search_policy_terms_goes_through_common_search(self):
        hits = asyncio.run(
            search_policy_terms(self.query({"edition": "terms-2024", "coverage": "own_damage"}))
        )
        self.assertEqual(self.ids(hits), ["t-2024"])
        self.assertTrue(hits[0]["retrieval_version"].startswith("vector-v1:"))

    def test_tool_keeps_its_corpus_even_if_caller_passes_another(self):
        hits = asyncio.run(search_fault_standards(self.query(corpus="policy_terms")))
        self.assertEqual({h["chunk"]["document"]["corpus"] for h in hits}, {"fault_standards"})
        hits = asyncio.run(search_dispute_cases(self.query(corpus="fault_standards")))
        self.assertEqual({h["chunk"]["document"]["corpus"] for h in hits}, {"dispute_cases"})

    def test_tool_raises_not_configured_when_unconnected(self):
        configure_retrieval(index=None)
        with self.assertRaises(NotImplementedError):
            asyncio.run(search_policy_terms(self.query({"edition": "terms-2024"})))


if __name__ == "__main__":
    unittest.main()
