"""D: 확정 흐름 — 판단 → 바로 산정(8·9번 자동) → 서류 추가 시 자동 재산정 → 담당자 [확인 완료] 한 번
→ 구상 청구 패키지.

실행: py -3.12 -m unittest discover -s tests -t .
정답은 손으로 계산했다. 손해 1,000만 → 자기부담금 50만(20%, 최고 50만), 보험금 950만, 상대 과실 70%
→ 예정액 665만.
"""

import asyncio
import unittest

from app.api import reports as api
from app.calculation.rules import RuleSet
from app.claims.package import EXAMPLE_RULE_NOTICE, NOT_RECOVERABLE
from app.reports.render import report_html
from tests import fixtures_d as fx

REVIEWED = RuleSet(version="reviewed-test")
COUNTERPARTY = fx.counterparty()
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}


def section(report, no: int) -> dict:
    return next(s for s in report["sections"] if s["no"] == no)


class ConfirmFlowTest(unittest.TestCase):
    actor, case = "handler-1", "synthetic-case"

    def setUp(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=REVIEWED, fetch_clauses=None)

    def tearDown(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)

    def run_(self, coro):
        return asyncio.run(coro)

    def submit(self, status: str = "draft_ready", fault_A: int = 30):
        return self.run_(
            api.submit_judgment(self.actor, fx.card(), fx.judgment(status, fault_A=fault_A))
        )

    def add(self, docs, facts=None):
        return self.run_(api.add_documents(self.actor, self.case, docs, case_facts=facts))

    def confirm(self, version, counterparty=COUNTERPARTY):
        return self.run_(
            api.confirm_report(self.actor, self.case, version, counterparty=counterparty)
        )

    def ready(self):
        """v1 판단(견적 없음) → v2 계약·견적·증빙."""
        self.submit()
        return self.add([fx.contract(), fx.estimate(), *fx.evidence()], NO_FLAGS)

    def test_judgment_calculates_immediately_with_null_amounts(self):
        v1 = self.submit()
        calc = v1["calculation"]
        self.assertEqual(
            (calc["status"], calc["insurance_payment"], calc["deductible"]),
            ("needs_documents", None, None),
        )
        self.assertEqual(section(v1, 8)["state"], "filled")
        self.assertIsNone(section(v1, 8)["body"]["자차 보험금"])
        self.assertEqual(
            (section(v1, 9)["state"], section(v1, 9)["empty_reason"]),
            ("empty", "견적서·산정 후 작성"),
        )
        self.assertEqual(v1["recovery"]["planned"], None)
        self.assertEqual(v1["approval_status"], "draft")

    def test_estimate_fills_sections_8_and_9_automatically(self):
        v2 = self.ready()
        self.assertEqual(
            (v2["versions"]["report_version"], v2["calculation"]["insurance_payment"]),
            (2, 9_500_000),
        )
        self.assertEqual(
            v2["recovery"],
            {
                "planned": 6_650_000,
                "claimed": 6_650_000,
                "recovered": None,
                "customer_deductible_review": 350_000,
            },
        )
        nine = section(v2, 9)["body"]
        self.assertEqual(
            (nine["구상 예정액(산정 보험금 × 상대 과실)"], nine["청구액(예정액 자동)"]),
            ("6,650,000원", "6,650,000원"),
        )

    def test_confirm_records_and_builds_one_row_package(self):
        self.ready()
        out = self.confirm(2)
        report, pkg = out["report"], out["package"]
        self.assertEqual(
            (report["approval_status"], report["approved_by"]), ("approved", self.actor)
        )
        self.assertIsNotNone(report["approved_at"])
        self.assertEqual(len(pkg["rows"]), 1)
        self.assertEqual(
            (
                pkg["rows"][0]["insurance_payment"],
                pkg["rows"][0]["planned_share"],
                pkg["rows"][0]["evidence_count"],
            ),
            (9_500_000, 6_650_000, 3),
        )
        self.assertEqual(
            (
                pkg["confirmed_by"],
                pkg["report_version"],
                pkg["package_version"],
                pkg["rule_notice"],
            ),
            (self.actor, 2, 1, None),
        )
        self.assertIn("확인 완료", out["html"])
        self.assertIn("확인 완료: handler-1", report_html(report))

    def test_confirm_without_estimate_confirms_without_package(self):
        self.submit()
        out = self.confirm(1)
        self.assertEqual(
            (out["package"], out["html"], out["claim_blocked_reason"]),
            (None, None, "견적서 필요 — 견적서 추가 후 청구서 생성"),
        )
        self.assertEqual(
            fx.record(self.actor, self.case).latest["approval_status"], "approved"
        )

    def test_not_ready_judgment_has_no_calculation_or_package(self):
        for status in ("needs_confirmation", "deferred"):
            with self.subTest(status=status):
                api.configure(repo=api.InMemoryReportRepository())
                v1 = self.submit(status)
                self.assertIsNone(v1["calculation"])
                self.assertIsNone(self.add([fx.contract(), fx.estimate()], NO_FLAGS))  # 서류만 저장
                self.assertEqual(len(fx.record(self.actor, self.case).reports), 1)
                # 확인 완료를 막는 두 경우 중 하나
                with fx.raises_api(self, 422, "confirm_blocked"):
                    self.confirm(1)

    def test_counterparty_fault_0_confirms_without_package(self):
        self.submit(fault_A=100)
        self.add([fx.contract(), fx.estimate()], NO_FLAGS)
        out = self.confirm(2)
        self.assertEqual(
            (out["report"]["approval_status"], out["package"], out["claim_blocked_reason"]),
            ("approved", None, NOT_RECOVERABLE),
        )

    def test_only_latest_version_can_be_confirmed(self):
        self.ready()
        with fx.raises_api(self, 409, "version_conflict"):
            self.confirm(1)

    def test_same_inputs_same_package(self):
        self.ready()
        first = self.confirm(2)
        again = self.confirm(2)
        self.assertIs(again["package"], first["package"])
        self.assertEqual(len(fx.record(self.actor, self.case).packages), 1)
        self.assertEqual(
            again["report"]["approved_at"], first["report"]["approved_at"]
        )  # 첫 확인 기록 유지

    def test_same_documents_do_not_create_version(self):
        v2 = self.ready()
        self.assertIs(self.add([fx.contract(), fx.estimate()]), v2)
        v3 = self.add([], {"고의": None})  # 담당자 입력 변경 → 검증 결과가 바뀌면 새 버전
        self.assertEqual(v3["versions"]["report_version"], 3)

    def test_changed_amount_after_confirm_needs_reconfirm(self):
        self.ready()
        first = self.confirm(2)["package"]
        revised = fx.estimate(12_000_000, "e-rev", received_at="2026-11-04T00:00:00+09:00")
        v3 = self.add([revised])
        self.assertEqual((v3["versions"]["report_version"], v3["approval_status"]), (3, "draft"))
        self.assertEqual(
            v3["judgment"]["versions"]["judgment_version"], 1
        )  # 판단은 다시 돌리지 않음
        self.assertEqual(
            (v3["calculation"]["insurance_payment"], v3["recovery"]["planned"]),
            (11_500_000, 8_050_000),
        )
        with fx.raises_api(self, 409, "version_conflict"):
            self.confirm(2)
        second = self.confirm(3)["package"]
        self.assertEqual(
            (first["package_version"], second["package_version"], second["amounts"]["claimed"]),
            (1, 2, 8_050_000),
        )

    def test_evidence_only_keeps_version_and_reconfirm_updates_package(self):
        v2 = self.ready()
        first = self.confirm(2)["package"]
        extra = {
            "kind": "evidence",
            "document_id": "synthetic-ev-extra",
            "doc_type": "photo",
            "storage_path": "synthetic/extra",
        }
        self.assertIs(self.add([extra]), v2)  # 금액 변화 없음 → 새 버전·재확인 불필요
        second = self.confirm(2)["package"]
        self.assertEqual(
            (first["rows"][0]["evidence_count"], second["rows"][0]["evidence_count"]), (3, 4)
        )
        self.assertEqual(second["package_version"], 2)

    def test_counterparty_optional(self):
        self.ready()
        out = self.confirm(2, counterparty=None)
        self.assertIsNone(out["package"]["counterparty"])
        self.assertIn("상대 보험사 미입력", out["html"])

    def test_example_rules_confirm_with_draft_notice(self):
        api.configure(rules=RuleSet())  # 예시 규칙(약관 검수 전)
        v2 = self.ready()
        self.assertEqual(v2["calculation"]["status"], "needs_contract_review")
        self.assertIn("예시 규칙(실무·약관 검수 전) — 확정 산정 아님", v2["calculation"]["reasons"])
        out = self.confirm(2)  # 막지 않음
        self.assertEqual(out["package"]["rule_notice"], "예시 규칙 기준 — 약관 검수 전 초안")
        self.assertEqual(out["package"]["rule_notice"], EXAMPLE_RULE_NOTICE)
        self.assertIn(EXAMPLE_RULE_NOTICE, out["html"])
        self.assertEqual(out["package"]["amounts"]["planned"], 6_650_000)


if __name__ == "__main__":
    unittest.main()
