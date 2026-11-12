"""D: 재정렬 비교 평가 틀의 도표·결정례 확장(--corpus). 네트워크·모델 없음(토크나이저는 가짜)."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evals.rerank_compare import common
from evals.rerank_compare.common import (
    CORPUS,
    DISPUTE,
    FAULT,
    RELEVANCE_INSTRUCTION,
    CorpusParents,
    common_settings,
    gpt_request,
    qwen_prompt,
    relevance_instruction,
    relevance_user_text,
    resolve_corpus,
    set_corpus,
)
from evals.rerank_compare.corpus_trees import (
    MODIFIER_GUIDE_PATH,
    build_dispute_tree,
    build_fault_tree,
    chart_section,
    chart_titles,
)
from evals.rerank_compare.decisionrag import (
    DEVIATIONS,
    GPT_DEVIATIONS,
    JEV_DEVIATIONS,
    DecisionRagRetriever,
    build_corpus_tree,
    corpus_deviations,
    describe,
)
from evals.rerank_compare.harness import (
    Meter,
    Question,
    dataset_info,
    load_examples,
    rank_scores,
    subset_metrics,
)
from evals.rerank_compare.jev_judge import build_questions
from evals.rerank_compare.run import method_name
from tests.evidence_d import has_corpus


class CharEncoder:
    """글자 하나 = 토큰 하나."""

    def encode(self, text):
        return [ord(ch) for ch in text]

    def decode(self, tokens):
        return "".join(chr(t) for t in tokens)


ENC = CharEncoder()


class CorpusCase(unittest.TestCase):
    """코퍼스 설정은 프로세스 전역이라 테스트마다 약관으로 되돌린다."""

    def tearDown(self):
        set_corpus("policy")


class InstructionTest(CorpusCase):
    def test_policy_wording_unchanged(self):
        self.assertEqual(relevance_instruction(), RELEVANCE_INSTRUCTION)
        self.assertIn("policy clause", RELEVANCE_INSTRUCTION)
        self.assertEqual(common_settings()["prompt_version"], "relevance-v1")

    def test_only_noun_changes_per_corpus(self):
        for name, noun, version in (
            ("fault", "fault ratio standard chart", "relevance-v1-fault"),
            ("dispute", "fault ratio dispute decision case", "relevance-v1-dispute"),
        ):
            corpus = set_corpus(name)
            text = relevance_instruction()
            self.assertEqual(text, RELEVANCE_INSTRUCTION.replace("policy clause", noun))
            self.assertIn(text, relevance_user_text("질문", "글"))  # Qwen·GPT 입력
            self.assertIn(text, qwen_prompt("질문", "글"))
            self.assertIn(text, gpt_request("질문", "글")["messages"][1]["content"])
            settings = common_settings()
            self.assertEqual(settings["corpus"], corpus)
            self.assertEqual(settings["prompt_version"], version)
            self.assertEqual(settings["candidate_mode"], "rrf")  # R@50 같음 → RRF(11/12)
            task = build_questions(["c000"])["c000"]["instructions"]["task"]
            self.assertEqual(task, text)  # Jev noul 질문도 같은 문구

    def test_corpus_names(self):
        self.assertEqual(resolve_corpus("fault"), FAULT)
        self.assertEqual(resolve_corpus("dispute_cases"), DISPUTE)
        with self.assertRaises(ValueError):
            resolve_corpus("pedestrian")
        self.assertEqual(method_name("rrf"), "rrf")  # 약관은 예전 파일 이름 그대로
        set_corpus("fault")
        self.assertEqual(method_name("rrf"), "fault-rrf")
        self.assertEqual(common.active_corpus(), FAULT)


def eval_row(example_id, corpus, expected="p"):
    return {
        "example_id": example_id,
        "request": {
            "corpus": corpus,
            "text": "t",
            "accident_date": "",
            "filters": {"document_id": "doc"},
            "top_k": 10,
        },
        "expected_parent_ids": [expected],
    }


class CorpusQuestionsTest(CorpusCase):
    def test_fault_set_is_one_subset_with_filters(self):
        set_corpus("fault")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "e.jsonl")
            rows = [eval_row("a", FAULT), eval_row("b", FAULT)]
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            examples = load_examples(path)
            info = dataset_info(examples)
            with self.assertRaises(ValueError):  # 다른 코퍼스 문항이 섞이면 멈춘다
                path.write_text(json.dumps(eval_row("c", DISPUTE)), encoding="utf-8")
                load_examples(path)
        self.assertEqual([e.subset for e in examples], ["fault100", "fault100"])
        self.assertEqual(examples[0].question.filters, {"document_id": "doc"})
        self.assertEqual(info["corpus"], FAULT)
        self.assertEqual(info["subsets"], {"fault100": 2})

    def test_metrics_keyed_by_corpus_subset(self):
        rows = [
            {
                "subset": "dispute100",
                "scores": rank_scores(["p"], ["p"]),
                "seconds": 1.0,
                "model_calls_by_model": {},
                "input_tokens": None,
                "output_tokens": None,
                "stages": [],
            }
        ]
        metrics = subset_metrics(rows, DISPUTE)
        self.assertEqual(list(metrics), ["dispute100"])
        self.assertEqual(metrics["dispute100"]["top1"], 1.0)


def chunk(corpus, pid, cid, page, text):
    return {"corpus": corpus, "parent_id": pid, "chunk_id": cid, "page_start": page, "text": text,
            "metadata": {}}  # fmt: skip


def fault_corpus():
    """도표 3개(두 절) + 공통 수정요소 해설 1개 + 결정례 2건."""
    guide = "fault-standards-2023-06:modifier-guide:130"
    chunks = [
        chunk(FAULT, "f:차1-1", "a", 148, "차1-1 녹색직진 대 적색직진\n사고 상황\n본문"),
        chunk(FAULT, "f:차1-1", "b", 149, "차1-1 녹색직진 대 적색직진\n관련 법규\n법"),
        chunk(FAULT, "f:차2-1", "c", 164, "차2-1 적색직진 대 녹색화살표 신호좌회전\n본문"),
        chunk(FAULT, "f:차43-2", "d", 371, "차43-2 후행 직진 대 선행 진로변경\n본문"),
        chunk(FAULT, guide, "e", 130, "① 졸음운전\n해설"),
        chunk(DISPUTE, "d:1", "g", 18, "심의번호 2018-1\n사고 사실\n신호위반"),
        chunk(DISPUTE, "d:2", "h", 40, "심의번호 2018-2\n사고 사실\n진로변경"),
    ]

    def chart(pid, chart_id, title):
        return {"corpus": FAULT, "is_parent": True, "parent_id": pid,
                "body": {"chart_id": chart_id, "title": title}}  # fmt: skip

    def case(pid, case_id, chart_id, title):
        return {"corpus": DISPUTE, "is_parent": True, "parent_id": pid,
                "body": {"case_id": case_id, "chart_id": chart_id, "title": title}}  # fmt: skip

    records = [
        chart("f:차1-1", "차1-1", "녹색직진 대 적색직진"),
        chart("f:차2-1", "차2-1", "적색직진 대 녹색화살표\n신호좌회전"),
        chart("f:차43-2", "차43-2", "후행 직진 대 선행 진로변경"),
        case("d:1", "2018-1", "차1-1", "한쪽 차량 신호위반 사고\n(기본과실)"),
        case("d:2", "2018-2", "차43-2", "진로변경 사고"),
    ]
    return SimpleNamespace(chunks=chunks, records=records), guide


class CorpusParentsTitleTest(unittest.TestCase):
    def test_titles_match_chunk_first_line(self):
        corpus, guide = fault_corpus()
        fault = CorpusParents(corpus, encoder=ENC, name=FAULT)
        self.assertEqual(fault.title("f:차2-1"), "차2-1 적색직진 대 녹색화살표 신호좌회전")
        self.assertEqual(fault.title(guide), "① 졸음운전")  # 레코드 없음 → 첫 줄
        # 제목과 같은 첫 줄은 passage_text가 뗀다
        first = fault.passages("f:차1-1")[0].text
        self.assertEqual(first, "차1-1 녹색직진 대 적색직진\n사고 상황\n본문")
        dispute = CorpusParents(corpus, encoder=ENC, name=DISPUTE)
        self.assertEqual(dispute.parent_ids(), ["d:1", "d:2"])
        self.assertEqual(dispute.title("d:1"), "심의번호 2018-1")
        self.assertEqual(dispute.bodies["d:2"]["chart_id"], "차43-2")


class CorpusTreeTest(unittest.TestCase):
    def test_chart_number_to_toc_section(self):
        self.assertIn("(1) 양쪽 신호등 있는 교차로", chart_section("차4-1")[1])
        self.assertIn("(5) 신호등 없는 교차로", chart_section("차18-2")[1])
        self.assertIn("다. 같은 방향", chart_section("차43-7")[1])
        self.assertTrue(chart_section("차61-3")[1].endswith("마. 자동차 대 이륜차 특수유형"))
        for bad in ("차22-1", "보1-1", "차1"):
            with self.assertRaises(ValueError):
                chart_section(bad)

    def test_fault_tree(self):
        corpus, guide = fault_corpus()
        parents = CorpusParents(corpus, encoder=ENC, name=FAULT)
        tree = build_fault_tree(parents, encoder=ENC)
        self.assertEqual(tree.version, "fault-toc-v1")
        self.assertEqual(tree.corpus, FAULT)
        self.assertEqual(
            [g.parent_ids for g in tree.groups],
            [(guide,), ("f:차1-1", "f:차2-1"), ("f:차43-2",)],  # 수정요소 해설이 목차 앞
        )
        self.assertEqual(tree.groups[0].group_id, MODIFIER_GUIDE_PATH)
        self.assertIn("(2) 중대한 과실", tree.groups[0].text)
        self.assertIn("차1-1 녹색직진 대 적색직진, 차2-1 적색직진", tree.groups[1].text)
        node = tree.parents["f:차1-1"].text
        head = f"{tree.groups[1].group_id} > 차1-1 녹색직진 대 적색직진\n"
        self.assertTrue(node.startswith(head))
        self.assertIn("관련 법규", node)  # 청크를 이어 붙인 본문 앞부분

    def test_dispute_tree_follows_chart_link(self):
        corpus, _ = fault_corpus()
        parents = CorpusParents(corpus, encoder=ENC, name=DISPUTE)
        charts = chart_titles(corpus)
        self.assertEqual(charts["차2-1"], "차2-1 적색직진 대 녹색화살표 신호좌회전")
        tree = build_dispute_tree(parents, charts, encoder=ENC)
        self.assertEqual(tree.version, "dispute-chart-v1")
        self.assertEqual([g.parent_ids for g in tree.groups], [("d:1",), ("d:2",)])
        self.assertEqual(tree.groups[0].group_id, chart_section("차1-1")[1])
        self.assertIn("차1-1 녹색직진 대 적색직진", tree.groups[0].text)
        node = tree.parents["d:1"].text
        label = "> 차1-1 녹색직진 대 적색직진 > 심의번호 2018-1 한쪽 차량 신호위반 사고 (기본과실)"
        self.assertIn(label, node)
        with self.assertRaises(ValueError):  # 도표 코퍼스에 없는 연결
            build_dispute_tree(parents, {"차1-1": "x"}, encoder=ENC)
        with self.assertRaises(ValueError):
            build_fault_tree(parents, encoder=ENC)

    def test_navigation_records_stages_and_budgets(self):
        corpus, _ = fault_corpus()
        search = SimpleNamespace(
            corpus_name=FAULT, corpus=corpus, parents=CorpusParents(corpus, encoder=ENC, name=FAULT)
        )
        tree = build_corpus_tree(search)

        class Scorer:
            model_name = "fake"

            def score(self, question, texts):
                return [float("진로변경" in t) for t in texts], len(texts), 0

        retriever = DecisionRagRetriever(tree, search.parents, Scorer(), group_budget=1)
        meter = Meter()
        import asyncio

        ranked = asyncio.run(retriever(Question("q", "진로변경", "", {}), meter))
        self.assertEqual(ranked, ["f:차43-2"])
        self.assertEqual([s["name"] for s in meter.stages], ["L1", "L2"])
        self.assertEqual(meter.stages[0]["kept_parents"], ["f:차43-2"])
        params = describe(tree, retriever, corpus_deviations(FAULT, "qwen"))
        self.assertEqual(params["corpus"], FAULT)
        self.assertEqual(params["tree"]["groups"], 3)


class CorpusDeviationsTest(unittest.TestCase):
    def test_policy_deviations_unchanged(self):
        self.assertEqual(corpus_deviations(CORPUS, "qwen"), DEVIATIONS)
        self.assertEqual(corpus_deviations(CORPUS, "gpt"), GPT_DEVIATIONS)
        self.assertEqual(corpus_deviations(CORPUS, "jev"), JEV_DEVIATIONS)

    def test_corpus_items(self):
        fault = corpus_deviations(FAULT, "jev")
        dispute = corpus_deviations(DISPUTE, "gpt")
        self.assertFalse(any("약관 목차" in d for d in fault + dispute))
        self.assertTrue(any("도표 원문 목차" in d for d in fault))
        self.assertTrue(any("chart_link_human_reviewed False" in d for d in dispute))
        self.assertTrue(any(d.startswith("[Jev로 해소]") for d in fault))
        self.assertTrue(any("Jev로 할 수 있으나" in d for d in fault))
        with self.assertRaises(ValueError):
            corpus_deviations(FAULT, "bm25")


@unittest.skipUnless(has_corpus(), "data/private/corpus-db-import/db_import 파일이 없다")
class RealCorpusTreesTest(unittest.TestCase):
    def test_trees_cover_all_parents(self):
        from app.retrieval.loader import read_corpus

        corpus = read_corpus()
        for name, groups, count in ((FAULT, 24, 127), (DISPUTE, 21, 226)):
            parents = CorpusParents(corpus, encoder=ENC, name=name)
            search = SimpleNamespace(corpus_name=name, corpus=corpus, parents=parents)
            tree = build_corpus_tree(search)
            placed = [pid for g in tree.groups for pid in g.parent_ids]
            self.assertEqual(len(tree.groups), groups)
            self.assertEqual(len(placed), count)
            self.assertEqual(set(placed), set(parents.parent_ids()))
            self.assertTrue(all(parents.title(p) for p in placed))


if __name__ == "__main__":
    unittest.main()
