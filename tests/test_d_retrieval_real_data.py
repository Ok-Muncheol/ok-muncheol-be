"""D: C의 코퍼스 적재 파일(db_import)로 검색 경로와 로컬 EvidenceStore를 확인한다. 외부 API 없음.

질의 임베더가 아직 없으므로 청크 벡터 하나를 질의로 넣어 자기 자신이 1등으로 나오는지 본다.
검색 품질 평가가 아니다(품질은 C 평가셋과 실제 질의 임베더가 있어야 잰다).
"""

import asyncio
import unittest
from collections import Counter

from app.retrieval.common import configure_retrieval, fetch_by_parent, search
from app.retrieval.evidence_store import LocalCorpusEvidenceStore
from app.retrieval.hybrid import RetrievalOptions, retrieve
from app.retrieval.loader import DOCUMENT_FIELDS, build_index, corpus_terms_edition, read_corpus
from app.tools import _evidence
from app.tools._evidence import validate_hits
from app.tools.dispute_cases import search_dispute_cases
from app.tools.fault_standards import search_fault_standards
from app.tools.policy_terms import search_policy_terms
from tests.evidence_d import has_corpus

CORPUS_COUNTS = {"fault_standards": 923, "dispute_cases": 1130, "policy_terms": 544}
EDITION = "2026.11.11"
VECTOR = RetrievalOptions(mode="vector")


def request(corpus, filters=None, top_k=3):
    return {
        "corpus": corpus,
        "text": "청크 벡터로 질의",
        "accident_date": "2026-10-20",
        "filters": filters or {},
        "top_k": top_k,
    }


@unittest.skipUnless(has_corpus(), "data/private/corpus-db-import/db_import 파일이 없다")
class RealIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = read_corpus()
        with cls.assertLogs(cls(), "app.retrieval.index", "WARNING") as logs:
            cls.index = build_index(cls.corpus)
        cls.warnings = [r.getMessage() for r in logs.records]
        cls.rows = {}
        for row in cls.index._rows.values():
            cls.rows.setdefault(row["chunk"]["document"]["corpus"], []).append(row)
        # 실제 검증(C validate_hits)은 EvidenceStore가 있어야 돈다. DB 대신 같은 행의 로컬 어댑터.
        cls.store = LocalCorpusEvidenceStore(cls.corpus)
        _evidence.configure_evidence_store(lambda: cls.store)

    @classmethod
    def tearDownClass(cls):
        _evidence.configure_evidence_store(None)

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )

    def search_with_vector(self, corpus, row, filters=None):
        return asyncio.run(
            retrieve(
                request(corpus, filters),
                row["embedding"],
                index=self.index,
                options=VECTOR,
                default_terms_edition=EDITION,
            )
        )

    def test_all_chunks_loaded_with_one_embedding_spec(self):
        self.assertEqual({k: len(v) for k, v in self.rows.items()}, CORPUS_COUNTS)
        spec = self.index.spec
        self.assertEqual(
            (spec.model, spec.dimensions, spec.version),
            ("text-embedding-3-small", 1536, "text-embedding-3-small:1536:v1"),
        )

    def test_unknown_periods_warn_once_per_document(self):
        # 도표·결정례는 적용 기간 모름. 약관은 corpus_documents에 2026-11-11이 있다
        self.assertEqual(len(self.warnings), 2)

    def test_chunk_vector_finds_itself_first(self):
        for corpus, rows in self.rows.items():
            for row in rows[:: len(rows) // 5][:5]:
                with self.subTest(corpus=corpus, chunk_id=row["chunk"]["chunk_id"]):
                    hits = self.search_with_vector(corpus, row)
                    self.assertAlmostEqual(hits[0]["score"], 1.0, places=6)
                    top = self.index._rows[hits[0]["chunk"]["chunk_id"]]
                    # 같은 벡터의 청크가 있으면 chunk_id 순이므로 벡터가 같은지로 확인한다
                    self.assertEqual(top["embedding"], row["embedding"])

    def test_unique_vector_returns_the_chunk_itself(self):
        for corpus, rows in self.rows.items():
            vectors = [tuple(r["embedding"]) for r in rows]
            row = next(r for r, v in zip(rows, vectors, strict=True) if vectors.count(v) == 1)
            with self.subTest(corpus=corpus):
                hit = self.search_with_vector(corpus, row)[0]
                self.assertEqual(hit["chunk"], row["chunk"])  # 출처 보존
                self.assertEqual(
                    hit["retrieval_version"],
                    "vector-v1:text-embedding-3-small:text-embedding-3-small:1536:v1",
                )

    def test_evaluation_document_id_filter_passes_all_chunks(self):
        for corpus, rows in self.rows.items():
            document_id = rows[0]["chunk"]["document"]["document_id"]
            with self.subTest(corpus=corpus):
                filters = {"document_id": document_id}
                self.assertTrue(self.search_with_vector(corpus, rows[0], filters))
                self.assertEqual(self.search_with_vector(corpus, rows[0], {"document_id": "x"}), [])

    def test_fetch_article_23_directly(self):
        configure_retrieval(index=self.index, default_terms_edition=EDITION)
        parent = "samsung-personal-auto-2026-11-11:general_article:23:p67"
        hits = asyncio.run(fetch_by_parent("policy_terms", [parent]))
        self.assertEqual([h["chunk"]["page_start"] for h in hits], [67, 68])
        self.assertEqual({h["score"] for h in hits}, {1.0})
        self.assertIn("고의", hits[0]["chunk"]["text"])
        self.assertIn("무면허운전", hits[1]["chunk"]["text"])
        self.assertIn("음주", hits[1]["chunk"]["text"])
        other = asyncio.run(fetch_by_parent("policy_terms", [parent], terms_edition="다른 판"))
        self.assertEqual(other, [])

    def connect_with(self, row):
        class StoredVectorEmbedder:
            model = "text-embedding-3-small"
            dimensions = 1536
            version = "text-embedding-3-small:1536:v1"

            async def embed(self, text):
                return row["embedding"]

        # 저장된 벡터로 자기 자신을 찾는 시험이라 벡터만 쓴다(기본값은 RRF)
        configure_retrieval(
            index=self.index,
            embedder=StoredVectorEmbedder(),
            options=VECTOR,
            default_terms_edition=EDITION,
        )

    def test_every_loaded_chunk_passes_tool_validation(self):
        for corpus, rows in self.rows.items():
            with self.subTest(corpus=corpus):
                request = {
                    "corpus": corpus,
                    "text": "x",
                    "accident_date": "",
                    "filters": {},
                    "top_k": len(rows),
                }
                hits = [{"chunk": r["chunk"], "score": 0.5, "retrieval_version": "v"} for r in rows]
                self.assertEqual(len(asyncio.run(validate_hits(request, hits))), len(rows))

    def test_fetch_by_parent_hits_pass_tool_validation(self):
        configure_retrieval(index=self.index, default_terms_edition=EDITION)
        parent = "samsung-personal-auto-2026-11-11:general_article:23:p67"
        for edition in (None, EDITION):
            with self.subTest(edition=edition):
                hits = asyncio.run(fetch_by_parent("policy_terms", [parent], terms_edition=edition))
                request = {
                    "corpus": "policy_terms",
                    "text": "x",
                    "accident_date": "",
                    "filters": {"edition": EDITION},
                    "top_k": 3,
                }
                self.assertEqual(asyncio.run(validate_hits(request, hits)), hits)

    def test_tools_search_real_index_with_validation(self):
        cases = (
            (search_policy_terms, "policy_terms", {"edition": EDITION}, "2026-10-20"),
            (search_policy_terms, "policy_terms", {}, ""),  # 판 없음 → 기본 판
            (search_fault_standards, "fault_standards", {}, "2026-10-20"),
            (search_fault_standards, "fault_standards", {}, ""),
            (search_dispute_cases, "dispute_cases", {}, ""),
        )
        for tool, corpus, filters, accident_date in cases:
            with self.subTest(corpus=corpus, filters=filters, accident_date=accident_date):
                row = self.rows[corpus][0]
                self.connect_with(row)
                query = {
                    "text": "x",
                    "accident_date": accident_date,
                    "filters": filters,
                    "top_k": 3,
                }
                hits = asyncio.run(tool(query))
                self.assertEqual(hits[0]["chunk"], row["chunk"])

    def test_keyword_and_rrf_hits_pass_tool_validation(self):
        modes = (
            RetrievalOptions(mode="bm25"),
            RetrievalOptions(mode="rrf"),
        )
        for options in modes:
            for tool, corpus in (
                (search_policy_terms, "policy_terms"),
                (search_fault_standards, "fault_standards"),
                (search_dispute_cases, "dispute_cases"),
            ):
                with self.subTest(options=options, corpus=corpus):
                    row = self.rows[corpus][0]
                    self.connect_with(row)
                    configure_retrieval(options=options)
                    query = {
                        "text": row["chunk"]["text"][:60],
                        "accident_date": "",
                        "filters": {},
                        "top_k": 3,
                    }
                    hits = asyncio.run(tool(query))  # 도구 안에서 validate_hits를 거친다
                    self.assertTrue(hits)
                    self.assertLessEqual(len(hits), 3)

    def test_chart_id_filter_through_tool(self):
        row = next(r for r in self.rows["dispute_cases"] if r["chunk"]["metadata"]["chart_id"])
        self.connect_with(row)
        chart = row["chunk"]["metadata"]["chart_id"]
        query = {"text": "x", "accident_date": "", "filters": {"chart_id": chart}, "top_k": 3}
        hits = asyncio.run(search_dispute_cases(query))
        self.assertEqual(hits[0]["chunk"], row["chunk"])

    def test_tool_refuses_filters_only_common_search_accepts(self):
        row = self.rows["policy_terms"][0]
        self.connect_with(row)
        for filters in (
            {"retrieval_role": "primary_clause"},
            {"coverage": ["own_damage"]},
            {"edition": None},
            {"terms_edition": EDITION},
        ):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                query = {"text": "x", "accident_date": "", "filters": filters, "top_k": 3}
                asyncio.run(search_policy_terms(query))
        request = {
            "corpus": "policy_terms",
            "text": "x",
            "accident_date": "",
            "filters": {"retrieval_role": ["primary_clause", "auxiliary_reference"]},
            "top_k": 3,
        }
        self.assertTrue(asyncio.run(search(request)))  # 공통 search에는 남아 있음

    def test_chart_id_filter_on_real_cases(self):
        row = next(r for r in self.rows["dispute_cases"] if r["chunk"]["metadata"]["chart_id"])
        chart = row["chunk"]["metadata"]["chart_id"]
        hits = self.search_with_vector("dispute_cases", row, {"chart_id": chart})
        self.assertEqual(hits[0]["chunk"], row["chunk"])
        self.assertTrue(all(h["chunk"]["metadata"]["chart_id"] == chart for h in hits))
        self.assertEqual(self.search_with_vector("dispute_cases", row, {"chart_id": "없음"}), [])
        hits = self.search_with_vector("dispute_cases", row, {"chart_id_candidates": chart})
        self.assertEqual(hits[0]["chunk"], row["chunk"])  # 후보 목록 필터도 계속 동작

    def test_default_edition_read_from_corpus_documents(self):
        self.assertEqual(self.corpus.terms_edition(), EDITION)
        self.assertEqual(corpus_terms_edition(), EDITION)

    def test_document_is_the_same_join_as_evidence_store(self):
        for corpus, rows in self.rows.items():
            with self.subTest(corpus=corpus):
                chunk = self.search_with_vector(corpus, rows[0])[0]["chunk"]
                self.assertEqual(tuple(chunk["document"]), DOCUMENT_FIELDS)
                self.assertTrue(chunk["document"]["source_uri"].startswith("corpus-sources/"))
                stored = asyncio.run(self.store.get_chunk(corpus, chunk["chunk_id"]))
                self.assertEqual(stored, chunk)
                self.assertIsNot(stored, chunk)  # 검색 결과를 고쳐도 검증 기준은 그대로

    def test_schedule_3_accident_window_through_tool(self):
        """별표3 과실상계는 사고일로 두 항목이 나뉜다. 사고일 범위 밖 항목은 검색에서 빠진다."""
        rows = [
            r
            for r in self.rows["policy_terms"]
            if r["chunk"]["metadata"].get("schedule") == "별표3"
            and (
                r["chunk"]["metadata"].get("accident_date_from")
                or r["chunk"]["metadata"].get("accident_date_to")
            )
        ]
        self.assertEqual(len(rows), 2)
        old = next(r for r in rows if r["chunk"]["metadata"].get("accident_date_to"))
        new = next(r for r in rows if r["chunk"]["metadata"].get("accident_date_from"))
        for row, accident_date, expected in (
            (new, "2026-10-20", new),
            (old, "2026-10-20", new),  # 옛 항목 벡터로 찾아도 범위 밖이라 빠진다
            (old, "2022-06-01", old),
            (new, "2022-06-01", old),
        ):
            with self.subTest(chunk=row["chunk"]["chunk_id"], accident_date=accident_date):
                self.connect_with(row)
                query = {
                    "text": "과실상계",
                    "accident_date": accident_date,
                    "filters": {"schedule": "별표3"},
                    "top_k": 10,
                }
                hits = asyncio.run(search_policy_terms(query))  # validate_hits까지 거친다
                ids = [h["chunk"]["chunk_id"] for h in hits]
                self.assertIn(expected["chunk"]["chunk_id"], ids)
                other = old if expected is new else new
                self.assertNotIn(other["chunk"]["chunk_id"], ids)
                self.assertEqual({h["chunk"]["metadata"]["schedule"] for h in hits}, {"별표3"})

    def test_primary_clause_filter_on_real_terms(self):
        rows = self.rows["policy_terms"]
        roles = Counter(r["chunk"]["metadata"]["retrieval_role"] for r in rows)
        self.assertEqual(
            roles,
            {"primary_clause": 332, "auxiliary_reference": 177, "primary_payment_standard": 35},
        )
        auxiliary = next(
            r for r in rows if r["chunk"]["metadata"]["retrieval_role"] == "auxiliary_reference"
        )
        self.assertEqual(
            self.search_with_vector("policy_terms", auxiliary)[0]["chunk"], auxiliary["chunk"]
        )
        hits = self.search_with_vector(
            "policy_terms", auxiliary, {"retrieval_role": "primary_clause"}
        )
        self.assertTrue(hits)
        self.assertEqual(
            {h["chunk"]["metadata"]["retrieval_role"] for h in hits}, {"primary_clause"}
        )

    def test_search_policy_terms_uses_default_edition_end_to_end(self):
        row = self.rows["policy_terms"][0]

        class StoredVectorEmbedder:
            """API 없이 저장된 벡터를 돌려준다. 색인과 같은 모델·차원·버전."""

            model = "text-embedding-3-small"
            dimensions = 1536
            version = "text-embedding-3-small:1536:v1"

            async def embed(self, text):
                return row["embedding"]

        configure_retrieval(
            index=self.index,
            embedder=StoredVectorEmbedder(),
            options=VECTOR,
            default_terms_edition=corpus_terms_edition(),
        )
        query = {"text": "x", "accident_date": "", "filters": {}, "top_k": 3}
        hits = asyncio.run(search_policy_terms(query))  # C의 도구 경유, terms_edition 없음
        self.assertEqual(hits[0]["chunk"], row["chunk"])
        self.assertEqual({h["chunk"]["document"]["edition"] for h in hits}, {EDITION})


if __name__ == "__main__":
    unittest.main()
