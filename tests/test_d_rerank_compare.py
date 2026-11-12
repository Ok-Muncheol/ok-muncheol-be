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
from evals.rerank_compare.decisionrag import (
    GPT_DEVIATIONS,
    GROUP_BUDGET,
    JEV_DEVIATIONS,
    JEV_RESOLVED,
    LAW_GROUP,
    NODE_BODY_TOKENS,
    PARENT_BUDGET,
    DecisionRagRetriever,
    build_tree,
    describe,
    jev_after_run,
)
from evals.rerank_compare.gpt_judge import GptJudge, GptJudgeError, cost_usd
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
from evals.rerank_compare.jev_judge import (
    API_URL,
    JevBudgetExceeded,
    JevJudge,
    JevJudgeError,
    build_state,
    split_round_robin,
)
from evals.rerank_compare.jev_judge import cost_usd as jev_cost_usd
from evals.rerank_compare.qwen_reranker import check_prompt_matches
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


def tree_corpus():
    """보통약관 2개 묶음 + 별표 + 특약 + 인용 법령 + 각주."""

    def chunk(pid, cid, page, text, **meta):
        return {"corpus": "policy_terms", "parent_id": pid, "chunk_id": cid, "page_start": page,
                "text": text, "metadata": meta}

    chunks = [
        chunk("a3", "a3-1", 59, "제3조(보상하는 손해)\n대인배상 본문",
              clause_type="general_article", article_number="3", side_note_parent_ids=["n59"]),
        chunk("a3", "a3-2", 60, "제3조(보상하는 손해)\n둘째 청크", clause_type="general_article",
              article_number="3", side_note_parent_ids=["n59"]),
        chunk("a12", "a12-1", 62, "제12조(보상하는 손해)\n자기신체사고 본문",
              clause_type="general_article", article_number="12", side_note_parent_ids=["n59"]),
        chunk("n59", "n59-1", 59, "약관 비고·각주\n각주 본문", clause_type="side_note"),
        chunk("s3", "s3-1", 103, "<별표 3> 과실상계 등 | 4. 기왕증\n기왕증 본문",
              clause_type="payment_standard", schedule="별표3"),
        chunk("r1", "r1-1", 122, "운전자 한정 특별약관\n특약 본문",
              clause_type="rider", rider_number="1"),
        chunk("law", "law-1", 268, "자동차보험약관 인용 법조문\n[도로교통법]\n제44조",
              clause_type="cited_law_appendix"),
    ]
    titles = {
        "a3": "제3조(보상하는 손해)",
        "a12": "제12조(보상하는 손해)",
        "r1": "운전자 한정 특별약관",
    }
    records = [
        {"corpus": "policy_terms", "is_parent": True, "parent_id": pid, "body": {"title": title}}
        for pid, title in titles.items()
    ]
    return SimpleNamespace(chunks=chunks, records=records)  # fmt: skip


class KeywordScorer:
    """글에 질문 낱말이 있으면 높은 점수(결정적). 부를 때마다 글 수를 센다."""

    model_name = "fake-reranker"

    def __init__(self):
        self.batches = []

    def score(self, question, texts):
        self.batches.append(len(texts))
        words = question.split()
        return [sum(w in t for w in words) / len(words) for t in texts], 10 * len(texts), 0


class DecisionRagTreeTest(unittest.TestCase):
    def setUp(self):
        self.parents = PolicyParents(tree_corpus(), encoder=ENC)
        self.tree = build_tree(self.parents, encoder=ENC)

    def test_groups_follow_toc_order_and_cover_every_parent(self):
        names = [g.group_id for g in self.tree.groups]
        self.assertEqual(len(names), 5)
        self.assertIn("제1절 대인배상Ⅰ", names[0])
        self.assertIn("제1절 자기신체사고", names[1])
        self.assertIn("<별표 3>", names[2])
        self.assertIn("특별약관 > Ⅰ.", names[3])
        self.assertEqual(names[4], LAW_GROUP)
        placed = [pid for g in self.tree.groups for pid in g.parent_ids]
        self.assertEqual(sorted(placed), sorted(self.parents.parent_ids()))

    def test_side_note_follows_first_linked_clause(self):
        first = self.tree.groups[0]
        self.assertEqual(first.parent_ids, ("a3", "n59"))
        self.assertIn("제3조(보상하는 손해) 각주", self.tree.parents["n59"].text)
        self.assertNotIn("각주", first.text)  # 묶음 글에는 각주 제목을 넣지 않는다

    def test_node_texts(self):
        self.assertIn("제3조(보상하는 손해)", self.tree.groups[0].text)
        self.assertIn("도로교통법", self.tree.groups[4].text)  # 인용 법령은 [법령명]
        a3 = self.tree.parents["a3"].text
        self.assertTrue(a3.startswith(f"{self.tree.groups[0].group_id} > 제3조(보상하는 손해)\n"))
        self.assertIn("둘째 청크", a3)  # 본문은 parent의 청크를 이어 붙인 앞부분

    def test_parent_body_is_cut_to_node_tokens(self):
        long_corpus = tree_corpus()
        long_corpus.chunks[0]["text"] = "제3조(보상하는 손해)\n" + "가" * 500
        tree = build_tree(PolicyParents(long_corpus, encoder=ENC), encoder=ENC)
        body = tree.parents["a3"].text.split("\n", 1)[1]
        self.assertEqual(len(body), NODE_BODY_TOKENS)

    def test_unknown_article_fails_loudly(self):
        bad = tree_corpus()
        for c in bad.chunks:
            if c["parent_id"] == "a12":
                c["metadata"]["article_number"] = "99"
        with self.assertRaises(ValueError):
            build_tree(PolicyParents(bad, encoder=ENC), encoder=ENC)


class DecisionRagNavigationTest(unittest.TestCase):
    def setUp(self):
        self.parents = PolicyParents(tree_corpus(), encoder=ENC)
        self.tree = build_tree(self.parents, encoder=ENC)
        self.scorer = KeywordScorer()

    def retriever(self, groups=2, parents=2):
        return DecisionRagRetriever(
            self.tree, self.parents, self.scorer, group_budget=groups, parent_budget=parents
        )

    def test_prunes_by_budget_and_records_stages(self):
        meter = Meter()
        ranked = asyncio.run(self.retriever()(Question("q", "대인배상 보상하는", "", {}), meter))
        self.assertEqual(ranked[0], "a3")
        l1, l2 = meter.stages
        self.assertEqual(l1["name"], "L1")
        self.assertEqual(len(l2["kept_parents"]), 2)
        self.assertTrue(set(l2["kept_parents"]) <= set(l1["kept_parents"]))
        self.assertTrue(set(ranked) <= set(l2["kept_parents"]))
        # 호출 = 묶음 5 + L1에서 남은 parent + L2에서 남은 parent의 청크
        chunks = sum(len(self.parents.chunks(p)) for p in l2["kept_parents"])
        self.assertEqual(self.scorer.batches, [5, len(l1["kept_parents"]), chunks])
        self.assertEqual(meter.total_calls, 5 + len(l1["kept_parents"]) + chunks)
        self.assertEqual(meter.input_tokens, 10 * meter.total_calls)

    def test_call_limit_stops_before_scoring(self):
        with self.assertRaises(ModelCallLimitExceeded):
            asyncio.run(self.retriever()(Question("q", "대인배상", "", {}), Meter(max_calls=4)))
        self.assertEqual(self.scorer.batches, [])

    def test_survival_is_scored_only_in_harness(self):
        examples = [
            Example(Question("q1", "대인배상 보상하는", "", {}), "policy80", ("a3",)),
            Example(Question("q2", "대인배상 보상하는", "", {}), "schedule8", ("s3",)),
        ]
        result = asyncio.run(run_method("drag", self.retriever(groups=1, parents=1), examples))
        rows = {r["example_id"]: r for r in result["questions"]}
        self.assertEqual([s["survival"] for s in rows["q1"]["stages"]], [1.0, 1.0])
        self.assertEqual([s["survival"] for s in rows["q2"]["stages"]], [0.0, 0.0])
        self.assertEqual(result["metrics"]["policy80"]["stage_survival"], {"L1": 1.0, "L2": 1.0})
        self.assertEqual(result["metrics"]["all88"]["stage_survival"], {"L1": 0.5, "L2": 0.5})
        self.assertIn("L1 0.500 · L2 0.500", format_table([result], "all88"))

    def test_describe_records_budgets_and_deviations(self):
        params = describe(self.tree, self.retriever())
        self.assertEqual(params["budgets"]["L1_groups"], 2)
        self.assertTrue(params["budgets_fixed_before_eval"])
        self.assertTrue(any("Qwen3-Reranker-0.6B" in d for d in params["deviations"]))
        self.assertTrue(any("하나씩" in d for d in params["deviations"]))
        self.assertEqual((GROUP_BUDGET, PARENT_BUDGET, NODE_BODY_TOKENS), (5, 20, 128))


def fake_completion(yes_logprob, *, prompt_tokens=100, cached=0, logprobs=True):
    top = [
        SimpleNamespace(token="Yes", logprob=yes_logprob),
        SimpleNamespace(token="No", logprob=-1.0),
    ]
    choice = SimpleNamespace(
        logprobs=SimpleNamespace(content=[SimpleNamespace(top_logprobs=top)]) if logprobs else None
    )
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=1,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )
    return SimpleNamespace(choices=[choice], usage=usage)


class FakeOpenAI:
    """chat.completions.create만 흉내 낸다. 글에 '관련'이 있으면 Yes 확률이 높다."""

    def __init__(self, fail=None, logprobs=True):
        self.requests = []
        self.fail = fail
        self.logprobs = logprobs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail:
            raise self.fail
        text = kwargs["messages"][1]["content"]
        yes = math.log(0.9) if "관련" in text else math.log(0.1)
        return fake_completion(yes, cached=20, logprobs=self.logprobs)


class GptJudgeTest(unittest.TestCase):
    def test_scores_each_text_separately_with_common_request(self):
        client = FakeOpenAI()
        judge = GptJudge(client=client, concurrency=2)
        scores, input_tokens, output_tokens = asyncio.run(judge.score("Q", ["관련 글", "다른 글"]))
        self.assertAlmostEqual(scores[0], 0.9)
        self.assertAlmostEqual(scores[1], 0.1)
        self.assertEqual((input_tokens, output_tokens, judge.cached_tokens), (200, 2, 40))
        usage = judge.usage()
        self.assertEqual(usage["requests"], 2)
        self.assertAlmostEqual(usage["cost_usd"], cost_usd(200, 40, 2))
        self.assertEqual(len(client.requests), 2)  # 글마다 한 요청(하나씩 판단)
        self.assertEqual(client.requests[0], gpt_request("Q", "관련 글"))

    def test_api_error_stops_without_original_message(self):
        judge = GptJudge(client=FakeOpenAI(fail=RuntimeError("sk-secret-key-in-message")))
        with self.assertRaises(GptJudgeError) as caught:
            asyncio.run(judge.score("Q", ["글"]))
        self.assertNotIn("sk-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_missing_logprobs_is_an_error(self):
        with self.assertRaises(GptJudgeError):
            asyncio.run(GptJudge(client=FakeOpenAI(logprobs=False)).score("Q", ["글"]))

    def test_cost(self):
        self.assertAlmostEqual(cost_usd(1_000_000, 0, 0), 0.15)
        self.assertAlmostEqual(cost_usd(1_000_000, 1_000_000, 1_000_000), 0.075 + 0.60)

    def test_gpt_retriever_reports_tokens_and_deviations(self):
        parents = PolicyParents(tree_corpus(), encoder=ENC)
        tree = build_tree(parents, encoder=ENC)
        retriever = DecisionRagRetriever(tree, parents, GptJudge(client=FakeOpenAI()))
        meter = Meter()
        asyncio.run(retriever(Question("q", "관련", "", {}), meter))
        self.assertEqual(meter.output_tokens, meter.total_calls)
        self.assertEqual(meter.calls, {"gpt-4o-mini": meter.total_calls})
        deviations = describe(tree, retriever, GPT_DEVIATIONS)["deviations"]
        self.assertTrue(any("gpt-4o-mini" in d for d in deviations))
        self.assertFalse(any("Qwen3-Reranker-0.6B(무료" in d for d in deviations))


class FakeJev:
    """TypeSafe /v1/systemone 흉내(httpx MockTransport). 글에 "관련"이 있으면 noul 0.9."""

    def __init__(self, *, statuses=(), model="jev-1.13.0", detail=None, reported_tokens=None):
        self.requests = []
        self.statuses = list(statuses)  # 앞에서부터 하나씩 이 상태로 답한다(그다음은 200)
        self.model = model
        self.detail = detail
        self.reported_tokens = reported_tokens  # usage.input_tokens를 이 값으로(비용 상한 시험)

    def handler(self, request):
        import httpx

        self.requests.append(request)
        if self.statuses:
            status = self.statuses.pop(0)
            headers = {"retry-after": "0"}
            return httpx.Response(status, headers=headers, json={"detail": self.detail})
        body = json.loads(request.content)
        candidates = body["state"]["candidates"]
        answers = {
            cid: {"type": "noul", "noul": 0.9 if "관련" in candidates[cid] else 0.1}
            for cid in body["questions"]
        }
        tokens = self.reported_tokens or len(request.content)
        usage = {"input_tokens": tokens, "output_tokens": len(answers)}
        return httpx.Response(200, json={"model": self.model, "answers": answers, "usage": usage})

    def judge(self, **kwargs):
        import httpx

        kwargs.setdefault("api_key", "ts-secret-key")
        kwargs.setdefault("encoder", ENC)
        kwargs.setdefault("retry_wait_cap", 0)
        return JevJudge(transport=httpx.MockTransport(self.handler), **kwargs)


class JevJudgeTest(unittest.TestCase):
    def test_shared_context_request_and_scores_in_order(self):
        fake = FakeJev()
        texts = ["무관 글", "관련 글", "무관 2"]
        scores, tokens_in, tokens_out = asyncio.run(fake.judge().score("질문", texts))
        self.assertEqual(scores, [0.1, 0.9, 0.1])
        self.assertEqual(len(fake.requests), 1)  # 세 글을 한 요청(공유 문맥)에
        request = fake.requests[0]
        self.assertEqual(str(request.url), API_URL)
        self.assertEqual(request.headers["authorization"], "Bearer ts-secret-key")
        self.assertNotIn("typesafe-danger-zone-long-context", request.headers)
        body = json.loads(request.content)
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertEqual(body["state"], build_state("질문", texts))
        self.assertEqual(
            body["questions"]["c001"],
            {"type": "noul", "instructions": {"task": RELEVANCE_INSTRUCTION, "candidate": "c001"}},
        )
        self.assertEqual(set(body["questions"]), {"c000", "c001", "c002"})
        self.assertEqual((tokens_in, tokens_out), (len(request.content), 3))

    def test_round_robin_split_under_budget(self):
        texts = [f"글{i}" * 10 for i in range(7)]
        whole = len(json.dumps(build_state("q", texts), ensure_ascii=False))
        batches = split_round_robin("q", texts, budget_o200k=whole // 2, encoder=ENC)
        k = len(batches)
        self.assertGreater(k, 1)
        self.assertEqual(batches, [list(range(s, 7, k)) for s in range(k)])
        for batch in batches:
            state = build_state("q", [texts[i] for i in batch])
            self.assertLessEqual(len(json.dumps(state, ensure_ascii=False)), whole // 2)
        one = split_round_robin("q", texts, budget_o200k=whole, encoder=ENC)
        self.assertEqual(one, [list(range(7))])

    def test_split_requests_merge_back_and_are_recorded(self):
        fake = FakeJev()
        texts = ["관련 " + "가" * 50, "무관 " + "나" * 50, "관련 " + "다" * 50]
        judge = fake.judge(budget_o200k=120)
        scores, _, _ = asyncio.run(judge.score("질문", texts))
        self.assertEqual(scores, [0.9, 0.1, 0.9])
        self.assertGreater(len(fake.requests), 1)
        usage = judge.usage()
        self.assertEqual(usage["stages_split"], 1)
        self.assertEqual(usage["requests"], len(fake.requests))
        self.assertFalse(usage["long_context"])
        self.assertEqual(sum(int(k) * v for k, v in usage["batch_sizes"].items()), 3)

    def test_text_too_long_alone_stops(self):
        with self.assertRaises(JevJudgeError):
            split_round_robin("q", ["가" * 500], budget_o200k=100, encoder=ENC)

    def test_retries_429_and_5xx_then_succeeds(self):
        fake = FakeJev(statuses=[429, 503])
        judge = fake.judge()
        scores, _, _ = asyncio.run(judge.score("q", ["관련"]))
        self.assertEqual(scores, [0.9])
        self.assertEqual((len(fake.requests), judge.usage()["retries"]), (3, 2))

    def test_client_error_stops_without_key_or_text(self):
        detail = [{"loc": ["body", "state"], "msg": "bad", "type": "invalid", "input": "비밀 글"}]
        fake = FakeJev(statuses=[422], detail=detail)
        with self.assertRaises(JevJudgeError) as caught:
            asyncio.run(fake.judge().score("q", ["비밀 글"]))
        message = str(caught.exception)
        self.assertIn("422", message)
        self.assertNotIn("비밀 글", message)
        self.assertNotIn("ts-secret-key", message)
        self.assertEqual(len(fake.requests), 1)  # 4xx는 다시 보내지 않는다

    def test_cost_cap_stops_before_sending(self):
        fake = FakeJev()
        with self.assertRaises(JevBudgetExceeded):
            asyncio.run(fake.judge(max_cost_usd=0.0).score("q", ["관련"]))
        self.assertEqual(fake.requests, [])

    def test_cost_cap_after_response(self):
        fake = FakeJev(reported_tokens=100_000_000)  # $4.2 — 보내기 전 어림(작음)은 통과
        with self.assertRaises(JevBudgetExceeded):
            asyncio.run(fake.judge(max_cost_usd=1.0).score("q", ["관련"]))
        self.assertEqual(len(fake.requests), 1)

    def test_usage_cost_models_and_no_key_in_describe(self):
        judge = FakeJev(model="jev-1.14.0").judge()
        asyncio.run(judge.score("q", ["관련", "무관"]))
        usage = judge.usage()
        self.assertAlmostEqual(usage["cost_usd"], jev_cost_usd(usage["input_tokens"]))
        self.assertAlmostEqual(jev_cost_usd(1_000_000), 0.042)
        self.assertEqual(usage["response_models"], {"jev-1.14.0": 1})
        self.assertNotIn("ts-secret-key", json.dumps(judge.describe() | usage, ensure_ascii=False))

    def test_missing_key_stops(self):
        with self.assertRaises(JevJudgeError):
            JevJudge(api_key="")

    def test_jev_retriever_uses_shared_context_per_stage(self):
        fake = FakeJev()
        parents = PolicyParents(tree_corpus(), encoder=ENC)
        tree = build_tree(parents, encoder=ENC)
        retriever = DecisionRagRetriever(tree, parents, fake.judge())
        meter = Meter()
        asyncio.run(retriever(Question("q", "관련", "", {}), meter))
        self.assertEqual(len(fake.requests), 3)  # L1·L2·L3 단계마다 한 요청
        self.assertEqual(meter.calls, {"jev-1.13.0": meter.total_calls})  # 판단 하나당 1회
        self.assertEqual([s["name"] for s in meter.stages], ["L1", "L2"])

    def test_jev_deviations_mark_resolved(self):
        self.assertTrue(all(d.startswith("[Jev로 해소]") for d in JEV_RESOLVED))
        self.assertEqual(list(JEV_DEVIATIONS[: len(JEV_RESOLVED)]), list(JEV_RESOLVED))
        self.assertFalse(any("Qwen3-Reranker-0.6B(무료" in d for d in JEV_DEVIATIONS))
        self.assertTrue(any("다지선다" in d and "넣지 않음" in d for d in JEV_DEVIATIONS))
        same = jev_after_run({"response_models": {"jev-1.13.0": 3}}, "jev-1.13.0")
        self.assertNotIn("deviations", same)
        other = jev_after_run({"response_models": {"jev-1.14.0": 3}}, "jev-1.13.0")
        self.assertIn("모델 버전: 논문 jev-1.13.0 → 실제 응답 jev-1.14.0", other["deviations"])


class QwenPromptTest(unittest.TestCase):
    def test_three_parts_equal_common_prompt(self):
        self.assertTrue(check_prompt_matches("질문", "글"))


@unittest.skipUnless(has_corpus(), "data/private/corpus-db-import/db_import 파일이 없다")
class RealCorpusTreeTest(unittest.TestCase):
    def test_toc_tree_covers_all_parents(self):
        from app.retrieval.loader import read_corpus

        parents = PolicyParents(read_corpus(), encoder=ENC)
        tree = build_tree(parents, encoder=ENC)
        self.assertEqual(len(tree.groups), 28)
        placed = [pid for g in tree.groups for pid in g.parent_ids]
        self.assertEqual(len(placed), 382)
        self.assertEqual(set(placed), set(parents.parent_ids()))
        nine = next(g for g in tree.groups if any(":general_article:9:" in p for p in g.parent_ids))
        # 데이터 coverage_context가 아니라 목차를 따른다
        self.assertIn("제3절 배상책임에서 공통으로 적용할 사항", nine.group_id)


if __name__ == "__main__":
    unittest.main()
