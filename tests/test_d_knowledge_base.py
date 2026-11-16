"""D: 운영 연결 함수(configure_with_knowledge_base). 네트워크 없이 가짜 값만 쓴다.

면책 판정은 LLM 없이 담당자 입력으로 한다(flag_judge, #204). 예전 면책 LLM 호출부 테스트는 그 코드와
함께 뺐다.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.api import reports as api
from app.calculation.exclusions import flag_judge
from app.calculation.rules import RuleSet
from app.retrieval import common as retrieval_common
from app.retrieval import embedding as retrieval_embedding
from app.retrieval.common import configure_retrieval, fetch_by_parent
from app.retrieval.embedding import (
    EmbeddingMismatch,
    EmbeddingSpec,
    FakeEmbedder,
    RetrievalNotConfigured,
)
from app.retrieval.hybrid import RetrievalOptions
from app.retrieval.index import InMemoryIndex
from app.retrieval.keyword import SimpleTokenizer
from app.retrieval.loader import (
    TABLES,
    CorpusDataError,
    CorpusRows,
    build_index,
    handoff_embedding_spec,
    read_corpus,
)
from app.tools import _evidence

FAKE_KEY = "sk-FAKE-for-tests-0123456789"
FAKE_SPEC = EmbeddingSpec(FakeEmbedder.model, FakeEmbedder.dimensions, FakeEmbedder.version)
needs_dotenv = unittest.skipUnless(importlib.util.find_spec("dotenv"), "python-dotenv 없음")


class ConfigureWithKnowledgeBaseTest(unittest.TestCase):
    def setUp(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            judge=flag_judge,
        )

    def tearDown(self):
        self.setUp()
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )
        _evidence.configure_evidence_store(None)

    def patches(self, **over):
        corpus = CorpusRows(
            documents={"d1": {"corpus": "policy_terms", "edition": "판-1"}},
            chunks=[],
            records=[],
            pages=[],
        )
        values = {
            "read_corpus": lambda: corpus,
            # FakeEmbedder와 같은 임베딩 값의 색인(연결 함수가 둘을 비교한다)
            "build_index": lambda corpus: SimpleNamespace(spec=FAKE_SPEC),
        }
        values.update(over)
        return patch.multiple(api, **values)

    def connect(self, env):
        with self.patches(), patch.dict(os.environ, env, clear=True):
            with patch.object(api.OpenAIEmbedder, "from_env", FakeEmbedder):
                return api.configure_with_knowledge_base()

    def test_local_registers_evidence_store(self):
        self.connect({"APP_ENV": "local"})
        self.assertIsNone(asyncio.run(_evidence.evidence_store().get_chunk("policy_terms", "c")))

    def test_production_and_vercel_do_not_register_evidence_store(self):
        for env in ({}, {"APP_ENV": "production"}, {"APP_ENV": "local", "VERCEL": "1"}):
            with self.subTest(env=env):
                self.assertIs(self.connect(env).fetch_clauses, fetch_by_parent)
                with self.assertRaisesRegex(RuntimeError, "not configured"):
                    _evidence.evidence_store()

    def test_missing_corpus_files_change_nothing(self):
        def missing():
            raise RetrievalNotConfigured("D: 코퍼스 적재 파일이 없습니다")

        with self.patches(read_corpus=missing), patch.dict(os.environ, {"APP_ENV": "local"}):
            with patch.object(api.OpenAIEmbedder, "from_env", FakeEmbedder):
                with self.assertRaises(RetrievalNotConfigured):
                    api.configure_with_knowledge_base()
        self.assertIsNone(api.services.fetch_clauses)
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            _evidence.evidence_store()

    def test_sets_everything_at_once(self):
        embedder = FakeEmbedder()
        with self.patches(), patch.object(api.OpenAIEmbedder, "from_env", lambda: embedder):
            services = api.configure_with_knowledge_base()
        self.assertIs(services, api.services)
        self.assertIs(services.fetch_clauses, fetch_by_parent)
        self.assertIs(services.judge, flag_judge)  # #204: 면책은 LLM 없이 담당자 입력으로

    def test_failure_leaves_services_unchanged(self):
        def missing():
            raise RetrievalNotConfigured("키 없음")

        with self.patches(), patch.object(api.OpenAIEmbedder, "from_env", missing):
            with self.assertRaises(RetrievalNotConfigured):
                api.configure_with_knowledge_base()
        self.assertIsNone(api.services.fetch_clauses)
        self.assertIs(api.services.judge, flag_judge)

    def test_settings_checked_before_loading_index(self):
        def must_not_load():
            raise AssertionError("설정이 없으면 색인을 읽지 않는다")

        def missing():
            raise RetrievalNotConfigured("임베딩 모델 없음")

        with self.patches(read_corpus=must_not_load):
            with patch.object(api.OpenAIEmbedder, "from_env", missing):
                with self.assertRaises(RetrievalNotConfigured):
                    api.configure_with_knowledge_base()


def SpecEmbedder():  # noqa: N802 -- OpenAIEmbedder.from_env 자리에 넣는 생성 함수
    """tests/test_d_corpus_loader.py 가짜 행과 같은 임베딩 값(m, 2, v)."""
    return FakeEmbedder(model="m", dimensions=2, version="v")


class KnowledgeBaseFailureLeavesStateTest(unittest.TestCase):
    """B가 main.py에서 RetrievalNotConfigured만 잡고 넘어가도 되는지 본다.

    코퍼스 문제는 모두 그 예외이고, 실패하면 검색 설정·EvidenceStore·보고서 서비스가 이전
    그대로여야 한다(실제 로더, tmp 가짜 행).
    """

    def setUp(self):
        from tests.test_d_corpus_loader import fake_rows

        self.base = Path(tempfile.mkdtemp()) / "db_import"
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base.parent)
        self.rows = fake_rows()
        self.write()
        # 이전 상태: 다른 색인·임베더·기본 판·어댑터가 이미 연결돼 있다
        self.previous_index = InMemoryIndex()
        self.previous_embedder = FakeEmbedder()
        self.previous_factory = lambda: "이전 어댑터"
        configure_retrieval(
            index=self.previous_index,
            embedder=self.previous_embedder,
            default_terms_edition="이전 판",
        )
        _evidence.configure_evidence_store(self.previous_factory)
        api.configure(fetch_clauses=None, judge=flag_judge)

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )
        _evidence.configure_evidence_store(None)
        api.configure(fetch_clauses=None, judge=flag_judge)

    def write(self, **over):
        for name in TABLES:
            rows = over.get(name, self.rows[name])
            text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
            (self.base / f"{name}.jsonl").write_text(text + "\n", encoding="utf-8")

    def connect(self, *, embedder=SpecEmbedder, **over):
        values = {
            "read_corpus": lambda: read_corpus(self.base),
            "build_index": lambda corpus: build_index(corpus, tokenizer=SimpleTokenizer()),
        }
        values.update(over)
        with patch.multiple(api, **values), patch.dict(os.environ, {"APP_ENV": "local"}):
            with patch.object(api.OpenAIEmbedder, "from_env", embedder):
                return api.configure_with_knowledge_base()

    def assert_unchanged(self):
        self.assertIs(retrieval_common._index, self.previous_index)
        self.assertEqual(retrieval_common._default_terms_edition, "이전 판")
        self.assertIs(retrieval_embedding.get_embedder(), self.previous_embedder)
        self.assertIs(_evidence._store_factory, self.previous_factory)
        self.assertIsNone(api.services.fetch_clauses)
        self.assertIs(api.services.judge, flag_judge)

    def assert_fails_unchanged(self, pattern, **connect_kw):
        with self.assertRaisesRegex(RetrievalNotConfigured, pattern):
            self.connect(**connect_kw)
        self.assert_unchanged()

    def test_success_replaces_everything(self):
        services = self.connect()
        self.assertIs(services.fetch_clauses, fetch_by_parent)
        self.assertIsNot(retrieval_common._index, self.previous_index)
        self.assertEqual(retrieval_common._default_terms_edition, "판-1")
        self.assertIsNot(_evidence._store_factory, self.previous_factory)

    def test_corpus_file_failures_change_nothing(self):
        chunks = self.rows["corpus_chunks"]
        no_terms = [d for d in self.rows["corpus_documents"] if d["corpus"] != "policy_terms"]
        cases = {
            "적재 파일이 없습니다": lambda: (self.base / "corpus_records.jsonl").unlink(),
            "행이 없습니다": lambda: self.write(corpus_source_pages=[]),
            "JSON이 아닙니다": lambda: (self.base / "corpus_chunks.jsonl").write_text(
                "{깨짐\n", encoding="utf-8"
            ),
            "corpus_documents에 없습니다": lambda: self.write(
                corpus_chunks=[{**chunks[0], "document_id": "none"}]
            ),
            "같은 chunk_id": lambda: self.write(corpus_chunks=[chunks[0], chunks[0]]),
            "벡터 길이": lambda: self.write(corpus_chunks=[{**chunks[0], "embedding": [1.0]}]),
            # 색인까지 만든 뒤 실패하는 경우(약관 판)
            "약관 판": lambda: self.write(corpus_documents=no_terms, corpus_chunks=[chunks[3]]),
        }
        for pattern, damage in cases.items():
            with self.subTest(pattern=pattern):
                self.write()
                damage()
                self.assert_fails_unchanged(pattern)

    def test_os_error_changes_nothing(self):
        real_open = Path.open

        def deny(path, *args, **kwargs):
            if path.name == "corpus_records.jsonl":
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", deny):
            self.assert_fails_unchanged("corpus_records.jsonl: 읽을 수 없습니다")

    def test_corpus_embedding_differs_from_query_embedder(self):
        with self.assertRaisesRegex(CorpusDataError, "질의 임베더"):
            self.connect(embedder=FakeEmbedder)  # 64차원 대 코퍼스 2차원
        self.assert_unchanged()

    def test_broken_handoff_changes_nothing(self):
        root = self.base.parent / "repo"
        (root / "data/private").mkdir(parents=True)
        (root / "data/private/handoff.json").write_text("{", encoding="utf-8")

        def from_env():  # OpenAIEmbedder.from_env가 맨 먼저 하는 handoff 확인을 흉내 낸다
            handoff_embedding_spec(root)

        self.assert_fails_unchanged("handoff.json", embedder=from_env)

    @needs_dotenv
    def test_embedding_settings_mismatch_is_not_configured(self):
        """.env 임베딩 모델·차원이 handoff와 다르거나 숫자가 아니면 EmbeddingSettingsInvalid.

        실제 OpenAIEmbedder.from_env를 부른다(키는 가짜, 네트워크 없음). 메시지에 키가 없어야 한다.
        """
        from_env = api.OpenAIEmbedder.from_env
        spec = handoff_embedding_spec()
        cases = (
            {"OPENAI_EMBEDDING_MODEL": "other-model", "OPENAI_EMBEDDING_DIMENSIONS": "1536"},
            {"OPENAI_EMBEDDING_MODEL": spec.model, "OPENAI_EMBEDDING_DIMENSIONS": "3"},
            {"OPENAI_EMBEDDING_MODEL": spec.model, "OPENAI_EMBEDDING_DIMENSIONS": "abc"},
        )
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY, **env}):
                with self.assertRaises(api.EmbeddingSettingsInvalid) as caught:
                    self.connect(embedder=from_env)
                error = caught.exception
                self.assertIsInstance(error, RetrievalNotConfigured)
                self.assertIsInstance(error.__cause__, EmbeddingMismatch)
                self.assertIn("OPENAI_EMBEDDING", str(error))
                self.assertNotIn(FAKE_KEY, str(error))
                self.assertNotIn(FAKE_KEY, repr(error.__cause__))
                self.assert_unchanged()

    def test_embedding_mismatch_from_settings_changes_nothing(self):
        def mismatch():
            raise EmbeddingMismatch("OPENAI_EMBEDDING_*: 임베딩 불일치")

        with self.assertRaises(api.EmbeddingSettingsInvalid):
            self.connect(embedder=mismatch)
        self.assert_unchanged()

    def test_failure_while_making_adapter_changes_nothing(self):
        def broken(corpus):
            raise CorpusDataError("어댑터를 만들 수 없음")

        self.assert_fails_unchanged("어댑터", local_evidence_store=broken)


if __name__ == "__main__":
    unittest.main()
