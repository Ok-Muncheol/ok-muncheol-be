"""D: db_import 로더와 로컬 EvidenceStore 어댑터를 tmp에 만든 가짜 행으로 확인한다(#52).

실제 코퍼스가 없어도(CI) 돈다. 외부 API 없음. 실제 코퍼스 확인은 test_d_retrieval_real_data.
"""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.retrieval.common import configure_retrieval, search
from app.retrieval.embedding import RetrievalNotConfigured
from app.retrieval.evidence_store import (
    LocalCorpusEvidenceStore,
    local_store_allowed,
    register_local_evidence_store,
)
from app.retrieval.hybrid import RetrievalOptions
from app.retrieval.keyword import SimpleTokenizer
from app.retrieval.loader import (
    DOCUMENT_FIELDS,
    TABLES,
    CorpusDataError,
    build_index,
    corpus_terms_edition,
    handoff_embedding_spec,
    read_corpus,
)
from app.tools import _evidence
from app.tools.fault_standards import search_fault_standards
from app.tools.policy_terms import search_policy_terms

HASH_T = "a" * 64
HASH_F = "b" * 64
TERMS = "terms-1"
CHART = "fault-1"
BM25 = RetrievalOptions(mode="bm25")


def document(document_id, corpus, content_hash, *, edition, effective_from=""):
    return {
        "corpus": corpus,
        "document_id": document_id,
        "source_uri": f"corpus-sources/{corpus}/{document_id}.pdf",
        "source_name": f"{document_id}.pdf",
        "edition": edition,
        "effective_from": effective_from,
        "effective_to": None,
        "effective_period_status": "unknown" if not effective_from else "assumed",
        "content_hash": content_hash,
        "parser_version": "p1",
        "usage_review": {"status": "permission_required"},
        "reviewed_by": None,
        "storage_sha256": content_hash,
        "storage_bytes": 10,
        "storage_relation": "original",
    }


def chunk(chunk_id, corpus, document_id, parent_id, text, metadata, page=1, vector=(1.0, 0.0)):
    return {
        "corpus": corpus,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "parent_id": parent_id,
        "page_start": page,
        "page_end": page,
        "text": text,
        "metadata": metadata,
        "raw_text": "원문 " + text,
        "display_text": None,
        "embedding_model": "m",
        "embedding_dimensions": 2,
        "embedding_version": "v",
        "embedding_content_hash": "c" * 64,
        "embedding": list(vector),
    }


def record(corpus, kind, record_id, body, *, parent_id=None, is_parent=False, document_id=None):
    return {
        "corpus": corpus,
        "kind": kind,
        "record_id": record_id,
        "parent_id": parent_id,
        "is_parent": is_parent,
        "document_id": document_id,
        "chart_id": body.get("chart_id"),
        "page_start": body.get("page_start"),
        "page_end": body.get("page_end"),
        "body": body,
    }


def parent_body(parent_id, document_id, **extra):
    return {
        "parent_id": parent_id,
        "document": {"document_id": document_id},
        "page_start": 1,
        "page_end": 2,
        **extra,
    }


def fake_rows():
    """약관(조항 1·별표3 사고일 두 항목) + 도표(도표 1·수정요소 1). C 검증 규칙을 만족한다."""
    clause = f"{TERMS}:general_article:23:p1"
    old_s3, new_s3 = f"{TERMS}:payment_standard:별표3:구", f"{TERMS}:payment_standard:별표3:신"
    chart_parent = f"{CHART}:차1-1"
    documents = [
        document(TERMS, "policy_terms", HASH_T, edition="판-1", effective_from="2026-11-11"),
        document(CHART, "fault_standards", HASH_F, edition="2023.6"),
    ]
    s3 = {"schedule": "별표3", "clause_type": "payment_standard"}
    chunks = [
        chunk(
            "t1",
            "policy_terms",
            TERMS,
            clause,
            "제23조 고의 무면허 음주 보상하지 않는 손해",
            {"clause_id": clause, "coverage": None, "article_number": "23"},
        ),
        chunk(
            "t2",
            "policy_terms",
            TERMS,
            old_s3,
            "과실상계 별표3 이전 사고",
            {**s3, "accident_date_to": "2022-12-31"},
            page=2,
        ),
        chunk(
            "t3",
            "policy_terms",
            TERMS,
            new_s3,
            "과실상계 별표3 이후 사고",
            {**s3, "accident_date_from": "2023-01-01"},
            page=2,
        ),
        chunk(
            "f1",
            "fault_standards",
            CHART,
            chart_parent,
            "교차로 녹색 직진 적색 직진",
            {"section": "chart", "chart_id": "차1-1"},
        ),
    ]
    records = [
        record(
            "policy_terms",
            "clause",
            clause,
            parent_body(clause, TERMS, clause_id=clause, coverage=None, side_note_parent_ids=[]),
            parent_id=clause,
            is_parent=True,
            document_id=TERMS,
        ),
        *[
            record(
                "policy_terms",
                "appendix",
                pid,
                parent_body(pid, TERMS, schedule="별표3"),
                parent_id=pid,
                is_parent=True,
                document_id=TERMS,
            )
            for pid in (old_s3, new_s3)
        ],
        record(
            "fault_standards",
            "chart",
            "차1-1",
            parent_body(chart_parent, CHART, chart_id="차1-1", modifier_ids=["m1"]),
            parent_id=chart_parent,
            is_parent=True,
            document_id=CHART,
        ),
        record(
            "fault_standards",
            "modifier",
            "m1",
            {"modifier_id": "m1", "chart_id": "차1-1"},
            parent_id=chart_parent,
        ),
    ]
    pages = [
        {"document_id": doc_id, "page": page, "content_hash": content_hash}
        for doc_id, content_hash in ((TERMS, HASH_T), (CHART, HASH_F))
        for page in (1, 2)
    ]
    return {
        "corpus_documents": documents,
        "corpus_chunks": chunks,
        "corpus_records": records,
        "corpus_source_pages": pages,
    }


class CorpusFilesCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()) / "db_import"
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base.parent)
        self.rows = fake_rows()
        self.write()

    def write(self, **over):
        for name in TABLES:
            rows = over.get(name, self.rows[name])
            text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
            (self.base / f"{name}.jsonl").write_text(text + "\n", encoding="utf-8")


class LoaderTest(CorpusFilesCase):
    def test_chunk_document_is_joined_from_corpus_documents(self):
        corpus = read_corpus(self.base)
        index = build_index(corpus, tokenizer=SimpleTokenizer())
        found = index._rows["t1"]["chunk"]
        self.assertEqual(
            set(found),
            {"chunk_id", "parent_id", "document", "page_start", "page_end", "text", "metadata"},
        )  # raw_text·display_text·임베딩 칼럼은 뺀다
        self.assertEqual(tuple(found["document"]), DOCUMENT_FIELDS)
        self.assertEqual(len(DOCUMENT_FIELDS), 9)
        self.assertIsNone(found["document"]["published_month"])  # 칼럼 없는 이전 파일
        self.assertEqual(
            found["document"]["source_uri"], f"corpus-sources/policy_terms/{TERMS}.pdf"
        )
        self.assertEqual(found["document"]["effective_from"], "2026-11-11")
        self.assertNotIn("source_name", found["document"])

    def test_published_month_is_passed_through_and_checked(self):
        dated = [{**row, "published_month": "2023-06"} for row in self.rows["corpus_documents"]]
        self.write(corpus_documents=dated)
        index = build_index(read_corpus(self.base), tokenizer=SimpleTokenizer())
        self.assertEqual(index._rows["t1"]["chunk"]["document"]["published_month"], "2023-06")
        for bad in ("2023-6", "2023-13", "2023-06-30"):
            with self.subTest(bad=bad):
                rows = [{**row, "published_month": bad} for row in self.rows["corpus_documents"]]
                self.write(corpus_documents=rows)
                with self.assertRaisesRegex(CorpusDataError, "published_month"):
                    build_index(read_corpus(self.base), tokenizer=SimpleTokenizer())

    def test_every_missing_file_is_not_configured(self):
        for name in TABLES:
            with self.subTest(name=name):
                self.write()
                (self.base / f"{name}.jsonl").unlink()
                with self.assertRaisesRegex(RetrievalNotConfigured, name):
                    read_corpus(self.base)
        (self.base / "corpus_documents.jsonl").unlink()
        with self.assertRaises(RetrievalNotConfigured):
            corpus_terms_edition(self.base)

    def test_terms_edition(self):
        self.assertEqual(read_corpus(self.base).terms_edition(), "판-1")
        self.assertEqual(corpus_terms_edition(self.base), "판-1")
        second = document("terms-2", "policy_terms", HASH_T, edition="판-2")
        self.write(corpus_documents=[*self.rows["corpus_documents"], second])
        with self.assertRaisesRegex(CorpusDataError, "corpus_documents.jsonl.*하나로"):
            corpus_terms_edition(self.base)

    def test_corpus_selection_and_keyword_corpora(self):
        corpus = read_corpus(self.base)
        index = build_index(corpus, ["fault_standards"], keyword_corpora=[])
        self.assertEqual(set(index._rows), {"f1"})
        self.assertEqual(index.keyword_corpora, frozenset())
        self.assertEqual(index.spec.dimensions, 2)


class CorpusFailureTest(CorpusFilesCase):
    """코퍼스 파일·데이터 문제는 모두 RetrievalNotConfigured(있는데 못 쓰면 CorpusDataError)."""

    def assert_not_configured(self, pattern, call=None, *, data_error=True):
        """예외가 RetrievalNotConfigured이고 메시지에 파일·문제가 있는지."""
        call = call or (lambda: build_index(read_corpus(self.base), tokenizer=SimpleTokenizer()))
        with self.assertRaisesRegex(RetrievalNotConfigured, pattern) as caught:
            call()
        self.assertEqual(isinstance(caught.exception, CorpusDataError), data_error)
        return caught.exception

    def write_text(self, name, text):
        self.write()
        (self.base / f"{name}.jsonl").write_bytes(text.encode("utf-8"))

    def test_folder_missing(self):
        self.assert_not_configured(
            "적재 파일이 없습니다",
            lambda: read_corpus(self.base / "없는 폴더"),
            data_error=False,
        )

    def test_only_some_files(self):
        for name in TABLES:
            if name != "corpus_chunks":
                (self.base / f"{name}.jsonl").unlink()
        error = self.assert_not_configured("적재 파일이 없습니다", data_error=False)
        self.assertNotIn("corpus_chunks", str(error))  # 있는 파일은 목록에 없다
        self.assertIn("corpus_documents", str(error))

    def test_empty_file_each_table(self):
        for name in TABLES:
            with self.subTest(name=name):
                self.write_text(name, "\n\n")
                self.assert_not_configured(f"{name}.jsonl: 행이 없습니다")

    def test_broken_json_line_keeps_cause_and_line(self):
        good = json.dumps(self.rows["corpus_chunks"][0], ensure_ascii=False)
        self.write_text("corpus_chunks", good + "\n{깨진 줄\n")
        error = self.assert_not_configured("corpus_chunks.jsonl 2번째 줄: JSON이 아닙니다")
        self.assertIsInstance(error.__cause__, json.JSONDecodeError)

    def test_not_utf8(self):
        self.write()
        (self.base / "corpus_records.jsonl").write_bytes(b"\xff\xfe\x00 bad")
        error = self.assert_not_configured("corpus_records.jsonl: UTF-8")
        self.assertIsInstance(error.__cause__, UnicodeDecodeError)

    def test_row_shape_problems(self):
        chunk, doc = self.rows["corpus_chunks"][0], self.rows["corpus_documents"][0]
        record = self.rows["corpus_records"][0]
        without_text = {k: v for k, v in chunk.items() if k != "text"}
        cases = {
            "칼럼 text이 없습니다": {"corpus_chunks": [without_text]},
            "칼럼 embedding의 타입이 틀렸습니다": {"corpus_chunks": [{**chunk, "embedding": None}]},
            "칼럼 page_start의 타입": {"corpus_chunks": [{**chunk, "page_start": "1"}]},
            "벡터에 숫자가 아닌 값": {"corpus_chunks": [{**chunk, "embedding": [1.0, "x"]}]},
            "벡터 길이 1 ≠ 차원 2": {"corpus_chunks": [{**chunk, "embedding": [1.0]}]},
            "metadata.accident_date_to가 날짜가 아닙니다": {
                "corpus_chunks": [
                    {**chunk, "metadata": {**chunk["metadata"], "accident_date_to": "2022-13-01"}}
                ]
            },
            "모르는 코퍼스": {"corpus_documents": [{**doc, "corpus": "other"}]},
            "effective_from가 날짜": {"corpus_documents": [{**doc, "effective_from": "2026.11"}]},
            "행이 JSON 객체가 아닙니다": {"corpus_source_pages": [[1, 2]]},
            "parent_id가 없습니다": {"corpus_records": [{**record, "parent_id": None}]},
        }
        for message, over in cases.items():
            with self.subTest(message=message):
                self.write(**over)
                self.assert_not_configured(message)

    def test_broken_links_and_duplicates(self):
        chunks = self.rows["corpus_chunks"]
        other_spec = {**chunks[3], "embedding_version": "v2"}
        cases = {
            "corpus_documents에 없습니다": {
                "corpus_chunks": [{**chunks[0], "document_id": "none"}]
            },
            "코퍼스가 문서와": {"corpus_chunks": [{**chunks[0], "corpus": "fault_standards"}]},
            "같은 chunk_id": {"corpus_chunks": [chunks[0], {**chunks[3], "chunk_id": "t1"}]},
            "같은 document_id": {"corpus_documents": self.rows["corpus_documents"][:1] * 2},
            "임베딩 모델·차원·버전이 하나가 아닙니다": {"corpus_chunks": [chunks[0], other_spec]},
        }
        for message, over in cases.items():
            with self.subTest(message=message):
                self.write(**over)
                self.assert_not_configured(message)

    def test_unreadable_file_os_error(self):
        real_open = Path.open

        def deny(path, *args, **kwargs):
            if path.name == "corpus_chunks.jsonl":
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", deny):
            error = self.assert_not_configured("corpus_chunks.jsonl: 읽을 수 없습니다")
        self.assertIsInstance(error.__cause__, PermissionError)

    def test_code_bugs_are_not_wrapped(self):
        """파일·데이터가 아닌 문제(TypeError 등)는 감싸지 않는다."""
        with patch("app.retrieval.loader._row_problem", side_effect=TypeError("버그")):
            with self.assertRaisesRegex(TypeError, "버그"):
                read_corpus(self.base)

    def test_terms_edition_problems(self):
        no_terms = [d for d in self.rows["corpus_documents"] if d["corpus"] != "policy_terms"]
        self.write(corpus_documents=no_terms, corpus_chunks=[self.rows["corpus_chunks"][3]])
        self.assert_not_configured("약관 판", lambda: read_corpus(self.base).terms_edition())
        self.assert_not_configured("약관 판", lambda: corpus_terms_edition(self.base))


class HandoffFailureTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "data/private").mkdir(parents=True)
        self.path = self.root / "data/private/handoff.json"

    def test_handoff_problems(self):
        spec = {"model": "m", "dimensions": 2, "version": "v"}
        cases = (
            (None, "파일이 없습니다", False),
            ("{", "handoff.json: JSON으로 읽을 수 없습니다", True),
            ("[]", "최상위가 JSON 객체가 아닙니다", True),
            ("{}", "embedding.model", True),
            (json.dumps({"embedding": {**spec, "dimensions": "2"}}), "embedding.model", True),
        )
        for text, message, data_error in cases:
            with self.subTest(text=text):
                self.path.unlink(missing_ok=True)
                if text is not None:
                    self.path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(RetrievalNotConfigured, message) as caught:
                    handoff_embedding_spec(self.root)
                self.assertEqual(isinstance(caught.exception, CorpusDataError), data_error)
        self.path.write_text(json.dumps({"embedding": spec}), encoding="utf-8")
        self.assertEqual(handoff_embedding_spec(self.root).dimensions, 2)

    def test_handoff_os_error(self):
        self.path.write_text("{}", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=PermissionError(13, "denied")):
            with self.assertRaisesRegex(CorpusDataError, "읽을 수 없습니다") as caught:
                handoff_embedding_spec(self.root)
        self.assertIsInstance(caught.exception.__cause__, PermissionError)


class EvidenceStoreTest(CorpusFilesCase):
    def setUp(self):
        super().setUp()
        self.corpus = read_corpus(self.base)
        self.store = LocalCorpusEvidenceStore(self.corpus)

    def tearDown(self):
        _evidence.configure_evidence_store(None)

    def get(self, method, *args):
        return asyncio.run(getattr(self.store, method)(*args))

    def test_get_chunk_is_the_same_join_as_the_index(self):
        index = build_index(self.corpus, tokenizer=SimpleTokenizer())
        for chunk_id, row in index._rows.items():
            corpus = row["chunk"]["document"]["corpus"]
            stored = self.get("get_chunk", corpus, chunk_id)
            self.assertEqual(stored, row["chunk"])
            self.assertIsNot(stored, row["chunk"])
            self.assertIsNot(stored["metadata"], row["chunk"]["metadata"])
        self.assertIsNone(self.get("get_chunk", "fault_standards", "t1"))  # 코퍼스까지 키

    def test_records_pages_and_missing_keys(self):
        clause = f"{TERMS}:general_article:23:p1"
        self.assertEqual(self.get("get_parent", "policy_terms", clause)["clause_id"], clause)
        self.assertIsNone(self.get("get_parent", "fault_standards", "m1"))  # 부모가 아닌 행
        self.assertEqual(
            self.get("get_related", "fault_standards", "modifier", "m1")["chart_id"], "차1-1"
        )
        self.assertIsNone(self.get("get_related", "fault_standards", "modifier", "m2"))
        self.assertEqual(self.get("get_source_page", TERMS, 2), {"content_hash": HASH_T})
        self.assertIsNone(self.get("get_source_page", TERMS, 3))

    def test_local_only_rule(self):
        cases = (
            ({"APP_ENV": "local"}, True),
            ({}, False),  # APP_ENV 기본값은 production
            ({"APP_ENV": "production"}, False),
            ({"APP_ENV": "local", "VERCEL": "1"}, False),
            ({"APP_ENV": "local", "VERCEL_ENV": "preview"}, False),
        )
        for env, allowed in cases:
            with self.subTest(env=env):
                self.assertIs(local_store_allowed(env), allowed)
                _evidence.configure_evidence_store(None)
                self.assertIs(register_local_evidence_store(self.corpus, env), allowed)
                if allowed:
                    self.assertIsInstance(_evidence.evidence_store(), LocalCorpusEvidenceStore)
                else:
                    with self.assertRaisesRegex(RuntimeError, "not configured"):
                        _evidence.evidence_store()


class ToolsWithLocalStoreTest(CorpusFilesCase):
    """C 도구(검색 → validate_hits)가 로컬 어댑터로 끝까지 통과하는지. BM25라 임베더가 없다."""

    def setUp(self):
        super().setUp()
        corpus = read_corpus(self.base)
        index = build_index(corpus, tokenizer=SimpleTokenizer())
        configure_retrieval(index=index, options=BM25, default_terms_edition=corpus.terms_edition())
        self.corpus = corpus

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )
        _evidence.configure_evidence_store(None)

    def query(self, text, accident_date="", filters=None):
        return {"text": text, "accident_date": accident_date, "filters": filters or {}, "top_k": 5}

    def ids(self, hits):
        return [h["chunk"]["chunk_id"] for h in hits]

    def test_not_registered_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            asyncio.run(search_policy_terms(self.query("제23조 고의")))

    def test_registered_store_validates_tool_results(self):
        register_local_evidence_store(self.corpus, {"APP_ENV": "local"})
        self.assertEqual(
            self.ids(asyncio.run(search_policy_terms(self.query("제23조 고의")))), ["t1"]
        )
        hits = asyncio.run(search_fault_standards(self.query("녹색 직진")))
        self.assertEqual(self.ids(hits), ["f1"])

    def test_tampered_document_rejected(self):
        register_local_evidence_store(self.corpus, {"APP_ENV": "local"})
        hits = asyncio.run(
            search({"corpus": "policy_terms", **self.query("제23조 고의"), "top_k": 1})
        )
        hits[0]["chunk"] = {
            **hits[0]["chunk"],
            "document": {**hits[0]["chunk"]["document"], "source_uri": "다른 경로"},
        }
        request = {"corpus": "policy_terms", **self.query("제23조 고의"), "top_k": 1}
        with self.assertRaisesRegex(ValueError, "document differs"):
            asyncio.run(_evidence.validate_hits(request, hits))

    def test_schedule_filter_and_accident_window(self):
        register_local_evidence_store(self.corpus, {"APP_ENV": "local"})
        schedule = {"schedule": "별표3"}
        cases = (
            ("", ["t2", "t3"]),  # 사고일이 없으면 범위로 거르지 않는다
            ("2022-12-31", ["t2"]),  # 양끝 포함
            ("2023-01-01", ["t3"]),
            ("2026-10-20", ["t3"]),
        )
        for accident_date, expected in cases:
            with self.subTest(accident_date=accident_date):
                hits = asyncio.run(
                    search_policy_terms(self.query("과실상계 별표3", accident_date, schedule))
                )
                self.assertEqual(sorted(self.ids(hits)), expected)
        # 범위 값이 없는 청크(제23조)는 사고일과 상관없이 통과
        hits = asyncio.run(search_policy_terms(self.query("제23조 고의", "2022-01-01")))
        self.assertEqual(self.ids(hits), ["t1"])
        # 공통 search에서도 범위가 걸린다(C 도구의 사전 거르기와 상관없이)
        request = {"corpus": "policy_terms", **self.query("과실상계", "2022-06-01", schedule)}
        self.assertEqual(self.ids(asyncio.run(search(request))), ["t2"])
        self.assertEqual(
            asyncio.run(search_policy_terms(self.query("과실상계", "", {"schedule": "별표9"}))),
            [],
        )


if __name__ == "__main__":
    unittest.main()
