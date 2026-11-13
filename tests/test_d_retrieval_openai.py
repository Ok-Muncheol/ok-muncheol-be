"""D: OpenAIEmbedder. 네트워크 없이 가짜 클라이언트만 쓴다(CI에서 API 호출 금지).

실제 .env를 읽지 않도록 임시 파일 경로를 넘긴다. 아래 키는 가짜 값이다.
"""

import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import llm as llm_policy
from app.retrieval.common import EmbeddingMismatch, configure_retrieval, search
from app.retrieval.embedding import (
    QUERY_TIMEOUT_SECONDS,
    EmbeddingRequestFailed,
    EmbeddingSpec,
    OpenAIEmbedder,
    RetrievalNotConfigured,
    _openai_client_factory,
)
from app.retrieval.hybrid import RetrievalOptions
from app.retrieval.index import InMemoryIndex
from app.retrieval.loader import ROOT, handoff_embedding_spec
from tests.fake_openai import status_error, timeout_error

FAKE_KEY = "sk-FAKE-for-tests-0123456789"
SPEC = EmbeddingSpec("text-embedding-3-small", 3, "text-embedding-3-small:3:v1")
needs_dotenv = unittest.skipUnless(importlib.util.find_spec("dotenv"), "python-dotenv 없음")


class FakeApi:
    """AsyncOpenAI 흉내. 호출 기록을 남기고, 지정한 벡터나 예외를 돌려준다.

    errors는 앞의 호출부터 차례로 던질 예외다(다 쓰면 vector·error대로 돌려준다)."""

    def __init__(self, vector=None, error=None, rows=1, errors=()):
        self.vector = [1.0, 0.0, 0.0] if vector is None else vector
        self.error = error
        self.errors = list(errors)
        self.rows = rows
        self.calls = []
        self.embeddings = self

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        if self.error:
            raise self.error
        return SimpleNamespace(data=[SimpleNamespace(embedding=self.vector)] * self.rows)


def embedder(api, **kw):
    return OpenAIEmbedder(
        model=SPEC.model,
        dimensions=SPEC.dimensions,
        version=SPEC.version,
        client_factory=api,
        **kw,
    )


class EmbedTest(unittest.TestCase):
    def test_request_uses_configured_model_and_dimensions(self):
        api = FakeApi()
        self.assertEqual(asyncio.run(embedder(api).embed("질문")), [1.0, 0.0, 0.0])
        self.assertEqual(
            api.calls,
            [
                {
                    "model": SPEC.model,
                    "dimensions": 3,
                    "input": ["질문"],
                    "encoding_format": "float",
                }
            ],
        )

    def test_same_question_is_cached(self):
        api = FakeApi()
        e = embedder(api)
        first = asyncio.run(e.embed("같은 질문"))
        first.append(9.9)  # 돌려준 목록을 고쳐도 캐시는 그대로
        self.assertEqual(asyncio.run(e.embed("같은 질문")), [1.0, 0.0, 0.0])
        self.assertEqual(len(api.calls), 1)
        asyncio.run(e.embed("다른 질문"))
        self.assertEqual(len(api.calls), 2)

    def test_cache_drops_oldest_at_limit(self):
        api = FakeApi()
        e = embedder(api, cache_size=2)
        for text in ("a", "b", "a", "c"):  # c가 들어오면 가장 오래 안 쓴 b가 빠짐
            asyncio.run(e.embed(text))
        self.assertEqual(len(api.calls), 3)
        asyncio.run(e.embed("a"))
        self.assertEqual(len(api.calls), 3)
        asyncio.run(e.embed("b"))
        self.assertEqual(len(api.calls), 4)

    def test_bad_vectors_rejected_and_not_cached(self):
        for vector in ([1.0], [0.0, 0.0, 0.0], [1.0, float("nan"), 0.0]):
            with self.subTest(vector=vector):
                api = FakeApi(vector=vector)
                e = embedder(api)
                with self.assertRaises(ValueError):
                    asyncio.run(e.embed("질문"))
                with self.assertRaises(ValueError):
                    asyncio.run(e.embed("질문"))
                self.assertEqual(len(api.calls), 2)

    def test_wrong_row_count_rejected(self):
        with self.assertRaises(EmbeddingRequestFailed):
            asyncio.run(embedder(FakeApi(rows=2)).embed("질문"))


class RetryPolicyTest(unittest.TestCase):
    """호출 1회 QUERY_TIMEOUT_SECONDS, SDK 재시도 끔, 일시 오류만 1회 더(app/llm.py 정책)."""

    def setUp(self):
        backoff = patch.object(llm_policy, "BACKOFF_SECONDS", 0)
        backoff.start()
        self.addCleanup(backoff.stop)

    def test_default_client_cuts_one_call_and_leaves_retries_to_the_policy(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY}):
            client = _openai_client_factory()()
        try:
            # 예전 AsyncOpenAI(timeout=90, max_retries=4)가 아니라 호출 1회 5초·SDK 재시도 없음
            self.assertEqual((client.timeout, client.max_retries), (QUERY_TIMEOUT_SECONDS, 0))
            self.assertEqual(QUERY_TIMEOUT_SECONDS, 5)
        finally:
            asyncio.run(client.close())

    def test_transient_error_is_retried_once(self):
        api = FakeApi(errors=[timeout_error()])
        self.assertEqual(asyncio.run(embedder(api).embed("질문")), [1.0, 0.0, 0.0])
        self.assertEqual(len(api.calls), 2)

    def test_gives_up_after_the_retry(self):
        api = FakeApi(errors=[timeout_error(), status_error(503)])
        with self.assertRaises(EmbeddingRequestFailed) as caught:
            asyncio.run(embedder(api).embed("질문"))
        self.assertEqual(str(caught.exception), "임베딩 요청 실패: APIStatusError")
        self.assertEqual(len(api.calls), 2)

    def test_client_error_is_not_retried(self):
        api = FakeApi(errors=[status_error(429)])
        with self.assertRaises(EmbeddingRequestFailed):
            asyncio.run(embedder(api).embed("질문"))
        self.assertEqual(len(api.calls), 1)


class SecretTest(unittest.TestCase):
    def test_api_error_never_carries_key(self):
        class AuthenticationError(Exception):
            pass

        api = FakeApi(error=AuthenticationError(f"Incorrect API key provided: {FAKE_KEY}"))
        with self.assertRaises(EmbeddingRequestFailed) as caught:
            asyncio.run(embedder(api).embed("질문"))
        exc = caught.exception
        self.assertEqual(str(exc), "임베딩 요청 실패: AuthenticationError")
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)  # 원본 예외를 연결하지 않음
        rendered = "".join(traceback.format_exception(exc))
        self.assertNotIn(FAKE_KEY, rendered)

    def test_repr_and_attributes_have_no_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY}):
            e = embedder(FakeApi())
            self.assertNotIn(FAKE_KEY, repr(e))
            self.assertNotIn(FAKE_KEY, str(vars(e)))


class FromEnvBase(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        self.dotenv = folder / ".env"
        environ = patch.dict(os.environ, {}, clear=True)
        environ.start()
        self.addCleanup(environ.stop)

    def write(self, key=FAKE_KEY, model=SPEC.model, dimensions="3"):
        lines = [f"OPENAI_API_KEY={key}"] if key else []
        lines += [f"OPENAI_EMBEDDING_MODEL={model}"] if model else []
        lines += [f"OPENAI_EMBEDDING_DIMENSIONS={dimensions}"] if dimensions else []
        self.dotenv.write_text("\n".join(lines), encoding="utf-8")

    def from_env(self, **kw):
        kw.setdefault("client_factory", FakeApi())
        return OpenAIEmbedder.from_env(expected=SPEC, dotenv_path=self.dotenv, **kw)


@needs_dotenv
class FromEnvTest(FromEnvBase):
    def test_reads_env_file(self):
        self.write()
        e = self.from_env()
        self.assertEqual((e.model, e.dimensions, e.version), (SPEC.model, 3, SPEC.version))
        self.assertNotIn(FAKE_KEY, repr(e))

    def test_existing_environment_wins_over_file(self):
        self.write(model="other-model")
        os.environ["OPENAI_EMBEDDING_MODEL"] = SPEC.model  # override=False
        self.assertEqual(self.from_env().model, SPEC.model)

    def test_mismatch_with_handoff_rejected(self):
        for kw in ({"model": "text-embedding-3-large"}, {"dimensions": "1536"}):
            with self.subTest(kw=kw):
                os.environ.clear()
                self.write(**kw)
                with self.assertRaises(EmbeddingMismatch):
                    self.from_env()

    def test_non_integer_dimensions_rejected(self):
        self.write(dimensions="many")
        with self.assertRaises(EmbeddingMismatch):
            self.from_env()

    def test_missing_values_not_configured(self):
        for kw in ({"key": ""}, {"model": ""}, {"dimensions": ""}):
            with self.subTest(kw=kw):
                os.environ.clear()
                self.write(**kw)
                with self.assertRaises(RetrievalNotConfigured):
                    self.from_env()

    def test_not_configured_is_not_implemented_error(self):
        # 면책 검색 쪽 담당자 입력 대체 경로가 NotImplementedError를 기준으로 동작한다
        self.write(key="")
        with self.assertRaises(NotImplementedError):
            self.from_env()

    def test_missing_openai_package_gives_install_hint(self):
        self.write()
        with patch.dict(sys.modules, {"openai": None}):
            with self.assertRaisesRegex(RetrievalNotConfigured, "pip install"):
                OpenAIEmbedder.from_env(expected=SPEC, dotenv_path=self.dotenv)


class MissingDotenvTest(FromEnvBase):
    def test_missing_dotenv_gives_install_hint(self):
        with patch.dict(sys.modules, {"dotenv": None}):
            with self.assertRaisesRegex(RetrievalNotConfigured, "pip install"):
                self.from_env()


class SearchTest(unittest.TestCase):
    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )

    def index(self, spec=SPEC):
        document = {
            "document_id": "d1",
            "corpus": "dispute_cases",
            "source_uri": "synthetic://d1",
            "edition": "e",
            "effective_from": "",
            "effective_to": None,
            "content_hash": "h",
            "parser_version": "p",
        }
        chunk = {
            "chunk_id": "c1",
            "parent_id": "p1",
            "document": document,
            "page_start": 1,
            "page_end": 1,
            "text": "합성",
            "metadata": {},
        }
        return InMemoryIndex(
            [
                {
                    "chunk": chunk,
                    "embedding": [1.0, 0.0, 0.0],
                    "embedding_model": spec.model,
                    "embedding_dimensions": spec.dimensions,
                    "embedding_version": spec.version,
                }
            ]
        )

    def request(self):
        return {
            "corpus": "dispute_cases",
            "text": "질문",
            "accident_date": "",
            "filters": {},
            "top_k": 3,
        }

    def test_search_uses_embedder_and_caches_question(self):
        api = FakeApi()
        with self.assertLogs("app.retrieval.index", "WARNING"):
            configure_retrieval(index=self.index(), embedder=embedder(api))
        for _ in range(2):
            hits = asyncio.run(search(self.request()))
            self.assertEqual(hits[0]["chunk"]["parent_id"], "p1")
        self.assertEqual(len(api.calls), 1)

    def test_index_with_other_model_rejected_before_api_call(self):
        api = FakeApi()
        other = EmbeddingSpec("other", 3, "other:3:v1")
        with self.assertLogs("app.retrieval.index", "WARNING"):
            configure_retrieval(index=self.index(other), embedder=embedder(api))
        with self.assertRaises(EmbeddingMismatch):
            asyncio.run(search(self.request()))
        self.assertEqual(api.calls, [])


@unittest.skipUnless((ROOT / "data/private/handoff.json").exists(), "C 산출물 없음")
class HandoffSpecTest(unittest.TestCase):
    def test_reads_embedding_from_handoff(self):
        self.assertEqual(
            handoff_embedding_spec(),
            EmbeddingSpec("text-embedding-3-small", 1536, "text-embedding-3-small:1536:v1"),
        )


if __name__ == "__main__":
    unittest.main()
