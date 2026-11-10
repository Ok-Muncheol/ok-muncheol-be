"""D: 운영 연결 함수와 LLM 호출부. 네트워크 없이 가짜 클라이언트만 쓴다. 아래 키는 가짜 값이다."""

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

from app.api import reports as api
from app.calculation.exclusions import flag_judge, llm_judge, openai_judge_llm
from app.calculation.rules import RuleSet
from app.retrieval.common import configure_retrieval, fetch_by_parent
from app.retrieval.embedding import FakeEmbedder, RetrievalNotConfigured
from app.retrieval.hybrid import RetrievalOptions
from app.retrieval.index import InMemoryIndex

FAKE_KEY = "sk-FAKE-for-tests-0123456789"
needs_dotenv = unittest.skipUnless(importlib.util.find_spec("dotenv"), "python-dotenv 없음")


class FakeChat:
    """OpenAI 클라이언트 흉내. 호출 인자를 기록하고 지정한 답이나 예외를 돌려준다."""

    def __init__(
        self, answer='{"result": "pass", "clause_ids": [], "detail": "해당 없음"}', error=None
    ):
        self.answer = answer
        self.error = error
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        message = SimpleNamespace(content=self.answer)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@needs_dotenv
class OpenAIJudgeLLMTest(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        self.dotenv = folder / ".env"
        self.dotenv.write_text(
            f"OPENAI_API_KEY={FAKE_KEY}\nOPENAI_MODEL=fake-model", encoding="utf-8"
        )
        environ = patch.dict(os.environ, {}, clear=True)
        environ.start()
        self.addCleanup(environ.stop)

    def llm(self, client):
        return openai_judge_llm(dotenv_path=self.dotenv, client=client)

    def test_sends_prompt_to_configured_model(self):
        client = FakeChat()
        self.assertIn("pass", self.llm(client)("질문"))
        self.assertEqual(
            client.calls,
            [{"model": "fake-model", "messages": [{"role": "user", "content": "질문"}]}],
        )

    def test_same_prompt_is_not_called_twice(self):
        client = FakeChat()
        llm = self.llm(client)
        self.assertEqual(llm("같은 질문"), llm("같은 질문"))
        self.assertEqual(len(client.calls), 1)
        llm("다른 질문")
        self.assertEqual(len(client.calls), 2)

    def test_failure_has_no_key_and_is_not_cached(self):
        class AuthenticationError(Exception):
            pass

        client = FakeChat(error=AuthenticationError(f"Incorrect API key: {FAKE_KEY}"))
        llm = self.llm(client)
        with self.assertRaises(ValueError) as caught:
            llm("질문")
        exc = caught.exception
        self.assertEqual(str(exc), "LLM 호출 실패: AuthenticationError")
        self.assertIsNone(exc.__context__)
        self.assertNotIn(FAKE_KEY, "".join(traceback.format_exception(exc)))
        client.error = None
        self.assertIn("pass", llm("질문"))

    def test_llm_failure_becomes_unknown_not_crash(self):
        judge = llm_judge(self.llm(FakeChat(error=RuntimeError("down"))))
        check = judge("무면허 운전 면책", [], {"무면허": True})
        self.assertEqual(check["result"], "unknown")
        self.assertIn("담당자 확인", check["detail"])

    def test_empty_answer_becomes_unknown(self):
        judge = llm_judge(self.llm(FakeChat(answer=None)))
        self.assertEqual(judge("무면허 운전 면책", [], {})["result"], "unknown")

    def test_missing_settings_not_configured(self):
        for text in ("OPENAI_MODEL=m", f"OPENAI_API_KEY={FAKE_KEY}"):
            with self.subTest(text=text.split("=")[0]):
                os.environ.clear()
                self.dotenv.write_text(text, encoding="utf-8")
                with self.assertRaises(RetrievalNotConfigured):
                    self.llm(FakeChat())

    def test_missing_openai_package_gives_install_hint(self):
        with patch.dict(sys.modules, {"openai": None}):
            with self.assertRaisesRegex(RetrievalNotConfigured, "pip install"):
                openai_judge_llm(dotenv_path=self.dotenv)


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

    def patches(self, **over):
        values = {
            "load_index": lambda: InMemoryIndex(),
            "handoff_terms_edition": lambda: "판-1",
            "openai_judge_llm": lambda: lambda prompt: "{}",
        }
        values.update(over)
        return patch.multiple(api, **values)

    def test_sets_everything_at_once(self):
        embedder = FakeEmbedder()
        with self.patches(), patch.object(api.OpenAIEmbedder, "from_env", lambda: embedder):
            services = api.configure_with_knowledge_base()
        self.assertIs(services, api.services)
        self.assertIs(services.fetch_clauses, fetch_by_parent)
        self.assertIsNot(services.judge, flag_judge)

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
            raise RetrievalNotConfigured("모델 없음")

        with self.patches(load_index=must_not_load, openai_judge_llm=missing):
            with patch.object(api.OpenAIEmbedder, "from_env", FakeEmbedder):
                with self.assertRaises(RetrievalNotConfigured):
                    api.configure_with_knowledge_base()


if __name__ == "__main__":
    unittest.main()
