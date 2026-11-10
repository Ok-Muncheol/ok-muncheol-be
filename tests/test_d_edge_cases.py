"""D: 손해액·보험금·보고서·구상 경계 사례 테스트 (외부 패키지·DB·모델 불필요).

실행: python3 -m unittest discover -s tests -t .
정답은 손으로 계산했다. 예시 규칙: 자기부담금 20%, 최저 20만·최고 50만.
"""

import asyncio
import unittest
from unittest.mock import patch

from app.api import reports as api
from app.calculation.damages import calculate_damages
from app.calculation.insurance import calculate_insurance
from app.calculation.rules import RuleSet
from app.claims.package import (
    NOT_RECOVERABLE,
    ClaimPackageBlocked,
    build_claim_package,
    compute_recovery,
)
from app.reports.assembler import assemble_review
from app.reports.render import package_html, to_json
from tests import evidence_d
from tests import fixtures_d as fx

REVIEWED = RuleSet(version="reviewed-test")
ZERO_PAYMENT_REASON = "자기부담금이 손해액 이상이라 지급액 0원 — 구상할 보험금 없음"
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}


def ready_report(fault_A: int = 30, card=None):
    return assemble_review(fx.judgment(fault_A=fault_A), card=card or fx.card())


def calc(docs, *, report=None, rules=REVIEWED):
    return calculate_insurance(
        report or ready_report(), docs, exclusion_checks=fx.NO_EXCLUSION, rules=rules
    )


class DamagesEdgeTest(unittest.TestCase):
    def test_newer_unverified_estimate_blocks_confirmed(self):
        d = calculate_damages(
            [
                fx.estimate(8_000_000, "e1"),
                fx.estimate(
                    12_000_000,
                    "e2",
                    read_by="ocr",
                    verification_status="unverified",
                    received_at="2026-10-30T00:00:00+09:00",
                ),
            ],
            party_id="P1",
        )
        self.assertEqual((d["status"], d["amount"]), ("needs_documents", None))
        self.assertIn("더 최신 견적", " ".join(d["reasons"]))
        self.assertEqual(set(d["source_document_ids"]), {"e1", "e2"})

    def test_older_unverified_estimate_does_not_block(self):
        d = calculate_damages(
            [
                fx.estimate(
                    12_000_000,
                    "e1",
                    read_by="ocr",
                    verification_status="unverified",
                    received_at="2026-10-20T00:00:00+09:00",
                ),
                fx.estimate(8_000_000, "e2"),
            ],
            party_id="P1",
        )
        self.assertEqual(
            (d["status"], d["amount"], d["source_document_ids"]), ("confirmed", 8_000_000, ["e2"])
        )

    def test_latest_compares_real_time_across_offsets(self):
        # e2 02:00Z = 11:00 KST → e1(10:00 KST)보다 나중
        d = calculate_damages(
            [
                fx.estimate(8_000_000, "e1", received_at="2026-10-25T10:00:00+09:00"),
                fx.estimate(9_000_000, "e2", received_at="2026-10-25T02:00:00+00:00"),
            ],
            party_id="P1",
        )
        self.assertEqual((d["amount"], d["source_document_ids"]), (9_000_000, ["e2"]))

    def test_other_party_estimate_ignored(self):
        d = calculate_damages([fx.estimate(party_id="P2")], party_id="P1")
        self.assertEqual(
            (d["status"], d["amount"], d["source_document_ids"]), ("needs_documents", None, [])
        )

    def test_manual_estimate_without_amount_is_not_confirmed(self):
        d = calculate_damages([fx.estimate(None)], party_id="P1")
        self.assertEqual((d["status"], d["amount"]), ("needs_documents", None))


class InsuranceEdgeTest(unittest.TestCase):
    def test_damage_above_insured_amount_is_capped(self):
        c = calc([fx.contract(insured_amount=20_000_000), fx.estimate(22_000_000)])
        self.assertEqual(
            (c["loss_type"], c["recognized_damage"], c["deductible"], c["insurance_payment"]),
            ("partial", 20_000_000, 500_000, 19_500_000),
        )

    def test_fixed_deductible_above_damage_pays_zero(self):
        c = calc([fx.contract(deductible_fixed=300_000), fx.estimate(200_000)])
        self.assertEqual(
            (c["status"], c["deductible"], c["insurance_payment"]), ("confirmed", 200_000, 0)
        )
        self.assertIn(ZERO_PAYMENT_REASON, c["reasons"])

    def test_fixed_deductible_equal_to_damage_pays_zero(self):
        c = calc([fx.contract(deductible_fixed=200_000), fx.estimate(200_000)])
        self.assertEqual(c["insurance_payment"], 0)
        self.assertIn(ZERO_PAYMENT_REASON, c["reasons"])

    def test_minimum_deductible_above_damage_pays_zero(self):
        c = calc([fx.contract(), fx.estimate(100_000)])  # 최저 20만 > 손해액 10만 → 손해액까지만
        self.assertEqual((c["deductible"], c["insurance_payment"]), (100_000, 0))
        self.assertIn(ZERO_PAYMENT_REASON, c["reasons"])

    def test_positive_payment_has_no_zero_reason(self):
        self.assertNotIn(ZERO_PAYMENT_REASON, calc([fx.contract(), fx.estimate()])["reasons"])

    def test_total_loss_boundary(self):
        self.assertEqual(calc([fx.contract(), fx.estimate(25_000_000)])["loss_type"], "total")
        self.assertEqual(calc([fx.contract(), fx.estimate(24_999_999)])["loss_type"], "partial")

    def test_without_vehicle_value_caps_at_insured_amount(self):
        c = calc([fx.contract(vehicle_value=None), fx.estimate(30_000_000)])
        self.assertEqual(
            (c["loss_type"], c["recognized_damage"], c["insurance_payment"]),
            ("partial", 25_000_000, 24_500_000),
        )

    def test_contract_rate_and_max_deductible(self):
        c = calc([fx.contract(deductible_rate=0.3, deductible_max=1_000_000), fx.estimate()])
        self.assertEqual((c["deductible"], c["insurance_payment"]), (1_000_000, 9_000_000))

    def test_fault_0_to_100_and_100_to_0(self):
        full = calc([fx.contract(), fx.estimate()], report=ready_report(fault_A=0))
        self.assertEqual((full["counterparty_fault"], full["insurance_payment"]), (100, 9_500_000))
        self.assertIn("= 9,500,000", full["formulas"]["구상 예정액(참고)"])
        none = calc([fx.contract(), fx.estimate()], report=ready_report(fault_A=100))
        self.assertEqual(
            (none["counterparty_fault"], none["insurance_payment"]), (0, 9_500_000)
        )  # 자차는 과실 무관 지급
        self.assertIn("= 0", none["formulas"]["구상 예정액(참고)"])


class BurdenSummaryTest(unittest.TestCase):
    """8번 부담 요약. 예시: 손해 1,000만 → 자기부담금 50만, 보험금 950만, 상대 과실 70%."""

    def summary(self, docs, fault_A: int = 30):
        base = ready_report(fault_A=fault_A)
        r = assemble_review(base["judgment"], calc(docs, report=base), previous=base)
        return next(s for s in r["sections"] if s["no"] == 8)["body"]["부담 요약"]

    def test_basic_burden_split(self):
        self.assertEqual(
            self.summary([fx.contract(), fx.estimate()]),
            {
                "고객 부담(자기부담금)": "500,000원",
                "보험사 지급(자차 보험금)": "9,500,000원",
                "상대방 부담 예상액(지급 전 예상치)": "6,650,000원",  # 9,500,000 × 70%
                "보험사 최종 부담 예상액": "2,850,000원",  # 9,500,000 − 6,650,000
            },
        )

    def test_no_estimate_keeps_null(self):
        self.assertEqual(
            self.summary([fx.contract()]),
            {
                "고객 부담(자기부담금)": None,
                "보험사 지급(자차 보험금)": None,
                "상대방 부담 예상액(지급 전 예상치)": None,
                "보험사 최종 부담 예상액": None,
            },
        )

    def test_counterparty_fault_0_not_recoverable(self):
        s = self.summary([fx.contract(), fx.estimate()], fault_A=100)
        self.assertEqual(
            (s["상대방 부담 예상액(지급 전 예상치)"], s["보험사 최종 부담 예상액"]),
            (NOT_RECOVERABLE, "9,500,000원"),
        )

    def test_floor_matches_share(self):
        s = self.summary(
            [fx.contract(deductible_fixed=0), fx.estimate(1_000_005)]
        )  # 1,000,005 × 70% = 700,003.5
        self.assertEqual(
            (s["상대방 부담 예상액(지급 전 예상치)"], s["보험사 최종 부담 예상액"]),
            ("700,003원", "300,002원"),
        )


class RecoveryEdgeTest(unittest.TestCase):
    """9번 자동 작성(산정 보험금 × 상대 과실)과 사건 1행 패키지."""

    def report_with_calc(self, docs, *, fault_A: int = 30, agreed=None, reason=None):
        base = ready_report(fault_A=fault_A)
        c = calc(docs, report=base)
        rec = compute_recovery(
            base, c, agreed_counterparty_fault=agreed, fault_change_reason=reason
        )
        return assemble_review(base["judgment"], c, recovery=rec, previous=base)

    def docs(self, *extra):
        return [fx.contract(), fx.estimate(), *fx.evidence(), *extra]

    def package(self, report, docs):
        return build_claim_package(
            report, documents=docs, counterparty=fx.counterparty(), confirmed_by="handler-1"
        )

    def test_counterparty_fault_0_blocks_package(self):
        docs = self.docs()
        r = self.report_with_calc(docs, fault_A=100)
        self.assertEqual(r["recovery"]["planned"], 0)
        with self.assertRaisesRegex(ClaimPackageBlocked, NOT_RECOVERABLE):
            self.package(r, docs)

    def test_agreed_fault_0_blocks_package(self):
        docs = self.docs()
        r = self.report_with_calc(docs, agreed=0, reason="협의")
        with self.assertRaisesRegex(ClaimPackageBlocked, "상대 과실 0% — 구상 대상 아님"):
            self.package(r, docs)

    def test_counterparty_fault_100_recovers_full_payment(self):
        docs = self.docs()
        r = self.report_with_calc(docs, fault_A=0)
        p = self.package(r, docs)
        self.assertEqual(
            (
                p["counterparty_fault"],
                p["amounts"]["planned"],
                r["recovery"]["customer_deductible_review"],
            ),
            (100, 9_500_000, 500_000),
        )  # 자기부담금 검토액은 9번(내부 참고)에서

    def test_planned_share_floors_below_won(self):
        docs = [fx.contract(deductible_fixed=0), fx.estimate(1_000_005), *fx.evidence()]
        r = self.report_with_calc(docs)
        self.assertEqual(
            (r["recovery"]["planned"], r["recovery"]["claimed"]), (700_003, 700_003)
        )  # 700,003.5 버림
        self.assertEqual(self.package(r, docs)["rows"][0]["planned_share"], 700_003)

    def test_package_has_claim_amounts_only(self):
        docs = self.docs()
        r = self.report_with_calc(docs)
        p = self.package(r, docs)
        self.assertEqual(
            p["amounts"], {"planned": 6_650_000, "claimed": 6_650_000, "recovered": None}
        )
        self.assertNotIn("customer_deductible_review", to_json(p))
        self.assertNotIn(
            "고객 자기부담금 검토액", package_html(p)
        )  # 기본 가정 표시("자기부담금: 기본 규칙 적용")는 허용
        nine = next(s for s in r["sections"] if s["no"] == 9)
        self.assertEqual(
            nine["body"]["고객 자기부담금 검토액"], "350,000원"
        )  # 보고서 9번에는 내부 참고로 유지
        self.assertEqual(nine["body"]["청구액(예정액 자동)"], "6,650,000원")

    def test_no_evidence_warns_but_builds(self):
        docs = [fx.contract(), fx.estimate()]
        p = self.package(self.report_with_calc(docs), docs)
        self.assertEqual(
            (p["missing_evidence"], p["rows"][0]["evidence_count"]), (["synthetic-case-C1"], 0)
        )
        self.assertIn("증빙 없음(경고)", package_html(p))

    def test_no_estimate_leaves_section_9_empty(self):
        r = self.report_with_calc([fx.contract()])
        self.assertIsNone(r["recovery"]["planned"])
        nine = next(s for s in r["sections"] if s["no"] == 9)
        self.assertEqual((nine["state"], nine["empty_reason"]), ("empty", "견적서·산정 후 작성"))


class ApiEdgeFlowTest(unittest.TestCase):
    """조항 조회 미연결(fetch_clauses=None) → flag_judge가 담당자 입력(case_facts)으로 면책
    판정.
    협의 과실은 set_agreed_fault로 입력·해제하고 9번·패키지에 반영된다."""

    actor, case = "handler-1", "synthetic-case"

    def setUp(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=REVIEWED, fetch_clauses=None)

    def tearDown(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)

    def run_(self, coro):
        return asyncio.run(coro)

    def calculate(self, facts):
        """v1 판단(견적 없음) → 서류·담당자 입력 사실 → v2 자동 재산정."""
        a, cid = self.actor, self.case
        self.run_(api.submit_judgment(a, fx.card(), fx.judgment()))
        v2 = self.run_(
            api.add_documents(
                a, cid, [fx.contract(), fx.estimate(), *fx.evidence()], case_facts=facts
            )
        )
        return v2["calculation"]

    def ready(self):
        self.calculate(NO_FLAGS)
        return self.run_(api.get_report(self.actor, self.case, 2))

    def agree(self, fault=None, reason=None, clear=False):
        return self.run_(
            api.set_agreed_fault(
                self.actor,
                self.case,
                agreed_counterparty_fault=fault,
                fault_change_reason=reason,
                clear=clear,
            )
        )

    def confirm(self, version):
        return self.run_(
            api.confirm_report(self.actor, self.case, version, counterparty=fx.counterparty())
        )

    def exclusion_checks(self, calc):
        return {c["key"]: c for c in calc["checks"] if c["key"].startswith("exclusion:")}

    def test_exclusions_judged_by_handler_input_without_search(self):
        calc = self.calculate(NO_FLAGS)
        checks = self.exclusion_checks(calc)
        self.assertEqual(set(checks), {"exclusion:무면허", "exclusion:음주", "exclusion:고의"})
        for c in checks.values():  # 셋 다 False = 담당자가 '해당 없음' 확인
            self.assertEqual(
                (c["result"], c["decided_by"], c["clause_ids"], c["detail"]),
                ("pass", "handler", [], "담당자 확인 — 해당 없음"),
            )
        self.assertEqual(calc["status"], "confirmed")
        eight = next(
            s
            for s in self.run_(api.get_report(self.actor, self.case, 2))["sections"]
            if s["no"] == 8
        )
        self.assertTrue(
            all(
                x["내용"] == "담당자 확인 — 해당 없음"
                for x in eight["body"]["산정 전 검증"]
                if x["항목"] in {c["label"] for c in checks.values()}
            )
        )

    def test_missing_handler_input_is_assumed_none(self):
        calc = self.calculate({"무면허": False, "음주": False})  # 고의 미입력 → 해당 없음으로 가정
        checks = self.exclusion_checks(calc)
        self.assertEqual(
            (checks["exclusion:고의"]["result"], checks["exclusion:고의"]["detail"]),
            ("assumed", "면책 사실 미입력 — 해당 없음으로 가정"),
        )
        self.assertEqual(calc["status"], "needs_contract_review")  # 가정이 있으면 확정 산정 아님
        self.assertIn("면책(고의): 면책 사실 미입력 — 해당 없음으로 가정", calc["assumptions"])
        self.assertEqual(calc["insurance_payment"], 9_500_000)  # 금액은 계산

    def test_no_handler_input_assumes_all_none(self):
        calc = self.calculate({})
        self.assertEqual({c["result"] for c in self.exclusion_checks(calc).values()}, {"assumed"})
        self.assertIn("면책: 면책 사실 미입력 — 해당 없음으로 가정", calc["assumptions"])
        self.assertEqual(calc["status"], "needs_contract_review")

    def test_handler_flag_true_is_not_payable(self):
        calc = self.calculate({**NO_FLAGS, "음주": True})
        check = self.exclusion_checks(calc)["exclusion:음주"]
        self.assertEqual(check["result"], "fail")
        self.assertIn("약관 조항 미연결 — 담당자 입력 기준", check["detail"])
        self.assertEqual((calc["status"], calc["insurance_payment"]), ("not_payable", None))
        out = self.confirm(2)  # 확인 완료는 되고 청구서만 없음
        self.assertEqual((out["report"]["approval_status"], out["package"]), ("approved", None))
        self.assertEqual(out["claim_blocked_reason"], "지급 불가 — 구상할 보험금 없음")

    def test_unimplemented_fetch_falls_back_to_handler_input(self):
        async def not_ready(*args, **kw):
            raise NotImplementedError

        api.configure(fetch_clauses=not_ready)
        calc = self.calculate(NO_FLAGS)
        self.assertEqual(
            {c["decided_by"] for c in self.exclusion_checks(calc).values()}, {"handler"}
        )

    def test_fetch_gets_contract_edition(self):
        seen = []

        async def fetch(corpus, parents, *, terms_edition=None):
            seen.append(terms_edition)
            return []

        api.configure(fetch_clauses=fetch)
        self.calculate(NO_FLAGS)
        self.assertEqual(seen[0], None)  # v1: 계약 전 → 기본 판 규칙에 맡김
        self.assertEqual(seen[-1], fx.contract()["terms_edition"])  # v2: 계약 값

    def test_connected_fetch_uses_configured_judge(self):
        async def fetch(*args, **kw):
            return [fx._hit("policy_terms", "synthetic-clause", "(합성) 면책 조항")]

        api.configure(fetch_clauses=fetch)
        with patch("app.calculation.exclusions.validate_hits", evidence_d.skip_validation):
            checks = self.exclusion_checks(self.calculate(NO_FLAGS))
        self.assertEqual(
            {c["decided_by"] for c in checks.values()}, {"code"}
        )  # flag_judge 기본 + 조항 ID 유지
        self.assertEqual(checks["exclusion:음주"]["clause_ids"], ["synthetic-clause"])

    def test_agreed_fault_used_by_package_and_kept_on_recalculation(self):
        self.ready()
        v3 = self.agree(60, "상대 보험사 협의")
        self.assertEqual((v3["versions"]["report_version"], v3["approval_status"]), (3, "draft"))
        self.assertEqual(
            (v3["recovery"]["planned"], v3["recovery"]["claimed"]), (5_700_000, 5_700_000)
        )
        self.assertEqual(
            (v3["recovery"]["agreed_counterparty_fault"], v3["recovery"]["fault_change_reason"]),
            (60, "상대 보험사 협의"),
        )
        nine = next(s for s in v3["sections"] if s["no"] == 9)
        self.assertEqual(nine["body"]["협의 과실 / 변경 이유"], "상대 과실 60% / 상대 보험사 협의")
        pkg = self.confirm(3)["package"]
        self.assertEqual(
            (pkg["counterparty_fault"], pkg["amounts"]["planned"], pkg["fault_change_reason"]),
            (60, 5_700_000, "상대 보험사 협의"),
        )
        revised = fx.estimate(12_000_000, "e-rev", received_at="2026-11-04T00:00:00+09:00")
        v4 = self.run_(
            api.add_documents(self.actor, self.case, [revised])
        )  # 재산정에도 협의 과실 이어 씀
        self.assertEqual(
            (v4["recovery"]["agreed_counterparty_fault"], v4["recovery"]["planned"]),
            (60, 6_900_000),
        )

    def test_clear_agreed_fault_reverts_to_judgment(self):
        self.ready()
        self.agree(60, "상대 보험사 협의")
        v4 = self.agree(reason="협의 철회", clear=True)
        self.assertEqual(
            (
                v4["recovery"]["agreed_counterparty_fault"],
                v4["recovery"]["fault_change_reason"],
                v4["recovery"]["planned"],
            ),
            (None, "협의 철회", 6_650_000),
        )  # 판단 과실 70% 기준
        nine = next(s for s in v4["sections"] if s["no"] == 9)
        self.assertEqual(
            nine["body"]["협의 과실 / 변경 이유"], "없음(판단 과실비율 기준) / 협의 해제: 협의 철회"
        )
        pkg = self.confirm(4)["package"]
        self.assertEqual(
            (pkg["counterparty_fault"], pkg["amounts"]["planned"], pkg["fault_change_reason"]),
            (70, 6_650_000, None),
        )

    def test_clear_agreed_fault_survives_estimate_recompute(self):
        self.ready()
        self.agree(60, "협의")
        self.agree(reason="협의 철회", clear=True)
        revised = fx.estimate(12_000_000, "e-rev", received_at="2026-11-04T00:00:00+09:00")
        v5 = self.run_(api.add_documents(self.actor, self.case, [revised]))
        self.assertEqual(
            (
                v5["recovery"]["agreed_counterparty_fault"],
                v5["recovery"]["fault_change_reason"],
                v5["recovery"]["planned"],
            ),
            (None, "협의 철회", 8_050_000),
        )  # 11,500,000 × 70%

    def test_agreed_fault_validation(self):
        self.ready()
        with fx.raises_api(self, 422, "invalid_agreed_fault", "이유"):
            self.agree(60)
        with fx.raises_api(self, 422, "invalid_agreed_fault", "0~100"):
            self.agree(120, "x")
        self.agree(60, "협의")
        with fx.raises_api(self, 422, "invalid_agreed_fault", "이유"):
            self.agree(clear=True)
        with fx.raises_api(self, 422, "invalid_agreed_fault", "함께 할 수 없습니다"):
            self.agree(50, "x", clear=True)

    def test_clear_without_agreement_rejected(self):
        self.ready()
        with fx.raises_api(self, 422, "invalid_agreed_fault", "되돌릴 협의 과실이 없습니다"):
            self.agree(reason="x", clear=True)

    def test_no_agreement_shown_in_section_9(self):
        v2 = self.ready()
        nine = next(s for s in v2["sections"] if s["no"] == 9)
        self.assertEqual(nine["body"]["협의 과실 / 변경 이유"], "없음(판단 과실비율 기준)")
        self.assertNotIn("agreed_counterparty_fault", v2["recovery"])

    def test_agreed_fault_0_confirms_without_package(self):
        self.ready()
        v3 = self.agree(0, "협의 결과 상대 무과실")
        self.assertEqual(v3["recovery"]["planned"], 0)
        out = self.confirm(3)
        self.assertEqual((out["package"], out["claim_blocked_reason"]), (None, NOT_RECOVERABLE))
        self.assertEqual(v3["approval_status"], "approved")  # 확인 기록은 남김
        self.assertEqual(api.services.repo.get(self.actor, self.case).packages, [])

    def test_regenerate_package_after_agreement(self):
        self.ready()
        first = self.confirm(2)["package"]
        self.agree(60, "협의")
        with fx.raises_api(self, 409, "version_conflict"):  # 예전 확인본으로는 불가
            self.confirm(2)
        second = self.confirm(3)["package"]
        self.assertEqual((first["package_version"], first["counterparty_fault"]), (1, 70))
        self.assertEqual(
            (
                second["package_version"],
                second["counterparty_fault"],
                second["amounts"]["planned"],
                second["report_version"],
            ),
            (2, 60, 5_700_000, 3),
        )
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertIs(self.confirm(3)["package"], second)  # 같은 입력 재확인 → 기존 패키지


if __name__ == "__main__":
    unittest.main()
