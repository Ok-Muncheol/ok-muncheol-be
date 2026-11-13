"""D(#204): 예시 규칙 자리 표시 ID(example:)는 보고서 근거 목록에 넣지 않는다(메모리 저장소).

예시 규칙이라는 표시는 그대로 남는다: 8번 '규칙 버전'·'근거 상태', 청구서 rule_notice(워터마크).
"""

import asyncio
import unittest

from app.api import reports as api
from app.calculation.rules import RuleSet
from tests import fixtures_d as fx

ACTOR, CASE = "handler-1", "synthetic-case"
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}
EXAMPLE_VERSION = RuleSet().version


def run(coro):
    return asyncio.run(coro)


def section(report, no):
    return next(s for s in report["sections"] if s["no"] == no)


def example_ids(ids):
    return [i for i in ids if str(i).startswith("example:") or str(i).startswith("example-")]


class ExampleRuleIdsTest(unittest.TestCase):
    def setUp(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)

    def tearDown(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)

    def check(self, report):
        calc = report["calculation"]
        self.assertTrue(calc["rule_version"].startswith("example-"))
        self.assertEqual(example_ids(section(report, 8)["evidence_ids"]), [])
        self.assertEqual(example_ids(section(report, 9)["evidence_ids"]), [])
        self.assertEqual(example_ids(calc["clause_ids"]), [])
        for c in calc["checks"]:
            self.assertEqual(example_ids(c["clause_ids"]), [], c["key"])
        # 예시 규칙이라는 표시는 그대로
        body8 = section(report, 8)["body"]
        self.assertEqual(body8["규칙 버전"], EXAMPLE_VERSION)
        self.assertTrue(body8["근거 상태"].startswith("예시 규칙 기준"))
        # 근거 목록에는 실제 서류가 남는다(견적서)
        self.assertIn(calc["damage"]["source_document_ids"][0], section(report, 8)["evidence_ids"])

    def test_with_contract(self):
        run(api.submit_judgment(ACTOR, fx.card(), fx.judgment()))
        report = run(
            api.add_documents(ACTOR, CASE, [fx.contract(), fx.estimate(), *fx.evidence()], NO_FLAGS)
        )
        self.check(report)
        keys = {c["key"] for c in report["calculation"]["checks"]}
        self.assertTrue(
            {"policy_period", "driver_scope"} <= keys
        )  # 예전에는 example: ID가 붙던 항목

    def test_without_contract_default_assumptions(self):
        run(api.submit_judgment(ACTOR, fx.card(), fx.judgment()))
        report = run(api.add_documents(ACTOR, CASE, [fx.estimate(), *fx.evidence()], NO_FLAGS))
        self.check(report)

    def test_total_loss_clause_id_dropped(self):
        run(api.submit_judgment(ACTOR, fx.card(), fx.judgment()))
        contract = fx.contract(vehicle_value=8_000_000)  # 손해액 1,000만 ≥ 차량가액 → 전손
        report = run(
            api.add_documents(ACTOR, CASE, [contract, fx.estimate(), *fx.evidence()], NO_FLAGS)
        )
        self.assertEqual(report["calculation"]["loss_type"], "total")
        self.check(report)

    def test_claim_watermark_kept(self):
        run(api.submit_judgment(ACTOR, fx.card(), fx.judgment()))
        report = run(
            api.add_documents(ACTOR, CASE, [fx.contract(), fx.estimate(), *fx.evidence()], NO_FLAGS)
        )
        pkg = run(
            api.confirm_report(
                ACTOR, CASE, report["versions"]["report_version"], counterparty=fx.counterparty()
            )
        )["package"]
        self.assertEqual(pkg["rule_notice"], "예시 규칙 기준 — 약관 검수 전 초안")
        self.assertEqual(pkg["rule_version"], EXAMPLE_VERSION)

    def test_reviewed_rule_version_stays_in_section_9(self):
        """검수된 규칙(example-가 아님)이면 규칙 버전은 예전처럼 9번 근거 목록에 남는다."""
        api.configure(rules=RuleSet(version="reviewed-test"))
        run(api.submit_judgment(ACTOR, fx.card(), fx.judgment()))
        report = run(
            api.add_documents(ACTOR, CASE, [fx.contract(), fx.estimate(), *fx.evidence()], NO_FLAGS)
        )
        self.assertIn("reviewed-test", section(report, 9)["evidence_ids"])


if __name__ == "__main__":
    unittest.main()
