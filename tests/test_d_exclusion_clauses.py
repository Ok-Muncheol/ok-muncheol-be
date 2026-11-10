"""D: 면책 근거 = 조항 ID 직접 조회뿐(벡터 검색 없음), C의 validate_hits로 검증. 네트워크 없음.

정답은 약관 원문에서 확인했다. 제23조 제1호 고의(p.67), 제14호 무면허·음주(p.68).
"""

import asyncio
import json
import unittest
from unittest.mock import patch

from app.api import reports as api
from app.calculation.exclusions import (
    NEEDS_CLAUSE,
    fetch_exclusion_clauses,
    flag_judge,
    llm_judge,
    review_exclusions,
)
from app.calculation.rules import DEFAULT_RULES, RuleSet
from app.reports.render import to_json
from app.retrieval.common import configure_retrieval, fetch_by_parent
from app.retrieval.evidence_store import LocalCorpusEvidenceStore
from app.retrieval.hybrid import RetrievalOptions
from app.retrieval.loader import build_index, read_corpus
from app.tools import _evidence
from tests import fixtures_d as fx
from tests.evidence_d import has_corpus, skip_validation

ARTICLE_23 = "samsung-personal-auto-2026-11-11:general_article:23:p67"
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}
TOPICS = DEFAULT_RULES.exclusion_topics


def hit(chunk_id, parent_id="clause-1", score=0.5, text="(합성) 조항"):
    return {
        "chunk": {
            "chunk_id": chunk_id,
            "parent_id": parent_id,
            "document": {},
            "page_start": 1,
            "page_end": 1,
            "text": text,
            "metadata": {},
        },
        "score": score,
        "retrieval_version": "synthetic",
    }


class Spy:
    """검색·조회 호출을 기록하는 가짜 함수."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __call__(self, *args, **kw):
        self.calls.append((args, kw))
        return self.result


def ids(hits):
    return [h["chunk"]["chunk_id"] for h in hits]


class RulesTest(unittest.TestCase):
    def test_every_topic_maps_to_article_23(self):
        self.assertEqual(set(DEFAULT_RULES.exclusion_clause_parents), set(TOPICS))
        for topic in TOPICS:
            self.assertEqual(DEFAULT_RULES.exclusion_clause_parents[topic], (ARTICLE_23,))

    def test_rule_set_still_example_and_hashable(self):
        self.assertTrue(DEFAULT_RULES.is_example)
        hash(RuleSet(version="reviewed-test"))


class SkipValidation(unittest.TestCase):
    """합성 청크를 쓰는 시험. 원문 대조(validate_hits)는 끈다. 실제 대조는 RealDataTest에 있다."""

    def setUp(self):
        patcher = patch("app.calculation.exclusions.validate_hits", skip_validation)
        patcher.start()
        self.addCleanup(patcher.stop)


class ReviewTest(SkipValidation):
    def review(self, facts, *, fetch, judge=flag_judge, terms_edition="판-1"):
        contract = {"terms_edition": terms_edition}
        return asyncio.run(review_exclusions(contract, facts, fetch=fetch, judge=judge))

    def test_judge_receives_only_directly_fetched_clauses(self):
        seen = {}

        def judge(topic, hits, facts):
            seen[topic] = ids(hits)
            return flag_judge(topic, hits, facts)

        fetch = Spy([hit("p67", ARTICLE_23, 1.0), hit("p68", ARTICLE_23, 1.0)])
        checks = self.review(NO_FLAGS, fetch=fetch, judge=judge)
        self.assertEqual(seen, dict.fromkeys(TOPICS, ["p67", "p68"]))
        self.assertEqual(checks[0]["clause_ids"], [ARTICLE_23])  # 조항 ID 중복 없음

    def test_fetch_arguments(self):
        fetch = Spy([hit("p67", ARTICLE_23, 1.0)])
        self.review(NO_FLAGS, fetch=fetch)
        self.assertEqual(
            fetch.calls[0], (("policy_terms", [ARTICLE_23]), {"terms_edition": "판-1"})
        )

    def test_missing_clause_without_input_marks_only(self):
        def judge(*args):
            raise AssertionError("조항이 없으면 판정기를 부르지 않는다")

        checks = self.review({}, fetch=Spy([]), judge=judge)
        self.assertEqual({(c["result"], c["detail"]) for c in checks}, {("unknown", NEEDS_CLAUSE)})
        self.assertEqual({c["decided_by"] for c in checks}, {"code"})

    def test_missing_clause_keeps_handler_input(self):
        checks = self.review({"무면허": False, "음주": True}, fetch=Spy([]))
        by_key = {c["key"]: c for c in checks}
        self.assertEqual(by_key["exclusion:무면허"]["result"], "pass")
        self.assertEqual(by_key["exclusion:무면허"]["decided_by"], "handler")
        self.assertEqual(by_key["exclusion:음주"]["result"], "fail")
        self.assertIn("약관 조항 미연결", by_key["exclusion:음주"]["detail"])
        self.assertEqual(by_key["exclusion:고의"]["detail"], NEEDS_CLAUSE)

    def test_topic_without_clause_id_marks_only(self):
        rules = RuleSet(exclusion_clause_parents={})
        fetch = Spy([hit("p67", ARTICLE_23, 1.0)])
        checks = asyncio.run(review_exclusions(None, {}, fetch=fetch, rules=rules))
        self.assertEqual({c["detail"] for c in checks}, {NEEDS_CLAUSE})
        self.assertEqual(fetch.calls, [])

    def test_llm_judge_can_cite_directly_fetched_clause(self):
        def llm(prompt):
            self.assertIn("직접 가져온 제14호", prompt)
            answer = {"result": "fail", "clause_ids": [ARTICLE_23], "detail": "제14호"}
            return json.dumps(answer, ensure_ascii=False)

        fetch = Spy([hit("p68", ARTICLE_23, 1.0, text="직접 가져온 제14호")])
        checks = self.review({}, fetch=fetch, judge=llm_judge(llm))
        self.assertEqual(checks[0]["clause_ids"], [ARTICLE_23])


class ValidationTest(unittest.TestCase):
    """직접 조회 결과도 C의 validate_hits를 거친다. 실패하면 조항 미확인으로 처리한다(막지 않음)."""

    def gather(self, fetch, edition="판-1"):
        return asyncio.run(fetch_exclusion_clauses(TOPICS[0], edition=edition, fetch=fetch))

    def test_validate_hits_gets_matching_request(self):
        seen = []

        async def validate(request, hits):
            seen.append(request)
            return hits

        hits = [hit("p67", ARTICLE_23, 1.0), hit("p68", ARTICLE_23, 1.0)]
        with patch("app.calculation.exclusions.validate_hits", validate):
            self.assertEqual(self.gather(Spy(hits)), hits)
            self.gather(Spy(hits), edition=None)
        self.assertEqual(
            (seen[0]["corpus"], seen[0]["filters"], seen[0]["top_k"]),
            ("policy_terms", {"edition": "판-1"}, 2),
        )
        self.assertEqual(seen[1]["filters"], {})  # 판 없음 → 기본 판 규칙으로 조회된 결과

    def test_detached_chunk_is_dropped_with_warning(self):
        with self.assertLogs("app.calculation.exclusions", "WARNING"):
            self.assertEqual(self.gather(Spy([hit("없는-청크", ARTICLE_23, 1.0)])), [])

    def test_failed_validation_marks_clause_unconfirmed_and_handler_true_still_applies(self):
        fetch = Spy([hit("없는-청크", ARTICLE_23, 1.0)])

        def judge(*args):
            raise AssertionError("검증 실패한 근거로 판정하지 않는다")

        with self.assertLogs("app.calculation.exclusions", "WARNING"):
            checks = asyncio.run(review_exclusions(None, {"음주": True}, fetch=fetch, judge=judge))
        by_key = {c["key"]: c for c in checks}
        self.assertEqual(by_key["exclusion:음주"]["result"], "fail")
        self.assertEqual(by_key["exclusion:무면허"]["detail"], NEEDS_CLAUSE)

    def test_unregistered_evidence_store_fails_closed(self):
        """EvidenceStore(DB 어댑터) 등록 전에는 C 검증이 RuntimeError → 조항 미확인(fail-closed)."""
        _evidence.configure_evidence_store(None)
        with self.assertLogs("app.calculation.exclusions", "WARNING") as logs:
            self.assertEqual(self.gather(Spy([hit("p67", ARTICLE_23, 1.0)])), [])
        self.assertIn("not configured", logs.output[0])

    def test_missing_corpus_file_does_not_block(self):
        async def unavailable(request, hits):
            raise RuntimeError("Corpus source file unavailable")

        with patch("app.calculation.exclusions.validate_hits", unavailable):
            with self.assertLogs("app.calculation.exclusions", "WARNING"):
                self.assertEqual(self.gather(Spy([hit("p67", ARTICLE_23, 1.0)])), [])


class HandlerPriorityTest(SkipValidation):
    """담당자 입력이 LLM보다 우선한다. 입력이 없을 때만 LLM 결과를 따른다."""

    actor, case = "handler-1", "synthetic-case"

    def setUp(self):
        super().setUp()
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(version="reviewed-test"),
            fetch_clauses=Spy([hit("p68", ARTICLE_23, 1.0)]),
        )

    def tearDown(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            judge=flag_judge,
        )

    def flow(self, answer, facts):
        ids = [ARTICLE_23] if answer != "unknown" else []
        reply = {"result": answer, "clause_ids": ids, "detail": "제14호 검토"}
        api.configure(judge=llm_judge(lambda prompt: json.dumps(reply, ensure_ascii=False)))
        asyncio.run(api.submit_judgment(self.actor, fx.card(), fx.judgment()))
        return asyncio.run(
            api.add_documents(self.actor, self.case, [fx.contract(), fx.estimate()], facts)
        )

    def check(self, report, topic=TOPICS[1]):
        return next(c for c in report["calculation"]["checks"] if c["label"] == topic)

    def section8(self, report):
        return next(s for s in report["sections"] if s["no"] == 8)["body"]

    def test_true_is_not_payable_whatever_llm_says(self):
        for answer in ("unknown", "pass", "fail"):
            with self.subTest(answer=answer):
                api.configure(repo=api.InMemoryReportRepository())
                report = self.flow(answer, {**NO_FLAGS, "음주": True})
                calc = report["calculation"]
                self.assertEqual((calc["status"], calc["insurance_payment"]), ("not_payable", None))
                check = self.check(report)
                self.assertEqual((check["result"], check["decided_by"]), ("fail", "handler"))
                self.assertIn(f"LLM 판정 {answer}", check["detail"])
                out = asyncio.run(api.confirm_report(self.actor, self.case, 2))
                self.assertEqual(out["claim_blocked_reasons"], ["지급 불가 — 구상할 보험금 없음"])

    def test_true_shows_clause_cited_by_llm(self):
        report = self.flow("fail", {**NO_FLAGS, "음주": True})
        check = self.check(report)
        self.assertEqual(check["clause_ids"], [ARTICLE_23])
        self.assertIn(f"근거 조항 {ARTICLE_23}", check["detail"])
        self.assertIn(ARTICLE_23, report["calculation"]["clause_ids"])
        self.assertTrue(any(ARTICLE_23 in r for r in report["calculation"]["reasons"]))

    def test_true_without_citation_says_so(self):
        check = self.check(self.flow("unknown", {**NO_FLAGS, "음주": True}))
        self.assertEqual(check["clause_ids"], [])
        self.assertIn("인용 조항 없음", check["detail"])

    def test_false_and_llm_pass_passes_without_warning(self):
        report = self.flow("pass", NO_FLAGS)
        check = self.check(report)
        self.assertEqual((check["result"], check["detail"]), ("pass", "담당자 확인 — 해당 없음"))
        self.assertNotIn("warning", check)
        self.assertEqual(report["calculation"]["reasons"], [])
        self.assertEqual(report["calculation"]["status"], "confirmed")

    def test_false_and_llm_fail_passes_with_warning_in_section_8(self):
        report = self.flow("fail", NO_FLAGS)
        calc = report["calculation"]
        self.assertEqual(calc["insurance_payment"], 9_500_000)
        check = self.check(report)
        warning = f"LLM은 면책 가능성 제시(조항 {ARTICLE_23}) — 담당자 확인"
        self.assertEqual((check["result"], check["warning"]), ("pass", warning))
        eight = self.section8(report)
        row = next(x for x in eight["산정 전 검증"] if x["항목"] == TOPICS[1])
        self.assertEqual((row["결과"], row["내용"]), ("통과", warning))
        self.assertTrue(any(warning in r for r in eight["사유"]))
        out = asyncio.run(api.confirm_report(self.actor, self.case, 2))
        self.assertIsNotNone(out["package"])  # 막지 않음

    def test_internal_judgment_stays_in_report_not_in_package(self):
        report = self.flow("fail", NO_FLAGS)
        warning = f"LLM은 면책 가능성 제시(조항 {ARTICLE_23}) — 담당자 확인"
        eight = self.section8(report)
        self.assertIn(warning, str(eight["사유"]))
        self.assertIn(warning, str(eight["산정 전 검증"]))
        pkg = asyncio.run(api.confirm_report(self.actor, self.case, 2))
        for name, text in (("JSON", to_json(pkg["package"])), ("HTML", pkg["html"])):
            with self.subTest(name):
                for internal in (
                    warning,
                    "LLM",
                    "면책 가능성",
                    "제14호 검토",
                    "산정 사유",
                    "산정 상태",
                ):
                    self.assertNotIn(internal, text)
        self.assertIn(
            warning, str(self.section8(report)["사유"])
        )  # 패키지를 만들어도 보고서는 그대로

    def test_unresolved_validation_not_in_package(self):
        judgment = fx.judgment()
        judgment["checks"] = [
            {**judgment["checks"][0], "passed": False, "reasons": ["내부 반박 사유"]},
            *judgment["checks"][1:],
        ]
        api.configure(judge=flag_judge)
        asyncio.run(api.submit_judgment(self.actor, fx.card(), judgment))
        report = asyncio.run(api.add_documents(self.actor, self.case, [fx.estimate()], NO_FLAGS))
        self.assertIn("내부 반박 사유", str(next(s for s in report["sections"] if s["no"] == 7)))
        out = asyncio.run(api.confirm_report(self.actor, self.case, 2))
        self.assertNotIn("내부 반박 사유", to_json(out["package"]))
        self.assertNotIn("내부 반박 사유", out["html"])
        self.assertTrue(out["package"]["basis"]["유사 결정례"])  # 결정례 근거는 유지

    def test_warning_survives_with_not_payable_from_another_flag(self):
        reply = {
            TOPICS[0]: {"result": "fail", "clause_ids": [ARTICLE_23], "detail": "x"},
            TOPICS[1]: {"result": "unknown", "clause_ids": [], "detail": "x"},
            TOPICS[2]: {"result": "pass", "clause_ids": [], "detail": "x"},
        }

        def llm(prompt):
            topic = next(t for t in TOPICS if t in prompt)
            return json.dumps(reply[topic], ensure_ascii=False)

        api.configure(judge=llm_judge(llm))
        asyncio.run(api.submit_judgment(self.actor, fx.card(), fx.judgment()))
        report = asyncio.run(
            api.add_documents(self.actor, self.case, [fx.estimate()], {**NO_FLAGS, "음주": True})
        )
        reasons = report["calculation"]["reasons"]
        self.assertEqual(report["calculation"]["status"], "not_payable")
        self.assertTrue(any("LLM은 면책 가능성 제시" in r for r in reasons), reasons)

    def test_false_and_llm_unknown_passes(self):
        check = self.check(self.flow("unknown", NO_FLAGS))
        self.assertEqual((check["result"], check["decided_by"]), ("pass", "handler"))
        self.assertNotIn("warning", check)

    def test_no_input_follows_llm(self):
        report = self.flow("fail", {})
        self.assertEqual(report["calculation"]["status"], "not_payable")
        self.assertEqual(self.check(report)["decided_by"], "llm")

    def test_no_input_and_unknown_only_marks(self):
        report = self.flow("unknown", {})
        check = self.check(report)
        self.assertEqual(
            (check["result"], check["detail"]), ("unknown", "면책 확인 필요 — 사실 미입력")
        )
        self.assertEqual(report["calculation"]["insurance_payment"], 9_500_000)
        out = asyncio.run(api.confirm_report(self.actor, self.case, 2))
        self.assertIsNotNone(out["package"])

    def test_llm_failure_does_not_override_handler_true(self):
        def broken(prompt):
            raise ValueError("LLM 호출 실패: TimeoutError")

        api.configure(judge=llm_judge(broken))
        asyncio.run(api.submit_judgment(self.actor, fx.card(), fx.judgment()))
        report = asyncio.run(
            api.add_documents(self.actor, self.case, [fx.estimate()], {**NO_FLAGS, "무면허": True})
        )
        self.assertEqual(report["calculation"]["status"], "not_payable")


class ApiFlowTest(SkipValidation):
    actor, case = "handler-1", "synthetic-case"

    def setUp(self):
        super().setUp()
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), judge=flag_judge)

    def tearDown(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            judge=flag_judge,
        )

    def run_flow(self, facts=None):
        asyncio.run(api.submit_judgment(self.actor, fx.card(), fx.judgment()))
        return asyncio.run(api.add_documents(self.actor, self.case, [fx.estimate()], facts))

    def exclusions(self, report):
        return [c for c in report["calculation"]["checks"] if c["key"].startswith("exclusion:")]

    def test_missing_clause_does_not_block(self):
        api.configure(fetch_clauses=Spy([]))
        report = self.run_flow()
        self.assertEqual({c["detail"] for c in self.exclusions(report)}, {NEEDS_CLAUSE})
        self.assertEqual(report["calculation"]["insurance_payment"], 9_500_000)
        out = asyncio.run(api.confirm_report(self.actor, self.case, 2))
        self.assertIsNotNone(out["package"])

    def test_missing_clause_with_handler_true_is_not_payable(self):
        api.configure(fetch_clauses=Spy([]))
        report = self.run_flow({**NO_FLAGS, "음주": True})
        self.assertEqual(report["calculation"]["status"], "not_payable")
        out = asyncio.run(api.confirm_report(self.actor, self.case, 2))
        self.assertEqual(out["claim_blocked_reasons"], ["지급 불가 — 구상할 보험금 없음"])

    def test_clause_ids_reach_section_8(self):
        api.configure(
            fetch_clauses=Spy([hit("p67", ARTICLE_23, 1.0), hit("p68", ARTICLE_23, 1.0)]),
        )
        report = self.run_flow(NO_FLAGS)
        self.assertEqual({tuple(c["clause_ids"]) for c in self.exclusions(report)}, {(ARTICLE_23,)})

    def test_unimplemented_fetch_falls_back_to_handler_input(self):
        async def not_ready(*args, **kw):
            raise NotImplementedError

        api.configure(fetch_clauses=not_ready)
        report = self.run_flow(NO_FLAGS)
        self.assertEqual({c["decided_by"] for c in self.exclusions(report)}, {"handler"})


@unittest.skipUnless(has_corpus(), "data/private/corpus-db-import/db_import 파일이 없다")
class RealDataTest(unittest.TestCase):
    """실제 약관 색인과 실제 validate_hits로 판정기 입력을 확인한다. API 호출 없음."""

    @classmethod
    def setUpClass(cls):
        corpus = read_corpus()
        cls.index = build_index(corpus, ["policy_terms"])
        cls.edition = corpus.terms_edition()
        # 실제 검증(C validate_hits)은 EvidenceStore가 있어야 돈다. DB 대신 같은 행의 로컬 어댑터.
        store = LocalCorpusEvidenceStore(corpus)
        _evidence.configure_evidence_store(lambda: store)

    @classmethod
    def tearDownClass(cls):
        _evidence.configure_evidence_store(None)

    def setUp(self):
        configure_retrieval(index=self.index, default_terms_edition=self.edition)

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            judge=flag_judge,
        )

    def gather(self, topic, edition=None, fetch=fetch_by_parent):
        return asyncio.run(fetch_exclusion_clauses(topic, edition=edition, fetch=fetch))

    def test_only_article_23_for_every_topic(self):
        for topic, word, page in (
            (TOPICS[0], "무면허운전", 68),
            (TOPICS[1], "음주", 68),
            (TOPICS[2], "고의", 67),
        ):
            with self.subTest(topic=topic):
                hits = self.gather(topic)
                self.assertEqual([h["chunk"]["page_start"] for h in hits], [67, 68])
                self.assertEqual({h["chunk"]["parent_id"] for h in hits}, {ARTICLE_23})
                located = [h for h in hits if word in h["chunk"]["text"]]
                self.assertEqual([h["chunk"]["page_start"] for h in located], [page])

    def test_default_and_contract_edition(self):
        self.assertEqual(self.gather(TOPICS[0]), self.gather(TOPICS[0], edition="2026.11.11"))
        self.assertEqual(
            {h["chunk"]["document"]["edition"] for h in self.gather(TOPICS[0])}, {"2026.11.11"}
        )

    def test_other_edition_finds_no_clause(self):
        self.assertEqual(self.gather(TOPICS[0], edition="다른 판"), [])

    def test_tampered_chunk_fails_real_validation(self):
        real = self.gather(TOPICS[0])
        tampered = {**real[0], "chunk": {**real[0]["chunk"], "text": "바뀐 본문"}}
        with self.assertLogs("app.calculation.exclusions", "WARNING"):
            self.assertEqual(self.gather(TOPICS[0], fetch=Spy([tampered, real[1]])), [])

    def test_report_flow_with_real_clauses(self):
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps({"result": "unknown", "clause_ids": [ARTICLE_23], "detail": "불명"})

        api.configure(
            repo=api.InMemoryReportRepository(),
            fetch_clauses=fetch_by_parent,
            judge=llm_judge(llm),
        )
        facts = {**NO_FLAGS, "음주": True}
        asyncio.run(api.submit_judgment("h", fx.card(), fx.judgment()))
        report = asyncio.run(api.add_documents("h", "synthetic-case", [fx.estimate()], facts))
        self.assertEqual(report["calculation"]["status"], "not_payable")
        self.assertIn(ARTICLE_23, report["calculation"]["clause_ids"])
        self.assertTrue(all("음주운" in p and "제23조" in p for p in prompts))
        self.assertTrue(all("인용 법조문" not in p for p in prompts))  # 부록이 섞이지 않음


if __name__ == "__main__":
    unittest.main()
