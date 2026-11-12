"""C: ③④ 현재 RAG 재정렬(evals/rerank_compare/current_rag_rerank). 모델·네트워크 없음."""

import asyncio
import math
import unittest
from types import SimpleNamespace

from evals.rerank_compare.common import gpt_request
from evals.rerank_compare.current_rag_rerank import CurrentRagReranker
from evals.rerank_compare.gpt_judge import GptJudge
from evals.rerank_compare.harness import Meter, ModelCallLimitExceeded, Question


def passage(parent_id, n, text):
    return SimpleNamespace(parent_id=parent_id, chunk_id=f"{parent_id}#{n}", text=text)


class FakeSearch:
    """후보 청크를 정해 둔다. 부른 mode를 남긴다."""

    def __init__(self, passages, candidate_mode="rrf"):
        self.passages = passages
        self.candidate_mode = candidate_mode
        self.modes = []

    async def candidate_passages(self, question, mode):
        self.modes.append(mode)
        return list(self.passages)


class KeywordScorer:
    """글에 질문 낱말이 있으면 높은 점수(결정적, 동기). 부를 때마다 글 수를 센다."""

    model_name = "fake-reranker"

    def __init__(self, drop_one=False):
        self.batches = []
        self.drop_one = drop_one

    def score(self, question, texts):
        self.batches.append(len(texts))
        words = question.split()
        scores = [sum(w in t for w in words) / len(words) for t in texts]
        return (scores[:-1] if self.drop_one else scores), 10 * len(texts), 0


class FakeOpenAI:
    """chat.completions.create만 흉내 낸다. 글에 '손해'가 있으면 Yes 확률이 높다."""

    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        yes = 0.9 if "손해" in kwargs["messages"][1]["content"] else 0.1
        top = [SimpleNamespace(token="Yes", logprob=math.log(yes))]
        choice = SimpleNamespace(
            logprobs=SimpleNamespace(content=[SimpleNamespace(top_logprobs=top)])
        )
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=1,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        return SimpleNamespace(choices=[choice], usage=usage)


PASSAGES = [
    passage("a", 1, "자기신체사고"),
    passage("b", 1, "대물배상"),
    passage("b", 2, "대인배상 보상하는 손해"),  # b의 최고 청크(MaxP)
    passage("c", 1, "대인배상"),
]
QUESTION = Question("q", "대인배상 보상하는", "", {})


class CurrentRagRerankTest(unittest.TestCase):
    def test_scores_every_chunk_once_and_ranks_by_max_p(self):
        search, scorer, meter = FakeSearch(PASSAGES), KeywordScorer(), Meter()
        ranked = asyncio.run(CurrentRagReranker(search, scorer)(QUESTION, meter))
        self.assertEqual(ranked, ["b", "c", "a"])
        self.assertEqual(search.modes, ["rrf"])
        self.assertEqual(scorer.batches, [4])  # 청크 4개를 한 번에(질문당 한 번)
        self.assertEqual(meter.total_calls, 4)  # 청크 하나당 1회
        self.assertEqual((meter.input_tokens, meter.output_tokens), (40, 0))

    def test_candidate_mode_can_be_changed_and_is_recorded(self):
        search = FakeSearch(PASSAGES)
        retriever = CurrentRagReranker(search, KeywordScorer(), mode="bm25")
        asyncio.run(retriever(QUESTION, Meter()))
        self.assertEqual(search.modes, ["bm25"])
        self.assertEqual(retriever.params["candidates"], "bm25")
        # 기본값은 search의 코퍼스별 후보 방식을 따른다
        search = FakeSearch(PASSAGES, candidate_mode="vector")
        default = CurrentRagReranker(search, KeywordScorer())
        asyncio.run(default(QUESTION, Meter()))
        self.assertEqual(search.modes, ["vector"])
        self.assertEqual(default.params["candidates"], "vector")

    def test_call_limit_stops_before_scoring(self):
        scorer = KeywordScorer()
        with self.assertRaises(ModelCallLimitExceeded):
            asyncio.run(
                CurrentRagReranker(FakeSearch(PASSAGES), scorer)(QUESTION, Meter(max_calls=3))
            )
        self.assertEqual(scorer.batches, [])

    def test_no_candidates_means_no_calls(self):
        meter = Meter()
        ranked = asyncio.run(CurrentRagReranker(FakeSearch([]), KeywordScorer())(QUESTION, meter))
        self.assertEqual((ranked, meter.total_calls), ([], 0))

    def test_score_count_mismatch_fails(self):
        retriever = CurrentRagReranker(FakeSearch(PASSAGES), KeywordScorer(drop_one=True))
        with self.assertRaises(RuntimeError):
            asyncio.run(retriever(QUESTION, Meter()))

    def test_gpt_judge_one_request_per_chunk_and_usage(self):
        client = FakeOpenAI()
        judge = GptJudge(client=client, concurrency=2)
        meter = Meter()
        ranked = asyncio.run(CurrentRagReranker(FakeSearch(PASSAGES), judge)(QUESTION, meter))
        self.assertEqual(ranked, ["b", "a", "c"])  # 나머지 동점(0.1)은 parent_id 순
        self.assertEqual(len(client.requests), 4)
        self.assertIn(gpt_request(QUESTION.text, PASSAGES[0].text), client.requests)
        self.assertEqual((meter.total_calls, meter.input_tokens, meter.output_tokens), (4, 400, 4))
        self.assertEqual(judge.usage()["requests"], 4)


if __name__ == "__main__":
    unittest.main()
