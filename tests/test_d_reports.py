"""D: 손해액·보험금·보고서·구상 결정적 로직 테스트 (외부 패키지·DB·모델 불필요).

실행: python3 -m unittest discover -s tests -t .
정답은 손으로 계산했다. 예시 규칙: 자기부담금 20%, 최저 20만·최고 50만.
"""

import asyncio
import unittest

from app.api import reports as api
from app.calculation.damages import calculate_damages
from app.calculation.exclusions import llm_judge
from app.calculation.insurance import CalculationBlocked, calculate_insurance
from app.calculation.rules import RuleSet
from app.claims.package import (
    EXAMPLE_RULE_NOTICE,
    ClaimPackageBlocked,
    build_claim_package,
    compute_recovery,
)
from app.reports.assembler import ReportInputMismatch, assemble_review, missing_numeric_evidence
from app.reports.render import package_html, report_html, report_markdown, to_json
from tests import fixtures_d as fx

REVIEWED = RuleSet(version="reviewed-test")  # '검수 완료'를 가정한 규칙 (값은 예시와 같음)


def ready_report(judgment=None, card=None):
    """판단 결과로 만든 보고서(승인 단계 없음)."""
    return assemble_review(judgment or fx.judgment(), card=card or fx.card())


class DamagesTest(unittest.TestCase):
    def test_latest_confirmed_estimate(self):
        d = calculate_damages(
            [
                fx.estimate(8_000_000, "e1"),
                fx.estimate(10_000_000, "e2", received_at="2026-10-28T09:00:00+09:00"),
            ],
            party_id="P1",
        )
        self.assertEqual(
            (d["status"], d["amount"], d["source_document_ids"]), ("confirmed", 10_000_000, ["e2"])
        )

    def test_unverified_ocr_is_null(self):
        d = calculate_damages(
            [fx.estimate(read_by="ocr", verification_status="unverified")], party_id="P1"
        )
        self.assertEqual((d["status"], d["amount"]), ("needs_documents", None))

    def test_missing_is_null_not_zero(self):
        self.assertIsNone(calculate_damages([], party_id="P1")["amount"])


class InsuranceTest(unittest.TestCase):
    def calc(self, docs, rules=REVIEWED, report=None, exclusions=fx.NO_EXCLUSION):
        return calculate_insurance(
            report or ready_report(), docs, exclusion_checks=exclusions, rules=rules
        )

    def test_basic_own_damage(self):
        c = self.calc([fx.contract(), fx.estimate()])
        self.assertEqual(c["status"], "confirmed")
        self.assertEqual(
            (
                c["recognized_damage"],
                c["deductible"],
                c["insurance_payment"],
                c["counterparty_fault"],
            ),
            (10_000_000, 500_000, 9_500_000, 70),
        )

    def test_example_rules_never_confirmed(self):
        c = self.calc([fx.contract(), fx.estimate()], rules=RuleSet())
        self.assertEqual(c["status"], "needs_contract_review")
        self.assertIn("예시 규칙", " ".join(c["reasons"]))

    def test_min_and_fixed_deductible(self):
        self.assertEqual(self.calc([fx.contract(), fx.estimate(600_000)])["deductible"], 200_000)
        self.assertEqual(
            self.calc([fx.contract(deductible_fixed=300_000), fx.estimate()])["insurance_payment"],
            9_700_000,
        )

    def test_total_loss(self):
        c = self.calc([fx.contract(), fx.estimate(30_000_000)])
        self.assertEqual(
            (c["loss_type"], c["recognized_damage"], c["insurance_payment"]),
            ("total", 25_000_000, 24_500_000),
        )

    def test_insured_is_chart_B(self):
        c = self.calc(
            [fx.contract(), fx.estimate(party_id="P2")],
            report=ready_report(card=fx.card(insured="P2")),
        )
        self.assertEqual(c["counterparty_fault"], 30)

    def test_not_payable_and_unknowns(self):
        self.assertEqual(
            self.calc([fx.contract(own_damage_joined=False), fx.estimate()])["status"],
            "not_payable",
        )
        self.assertEqual(
            self.calc([fx.contract(driver_in_scope=False), fx.estimate()])["status"], "not_payable"
        )
        self.assertEqual(
            self.calc([fx.contract(period_end="2026-10-01"), fx.estimate()])["status"],
            "not_payable",
        )
        drunk = [{**fx.NO_EXCLUSION[1], "result": "fail"}]
        self.assertEqual(
            self.calc([fx.contract(), fx.estimate()], exclusions=drunk)["status"], "not_payable"
        )
        self.assertEqual(
            self.calc([fx.contract(driver_in_scope=None), fx.estimate()])["status"],
            "needs_contract_review",
        )
        self.assertEqual(
            self.calc([fx.contract(), fx.estimate()], exclusions=None)["status"],
            "needs_contract_review",
        )
        nodoc = self.calc([fx.contract()])
        self.assertEqual((nodoc["status"], nodoc["insurance_payment"]), ("needs_documents", None))

    def test_calculates_without_approval_but_not_without_fault(self):
        draft = assemble_review(fx.judgment(), card=fx.card())  # 승인 단계 없이 바로 산정
        self.assertEqual(
            calculate_insurance(draft, [fx.contract(), fx.estimate()])["insurance_payment"],
            9_500_000,
        )
        with self.assertRaises(CalculationBlocked):
            calculate_insurance(
                assemble_review(fx.judgment("needs_confirmation"), card=fx.card()), []
            )

    def test_llm_judge_drops_unknown_clause_ids(self):
        hit = fx._hit("policy_terms", "synthetic-clause-drunk", "(합성) 음주 운전 면책")
        judge = llm_judge(lambda p: '{"result":"fail","clause_ids":["made-up"],"detail":"x"}')
        self.assertEqual(judge("음주 운전", [hit], {})["result"], "unknown")


class ReportTest(unittest.TestCase):
    def test_sections_1_to_7_and_empty_8_9(self):
        r = assemble_review(fx.judgment(), card=fx.card())
        self.assertEqual([s["no"] for s in r["sections"]], list(range(1, 10)))
        self.assertEqual([s["state"] for s in r["sections"]][7:], ["empty", "empty"])
        self.assertEqual(missing_numeric_evidence(r), [])
        self.assertEqual(r["versions"]["report_version"], 1)

    def test_numbers_blank_when_not_ready(self):
        r = assemble_review(fx.judgment("needs_confirmation"), card=fx.card())
        states = {s["no"]: s["state"] for s in r["sections"]}
        self.assertEqual((states[4], states[6], states[8]), ("empty", "empty", "empty"))
        five = next(s for s in r["sections"] if s["no"] == 5)
        self.assertEqual(five["body"]["목록"][0]["가감"], "—")

    def test_handler_edit_marked(self):
        two = next(
            s for s in assemble_review(fx.judgment(), card=fx.card())["sections"] if s["no"] == 2
        )
        edited = {f["칸"]: f["담당자 수정"] for f in two["body"]["사실 카드"]}
        self.assertTrue(edited["signal_A"])

    def test_version_mismatch_rejected(self):
        with self.assertRaises(ReportInputMismatch):
            assemble_review(fx.judgment(), card={**fx.card(), "version": 3})

    def test_render(self):
        base = ready_report()
        c = calculate_insurance(
            base, [fx.contract(), fx.estimate()], exclusion_checks=fx.NO_EXCLUSION, rules=REVIEWED
        )
        r = assemble_review(base["judgment"], c, previous=base)
        self.assertIn("9,500,000원", report_html(r))
        self.assertIn("## 8. 손해액·보험금", report_markdown(r))


class ClaimTest(unittest.TestCase):
    def report_with_calc(self, rules=REVIEWED, docs=None):
        base = ready_report()
        docs = docs if docs is not None else [fx.contract(), fx.estimate(), *fx.evidence()]
        c = calculate_insurance(base, docs, exclusion_checks=fx.NO_EXCLUSION, rules=rules)
        return assemble_review(
            base["judgment"], c, recovery=compute_recovery(base, c), previous=base
        ), docs

    def package(self, report, docs):
        return build_claim_package(
            report,
            documents=docs,
            counterparty=fx.counterparty(),
            confirmed_by="handler-1",
            now="2026-11-06T10:00:00+09:00",
        )

    def test_recovery_auto_filled(self):
        r, _ = self.report_with_calc()
        # 9,500,000 × 70% = 6,650,000 / 자기부담금 500,000 × 70% = 350,000. 청구액은 예정액으로 자동
        self.assertEqual(
            r["recovery"],
            {
                "planned": 6_650_000,
                "claimed": 6_650_000,
                "recovered": None,
                "customer_deductible_review": 350_000,
            },
        )

    def test_package_one_row_per_case(self):
        r, docs = self.report_with_calc()
        p = self.package(r, docs)
        self.assertEqual(len(p["rows"]), 1)
        row = p["rows"][0]
        self.assertEqual(
            (
                row["claim_no"],
                row["insured_party_id"],
                row["insurance_payment"],
                row["counterparty_fault"],
                row["planned_share"],
                row["evidence_count"],
            ),
            ("synthetic-case-C1", "P1", 9_500_000, 70, 6_650_000, 3),
        )
        self.assertEqual(
            (p["confirmed_by"], p["missing_evidence"], p["rule_notice"]), ("handler-1", [], None)
        )
        self.assertIn("구상 요약표", package_html(p))
        self.assertEqual(self.package(r, docs)["idempotency_key"], p["idempotency_key"])

    def test_package_blocks(self):
        r, docs = self.report_with_calc(docs=[fx.contract()])
        with self.assertRaisesRegex(ClaimPackageBlocked, "견적서 필요"):  # 금액 null
            self.package(r, docs)
        r, docs = self.report_with_calc()
        bad = {**r, "recovery": {**r["recovery"], "agreed_counterparty_fault": 60}}
        with self.assertRaisesRegex(ClaimPackageBlocked, "이유"):  # 협의 변경 이유 없음
            self.package(bad, docs)
        pending = assemble_review(fx.judgment("needs_confirmation"), card=fx.card())
        with self.assertRaises(ClaimPackageBlocked):  # 추가 확인 → 산정·패키지 없음
            self.package(pending, docs)

    def test_example_rules_still_make_marked_package(self):
        r, docs = self.report_with_calc(rules=RuleSet())
        self.assertEqual(
            r["calculation"]["status"], "needs_contract_review"
        )  # 예시 규칙 → 확정 아님
        p = self.package(r, docs)  # 그래도 청구서 초안은 나온다
        self.assertEqual(p["rule_notice"], EXAMPLE_RULE_NOTICE)
        self.assertEqual(EXAMPLE_RULE_NOTICE, "예시 규칙 기준 — 약관 검수 전 초안")
        self.assertIn(EXAMPLE_RULE_NOTICE, package_html(p))
        self.assertIn(EXAMPLE_RULE_NOTICE, to_json(p))
        self.assertEqual(p["amounts"]["planned"], 6_650_000)


class ApiFlowTest(unittest.TestCase):
    def tearDown(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)

    def test_full_flow(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=REVIEWED, fetch_clauses=None)
        run = asyncio.run
        actor, cid = "handler-1", "synthetic-case"
        v1 = run(api.submit_judgment(actor, fx.card(), fx.judgment()))
        self.assertEqual(
            v1["calculation"]["status"], "needs_documents"
        )  # 승인 없이 바로 산정, 견적서 없음
        self.assertIsNone(v1["calculation"]["insurance_payment"])  # 미확정은 0이 아니라 null
        v2 = run(
            api.add_documents(
                actor,
                cid,
                [fx.contract()],
                case_facts={"무면허": False, "음주": False, "고의": False},
            )
        )
        self.assertEqual(v2["versions"]["report_version"], 2)
        self.assertEqual(
            run(api.confirm_report(actor, cid, 2))["claim_blocked_reason"],
            "견적서 필요 — 견적서 추가 후 청구서 생성",
        )  # 확인 완료는 되고 청구서만 없음
        v3 = run(api.add_documents(actor, cid, [fx.estimate()]))
        self.assertEqual(
            (v3["versions"]["report_version"], v3["judgment"]["versions"]["judgment_version"]),
            (3, 1),
        )
        self.assertEqual(v3["calculation"]["insurance_payment"], 9_500_000)
        self.assertEqual(
            v3["calculation"]["status"], "confirmed"
        )  # 약관 검색 미연결 → 담당자 입력(case_facts)으로 면책 판정
        self.assertEqual(
            {
                c["decided_by"]
                for c in v3["calculation"]["checks"]
                if c["key"].startswith("exclusion:")
            },
            {"handler"},
        )
        self.assertIs(
            run(api.add_documents(actor, cid, [fx.estimate()])), v3
        )  # 같은 서류 재전송 → 새 버전 없음
        with fx.raises_api(self, 409, "version_conflict"):
            run(api.confirm_report(actor, cid, 2))
        out = run(api.confirm_report(actor, cid, 3))
        self.assertEqual(
            (out["report"]["approval_status"], out["report"]["approved_by"]), ("approved", actor)
        )
        self.assertEqual(out["package"]["amounts"]["planned"], 6_650_000)
        with fx.raises_api(self, 403, "forbidden"):
            run(api.get_report("someone-else", cid, 1))


if __name__ == "__main__":
    unittest.main()
