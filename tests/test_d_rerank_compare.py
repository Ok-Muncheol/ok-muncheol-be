"""D: 재정렬 비교 평가 틀(evals/rerank_compare). 네트워크·모델 없음(토크나이저는 가짜)."""

import asyncio
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evals.rerank_compare import common
from evals.rerank_compare.baselines import CachedEmbedder, PolicySearch, QueryVectorMissing
from evals.rerank_compare.common import (
    ANSWER_INSTRUCTION,
    RELEVANCE_INSTRUCTION,
    SYSTEM_PROMPT,
    PolicyParents,
    gpt_request,
    max_p,
    passage_text,
    qwen_prompt,
    relevance_user_text,
    truncate_tokens,
    yes_probability,
)
from evals.rerank_compare.harness import (
    Example,
    Meter,
    ModelCallLimitExceeded,
    Question,
    format_table,
    load_examples,
    rank_scores,
    run_method,
)
from tests.evidence_d import has_corpus


class CharEncoder:
    """글자 하나 = 토큰 하나."""

    def encode(self, text):
        return [ord(ch) for ch in text]

    def decode(self, tokens):
        return "".join(chr(t) for t in tokens)


ENC = CharEncoder()


def question(example_id="q1", accident_date=""):
    return Question(example_id, "질문", accident_date, {})


def example(example_id, subset, expected):
    return Example(question(example_id), subset, tuple(expected))


class PassageTest(unittest.TestCase):
    def test_truncate_keeps_short_text(self):
        self.assertEqual(truncate_tokens("가나다", 5, encoder=ENC), "가나다")
        self.assertEqual(truncate_tokens("가나다라마바", 4, encoder=ENC), "가나다라")

    def test_truncate_strips_broken_character(self):
        class Broken(CharEncoder):
            def decode(self, tokens):
                return super().decode(tokens) + "�"

        self.assertEqual(truncate_tokens("가나다", 2, encoder=Broken()), "가나")

    def test_passage_is_title_plus_body_and_only_body_is_cut(self):
        limit = common.MAX_PASSAGE_TOKENS
        body = "제1조(정의)\n제1조(정의)\n" + "가" * 600
        text = passage_text("제1조(정의)", body, encoder=ENC)
        self.assertEqual(text, "제1조(정의)\n" + "가" * limit)

    def test_coverage_heading_becomes_title(self):
        body = "대인배상Ⅰ | 제3조(보상하는 손해)\n제3조(보상하는 손해)\n본문"
        text = passage_text("제3조(보상하는 손해)", body, encoder=ENC)
        self.assertEqual(text, "대인배상Ⅰ | 제3조(보상하는 손해)\n본문")

    def test_body_without_title_line(self):
        self.assertEqual(passage_text("제목", "본문", encoder=ENC), "제목\n본문")
        self.assertEqual(passage_text("", "본문", encoder=ENC), "본문")


class ModelInputTest(unittest.TestCase):
    def test_both_models_get_the_same_user_text(self):
        user = relevance_user_text("Q", "D")
        self.assertIn(RELEVANCE_INSTRUCTION, user)
        self.assertIn(user, qwen_prompt("Q", "D"))
        self.assertIn(SYSTEM_PROMPT, qwen_prompt("Q", "D"))
        request = gpt_request("Q", "D")
        self.assertEqual(request["model"], "gpt-4o-mini")
        self.assertEqual(request["temperature"], 0)
        self.assertTrue(request["logprobs"])
        self.assertEqual(request["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        self.assertEqual(request["messages"][1]["content"], f"{user}\n{ANSWER_INSTRUCTION}")

    def test_yes_probability_accepts_three_shapes_and_merges_case(self):
        items = [
            {"token": "Yes", "logprob": math.log(0.5)},
            ("yes", math.log(0.1)),
            SimpleNamespace(token=" YES", logprob=math.log(0.05)),
            {"token": "No", "logprob": math.log(0.3)},
        ]
        self.assertAlmostEqual(yes_probability(items), 0.65)
        self.assertEqual(yes_probability([("No", 0.0)]), 0.0)

    def test_max_p_takes_best_chunk_and_breaks_ties_by_id(self):
        ranked = max_p([("b", 0.2), ("a", 0.5), ("b", 0.9), ("c", 0.5)])
        self.assertEqual(ranked, [("b", 0.9), ("a", 0.5), ("c", 0.5)])


class ScoreTest(unittest.TestCase):
    def test_rank_scores(self):
        scores = rank_scores(["x", "p1", "y"], ["p1"])
        self.assertEqual(scores["first_correct_rank"], 2)
        self.assertEqual((scores["top1"], scores["r3"], scores["mrr10"]), (0.0, 1.0, 0.5))
        miss = rank_scores([f"x{i}" for i in range(10)], ["p1"])
        self.assertEqual((miss["first_correct_rank"], miss["r10"], miss["mrr10"]), (None, 0, 0))

    def test_recall_with_two_answers(self):
        scores = rank_scores(["a", "x", "y", "z", "b"], ["a", "b"])
        self.assertEqual((scores["top1"], scores["r3"], scores["r5"]), (1.0, 0.5, 1.0))

    def test_rejects_question_without_answer(self):
        with self.assertRaises(ValueError):
            rank_scores(["a"], [])


class RunMethodTest(unittest.TestCase):
    EXAMPLES = [
        example("q1", "policy80", ["p1"]),
        example("q2", "policy80", ["p2"]),
        example("q3", "schedule8", ["s1"]),
    ]
    ANSWERS = {"q1": ["p1"], "q2": ["p2"], "q3": ["s1"]}

    def run_method(self, retrieve, **kwargs):
        return asyncio.run(run_method("fake", retrieve, self.EXAMPLES, **kwargs))

    def test_sync_retriever_with_tokens(self):
        def retrieve(q, meter):
            meter.add("gpt-4o-mini", input_tokens=100, output_tokens=1)
            return ["p1", "p1", "s1"] if q.example_id != "q2" else ["x", "p2"]

        result = self.run_method(retrieve)
        json.dumps(result, ensure_ascii=False)  # 그대로 저장할 수 있어야 한다
        metrics = result["metrics"]
        self.assertEqual(set(metrics), {"policy80", "schedule8", "all88"})
        self.assertEqual(metrics["policy80"]["n"], 2)
        self.assertEqual(metrics["policy80"]["top1"], 0.5)
        self.assertEqual(metrics["schedule8"]["mrr10"], 0.5)
        self.assertEqual(metrics["all88"]["model_calls"], 3)
        self.assertEqual(metrics["all88"]["input_tokens"], 300)
        first = result["questions"][0]
        self.assertEqual(first["ranked"], ["p1", "s1"])  # 같은 parent는 첫 순위만
        self.assertEqual(first["model_calls_by_model"], {"gpt-4o-mini": 1})
        self.assertEqual(result["common"]["relevance_instruction"], RELEVANCE_INSTRUCTION)
        self.assertIn("|", format_table([result], "all88"))

    def test_async_retriever_without_model(self):
        async def retrieve(q, meter):
            self.assertFalse(hasattr(q, "expected"))  # 끼우는 함수는 정답을 받지 않는다
            return self.ANSWERS[q.example_id]

        metrics = self.run_method(retrieve)["metrics"]["all88"]
        self.assertEqual((metrics["top1"], metrics["model_calls"]), (1.0, 0))
        self.assertIsNone(metrics["input_tokens"])

    def test_call_limit_stops_the_run(self):
        def retrieve(q, meter):
            meter.add("qwen", calls=2)
            return []

        with self.assertRaises(ModelCallLimitExceeded):
            self.run_method(retrieve, max_model_calls=3)

    def test_rejects_bad_rankings(self):
        for bad in ("p1", [1], [f"p{i}" for i in range(11)]):
            with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
                self.run_method(lambda q, meter, bad=bad: bad)

    def test_meter_tokens_stay_none_until_reported(self):
        meter = Meter()
        meter.add("m")
        self.assertIsNone(meter.input_tokens)
        meter.add("m", input_tokens=5)
        self.assertEqual((meter.total_calls, meter.input_tokens), (2, 5))


class LoadQuestionsTest(unittest.TestCase):
    def test_subset_by_80_file(self):
        def row(example_id, date=""):
            return {
                "example_id": example_id,
                "request": {
                    "corpus": "policy_terms",
                    "text": "t",
                    "accident_date": date,
                    "filters": {"document_id": "d"},
                    "top_k": 10,
                },
                "expected_parent_ids": ["p"],
            }

        with tempfile.TemporaryDirectory() as tmp:
            all_path, old_path = Path(tmp, "all.jsonl"), Path(tmp, "80.jsonl")
            rows = [row("a"), row("b", "2024-06-01")]
            all_path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            old_path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
            examples = load_examples(all_path, old_path)
        self.assertEqual([e.subset for e in examples], ["policy80", "schedule8"])
        self.assertEqual(examples[1].question.accident_date, "2024-06-01")
        self.assertEqual(examples[0].question.filters, {"document_id": "d"})
        self.assertEqual(examples[0].expected, ("p",))


def fake_corpus():
    def chunk(parent_id, chunk_id, page, text):
        return {
            "corpus": "policy_terms",
            "parent_id": parent_id,
            "chunk_id": chunk_id,
            "page_start": page,
            "text": text,
        }

    return SimpleNamespace(
        chunks=[
            chunk("art3", "c2", 60, "제3조(보상)\n둘째"),
            chunk("art3", "c1", 59, "제3조(보상)\n첫째"),
            chunk("sched", "c3", 100, "<별표 3> 과실상계 | 4. 기왕증\n본문"),
            chunk("note", "c4", 70, "약관 비고·각주\n각주"),
            {"corpus": "fault_standards", "parent_id": "x", "chunk_id": "x", "page_start": 1},
        ],
        records=[
            {"corpus": "policy_terms", "is_parent": True, "parent_id": "art3",
             "body": {"title": "제3조(보상)"}},
            {"corpus": "policy_terms", "is_parent": True, "parent_id": "sched",
             "body": {"schedule": "별표3", "schedule_title": "과실상계 등", "item": "4. 기왕증"}},
        ],
    )  # fmt: skip


class PolicyParentsTest(unittest.TestCase):
    def test_titles_and_passages_in_page_order(self):
        parents = PolicyParents(fake_corpus(), encoder=ENC)
        self.assertEqual(parents.parent_ids(), ["art3", "note", "sched"])
        self.assertEqual(parents.title("sched"), "별표3 과실상계 등 4. 기왕증")
        self.assertEqual(parents.title("note"), "약관 비고·각주")  # 레코드 제목 없음 → 첫 줄
        passages = parents.passages("art3")
        self.assertEqual([p.chunk_id for p in passages], ["c1", "c2"])
        self.assertEqual(passages[0].text, "제3조(보상)\n첫째")


class FakeSearch(PolicySearch):
    """색인 없이 청크 순위만 정해 둔다."""

    def __init__(self, parents_in_order):
        self.order = parents_in_order
        self.chunk_count = len(parents_in_order)
        self.requests = []
        self.modes = []

    async def chunks(self, question, mode, top_k):
        self.requests.append(top_k)
        self.modes.append(mode)
        return [{"parent_id": p} for p in self.order[:top_k]]


class UniqueParentsTest(unittest.TestCase):
    def test_grows_until_enough_unique_parents(self):
        search = FakeSearch(["a", "a", "a", "b", "b", "c", "d", "e", "f"])
        parents = asyncio.run(search.unique_parents(question(), "bm25", 4))
        self.assertEqual(parents, ["a", "b", "c", "d"])
        self.assertEqual(search.requests, [4, 8])

    def test_stops_when_results_run_out(self):
        search = FakeSearch(["a", "a", "b"])
        self.assertEqual(asyncio.run(search.unique_parents(question(), "bm25", 5)), ["a", "b"])

    def test_candidates_default_to_rrf_fifty(self):
        search = FakeSearch([f"p{i}" for i in range(60)])
        self.assertEqual(len(asyncio.run(search.candidate_parents(question()))), 50)
        self.assertEqual(set(search.modes), {"rrf"})

    def test_legacy_uses_ten_chunks(self):
        search = FakeSearch(["a"] * 5 + [f"p{i}" for i in range(20)])
        ranked = asyncio.run(search.legacy_retriever("bm25")(question(), Meter()))
        self.assertEqual(ranked, ["a", "p0", "p1", "p2", "p3", "p4"])
        filled = asyncio.run(search.parent_retriever("bm25")(question(), Meter()))
        self.assertEqual(len(filled), 10)


class CachedEmbedderTest(unittest.TestCase):
    def test_offline_cache_does_not_call_api(self):
        embedder = CachedEmbedder({"q": [1.0]})
        self.assertEqual(asyncio.run(embedder.embed("q")), [1.0])
        with self.assertRaises(QueryVectorMissing):
            asyncio.run(embedder.embed("없는 질의"))

    def test_api_cap(self):
        class Inner:
            async def embed(self, text):
                return [0.5]

        embedder = CachedEmbedder({}, inner=Inner(), allow_api=True, max_api_calls=1)
        self.assertEqual(asyncio.run(embedder.embed("a")), [0.5])
        self.assertEqual(asyncio.run(embedder.embed("a")), [0.5])  # 캐시
        with self.assertRaises(QueryVectorMissing):
            asyncio.run(embedder.embed("b"))
        self.assertEqual((embedder.api_calls, list(embedder.added)), (1, ["a"]))


@unittest.skipUnless(has_corpus(), "data/private/corpus-db-import/db_import 파일이 없다")
class RealCorpusParentsTest(unittest.TestCase):
    def test_every_policy_parent_has_title_and_passage(self):
        from app.retrieval.loader import read_corpus

        parents = PolicyParents(read_corpus(), encoder=ENC)
        ids = parents.parent_ids()
        self.assertEqual(len(ids), 382)
        self.assertTrue(all(parents.title(p) for p in ids))
        self.assertTrue(all(parents.passages(p) for p in ids))


if __name__ == "__main__":
    unittest.main()
