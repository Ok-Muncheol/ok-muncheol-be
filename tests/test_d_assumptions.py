"""D: 기본 가정 — 견적서만 넣어도 보험금을 산정해 1차 판정 초안을 빠르게 낸다.

실행: py -3.12 -m unittest discover -s tests -t .
- 계약 서류 없음 → 자차 가입·보험기간 안·운전자 범위 안으로 가정, 전손 한도 검사 생략
- 자기부담금 → rules.py 기본 규칙(20%, 최저 20만·최고 50만)
- 면책 사실 미입력 → 확인 필요(unknown, 가정하지 않음, #204) / 모두 False → 담당자 확인
  / True → not_payable
- 확인 완료는 최신·draft_ready면 항상 된다. 청구서를 못 만들면 package=None + claim_blocked_reason
정답은 손으로 계산했다. 손해 1,000만 → 자기부담금 50만(20%=200만, 최고 50만), 보험금 950만, 상대
과실 70% → 665만.
"""

import asyncio
import unittest
from unittest.mock import patch

from app.api import reports as api
from app.calculation.rules import RuleSet
from tests import evidence_d
from tests import fixtures_d as fx

REVIEWED = RuleSet(version="reviewed-test")
NONE_CHECKED = {"무면허": False, "음주": False, "고의": False}  # FE '해당 없음' 체크
CONTRACT_A = (
    "계약 정보: 기본 가정(미입력) — 자차 가입·보험기간 안·운전자 범위 안, 전손 한도 검사 생략"
)
DEDUCTIBLE_A = "자기부담금: 기본 규칙 적용(20%, 최저 20만·최고 50만)"
# #204부터 면책 미입력은 기본 가정이 아니라 확인 필요(unknown) — 산정 사유에 항목마다 표시
EXCLUSION_UNKNOWN = "면책 확인 필요 — 사실 미입력"
NEEDS_ESTIMATE = "견적서 필요 — 견적서 추가 후 청구서 생성"


def section(report, no: int) -> dict:
    return next(s for s in report["sections"] if s["no"] == no)


class AssumptionTest(unittest.TestCase):
    actor, case = "handler-1", "synthetic-case"

    def setUp(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=REVIEWED,
            fetch_clauses=None,
            judge=api.flag_judge,
        )

    def tearDown(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            judge=api.flag_judge,
        )

    def run_(self, coro):
        return asyncio.run(coro)

    def submit(self, fault_A: int = 30, insured: str = "P1"):
        return self.run_(
            api.submit_judgment(self.actor, fx.card(insured), fx.judgment(fault_A=fault_A))
        )

    def add(self, docs, facts=None):
        return self.run_(api.add_documents(self.actor, self.case, docs, case_facts=facts))

    def confirm(self, version):
        return self.run_(
            api.confirm_report(self.actor, self.case, version, counterparty=fx.counterparty())
        )

    def checks(self, report) -> dict[str, dict]:
        return {c["key"]: c for c in report["calculation"]["checks"]}

    # ── 5-1. 견적서만 ──
    def test_estimate_only_calculates_with_assumptions_and_builds_claim(self):
        self.submit()
        v2 = self.add([fx.estimate()])  # 계약·면책 입력 없음
        calc = v2["calculation"]
        self.assertEqual(
            (calc["recognized_damage"], calc["deductible"], calc["insurance_payment"]),
            (10_000_000, 500_000, 9_500_000),
        )
        self.assertEqual(calc["assumptions"], [CONTRACT_A, DEDUCTIBLE_A])
        self.assertEqual(
            calc["status"], "needs_contract_review"
        )  # 가정·확인 필요가 있으면 확정 산정 아님(막지는 않음, 지급 불가 아님)
        for topic in ("무면허 운전 면책", "음주 운전 자기차량손해 면책", "고의 사고 면책"):
            self.assertIn(f"{topic}: {EXCLUSION_UNKNOWN}", calc["reasons"])

        eight = section(v2, 8)["body"]
        self.assertEqual(eight["기본 가정"], [CONTRACT_A, DEDUCTIBLE_A])
        rows = {x["항목"]: x for x in eight["산정 전 검증"]}
        for label in (
            "자기차량손해 가입",
            "사고일이 보험기간 안",
            "운전자 범위",
            "전손·보험가입금액 한도",
        ):
            self.assertEqual(rows[label]["결과"], "기본 가정(미입력)")
        self.assertEqual(rows["자기부담금"]["내용"], "기본 규칙 적용(20%, 최저 20만·최고 50만)")
        self.assertEqual(
            tuple(rows["음주 운전 자기차량손해 면책"][k] for k in ("결과", "내용")),
            ("확인 필요", EXCLUSION_UNKNOWN),
        )
        self.assertIn("기본 규칙 적용(20%, 최저 20만·최고 50만)", eight["계산식"]["자기부담금"])

        out = self.confirm(2)
        pkg = out["package"]
        self.assertIsNone(out["claim_blocked_reason"])
        self.assertEqual(out["report"]["approval_status"], "approved")
        self.assertEqual(
            (pkg["rows"][0]["insurance_payment"], pkg["amounts"]["planned"]), (9_500_000, 6_650_000)
        )
        self.assertEqual(pkg["assumptions"], [CONTRACT_A, DEDUCTIBLE_A])
        self.assertEqual(pkg["basis"]["기본 가정"], pkg["assumptions"])
        self.assertIn("기본 가정 2건", out["html"])
        self.assertNotIn(EXCLUSION_UNKNOWN, out["html"])  # 내부 판단은 청구서에 넣지 않는다
        self.assertIn(CONTRACT_A, out["html"])

    # ── 5-2. 면책 '해당 없음' 확인 ──
    def test_handler_none_checked_changes_label_not_amount(self):
        self.submit()
        v2 = self.add([fx.estimate()])
        v3 = self.add([], NONE_CHECKED)
        self.assertEqual(
            v3["versions"]["report_version"], 3
        )  # 면책 사실이 들어오면 재산정 → 새 버전
        for key in ("exclusion:무면허", "exclusion:음주", "exclusion:고의"):
            self.assertEqual(
                (self.checks(v2)[key]["result"], self.checks(v3)[key]["result"]),
                ("unknown", "pass"),
            )
            self.assertEqual(self.checks(v3)[key]["detail"], "담당자 확인 — 해당 없음")
        self.assertEqual(
            v3["calculation"]["insurance_payment"], v2["calculation"]["insurance_payment"]
        )
        self.assertEqual(v3["calculation"]["assumptions"], [CONTRACT_A, DEDUCTIBLE_A])

    # ── 5-3. 확인 완료 OK + 청구서 없음 + 사유 ──
    def test_confirm_ok_without_claim(self):
        cases = {
            "견적서 없음": (30, [], None, NEEDS_ESTIMATE),
            "100:0": (100, [fx.estimate()], None, "상대 과실 0% — 구상 대상 아님"),
            "음주=True": (
                30,
                [fx.estimate()],
                {**NONE_CHECKED, "음주": True},
                "지급 불가 — 구상할 보험금 없음",
            ),
            "자차 미가입": (
                30,
                [fx.contract(own_damage_joined=False), fx.estimate()],
                None,
                "지급 불가 — 구상할 보험금 없음",
            ),
            "보험금 0원": (
                30,
                [fx.estimate(150_000)],
                None,
                "자기부담금이 손해액 이상이라 지급액 0원 — 구상할 보험금 없음",
            ),
        }
        for name, (fault_A, docs, facts, reason) in cases.items():
            with self.subTest(name):
                api.configure(repo=api.InMemoryReportRepository())
                latest = self.submit(fault_A)
                if docs or facts:
                    latest = self.add(docs, facts)
                out = self.confirm(latest["versions"]["report_version"])
                self.assertEqual(
                    (out["report"]["approval_status"], out["report"]["approved_by"]),
                    ("approved", self.actor),
                )
                self.assertEqual(
                    (out["package"], out["html"], out["claim_blocked_reason"]), (None, None, reason)
                )
                self.assertEqual(fx.record(self.actor, self.case).packages, [])

    def test_drunk_reason_says_terms_not_linked(self):
        self.submit()
        v2 = self.add([fx.estimate()], {**NONE_CHECKED, "음주": True})
        self.assertEqual(
            (v2["calculation"]["status"], v2["calculation"]["insurance_payment"]),
            ("not_payable", None),
        )
        self.assertIn(
            "약관 조항 미연결 — 담당자 입력 기준", self.checks(v2)["exclusion:음주"]["detail"]
        )

    def test_no_estimate_is_always_null(self):
        v1 = self.submit()
        damage = v1["calculation"]["damage"]
        self.assertEqual(
            (damage["status"], damage["amount"], damage["reasons"]),
            ("needs_documents", None, ["견적서 없음 — 손해액 미확정"]),
        )
        v2 = self.add([fx.contract()], NONE_CHECKED)  # 계약·면책이 있어도 견적서가 없으면 금액 미정
        self.assertEqual(
            (v2["calculation"]["status"], v2["calculation"]["insurance_payment"]),
            ("needs_documents", None),
        )
        self.assertEqual(self.confirm(2)["claim_blocked_reasons"], [NEEDS_ESTIMATE])

    def test_null_amount_reason_names_cause(self):
        for name, insured, docs in (
            ("매핑 불명", "P9", [fx.estimate(party_id="P9")]),  # 가입자 P9가 도표 A/B에 없음
            ("가입금액 없음", "P1", [fx.contract(insured_amount=None), fx.estimate()]),
        ):
            with self.subTest(name):
                api.configure(repo=api.InMemoryReportRepository())
                self.submit(insured=insured)
                v2 = self.add(docs)
                self.assertIsNone(v2["calculation"]["insurance_payment"])
                out = self.confirm(2)
                self.assertIsNone(out["package"])
                self.assertTrue(
                    out["claim_blocked_reason"].startswith("보험금 산정 불가 — "),
                    out["claim_blocked_reason"],
                )

    def test_all_blocked_reasons_returned(self):
        self.submit()
        v2 = self.add([], {**NONE_CHECKED, "음주": True})  # 음주 + 견적서 없음
        out = self.confirm(v2["versions"]["report_version"])
        self.assertEqual(out["report"]["approval_status"], "approved")
        self.assertEqual(
            out["claim_blocked_reasons"], ["지급 불가 — 구상할 보험금 없음", NEEDS_ESTIMATE]
        )
        self.assertEqual(
            out["claim_blocked_reason"], "지급 불가 — 구상할 보험금 없음"
        )  # 첫 번째 값

    def test_blocked_reasons_include_counterparty_0_with_no_estimate(self):
        self.submit(fault_A=100)
        out = self.confirm(1)
        self.assertEqual(
            out["claim_blocked_reasons"], [NEEDS_ESTIMATE, "상대 과실 0% — 구상 대상 아님"]
        )

    def test_claim_built_has_empty_reasons(self):
        self.submit()
        self.add([fx.estimate()])
        out = self.confirm(2)
        self.assertEqual((out["claim_blocked_reasons"], out["claim_blocked_reason"]), ([], None))

    def test_case_facts_validated(self):
        self.submit()
        for bad in ({"무보험": True}, {"음주": "yes"}, {"고의": 1}, {"음주": False, "과속": False}):
            with self.subTest(bad=bad):
                with fx.raises_api(self, 422, "invalid_case_facts"):
                    self.add([fx.estimate()], bad)
        rec = fx.record(self.actor, self.case)
        self.assertEqual(
            (rec.documents, rec.case_facts, len(rec.reports)), ([], {}, 1)
        )  # 잘못된 입력은 아무것도 바꾸지 않음
        self.assertEqual(
            self.add([], {"음주": None, "고의": False})["versions"]["report_version"], 2
        )  # None·bool은 허용

    # ── 5-4. 견적서 없이 확인 완료 → 견적서 추가 → 새 버전 → 다시 확인 완료 → 청구서 ──
    def test_confirm_then_add_estimate_then_reconfirm(self):
        self.submit()
        first = self.confirm(1)
        self.assertEqual((first["package"], first["claim_blocked_reason"]), (None, NEEDS_ESTIMATE))
        v2 = self.add([fx.estimate()])
        self.assertEqual(
            (v2["versions"]["report_version"], v2["approval_status"]), (2, "draft")
        )  # 확인 풀림
        with fx.raises_api(self, 409, "version_conflict"):
            self.confirm(1)
        second = self.confirm(2)
        self.assertIsNone(second["claim_blocked_reason"])
        self.assertEqual(
            (
                second["package"]["package_version"],
                second["package"]["report_version"],
                second["package"]["amounts"]["claimed"],
            ),
            (1, 2, 6_650_000),
        )

    # ── 계약이 들어오면 그 값으로 재산정 ──
    def test_contract_replaces_assumption(self):
        self.submit()
        self.add([fx.estimate()])
        v3 = self.add([fx.contract(deductible_fixed=300_000)], NONE_CHECKED)
        calc = v3["calculation"]
        self.assertEqual(
            (calc["deductible"], calc["insurance_payment"], calc["assumptions"]),
            (300_000, 9_700_000, []),
        )
        self.assertEqual(calc["status"], "confirmed")

    # ── 지식베이스 연결 후: 사실이 없어 unknown → 막지 않고 표시만, 조항은 근거로 붙음 ──
    def test_unknown_without_facts_only_marks(self):
        async def fetch(*args, **kw):
            return [fx._hit("policy_terms", "synthetic-clause", "(합성) 면책 조항")]

        api.configure(fetch_clauses=fetch)  # 판정기는 기본(담당자 입력, LLM 없음)
        patcher = patch("app.calculation.exclusions.validate_hits", evidence_d.skip_validation)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.submit()
        v2 = self.add([fx.estimate()])  # 계약 없음
        ex = [c for c in v2["calculation"]["checks"] if c["key"].startswith("exclusion:")]
        self.assertEqual(
            {(c["result"], c["detail"]) for c in ex}, {("unknown", "면책 확인 필요 — 사실 미입력")}
        )
        self.assertTrue(all(c["clause_ids"] == ["synthetic-clause"] for c in ex))
        self.assertEqual(v2["calculation"]["status"], "needs_contract_review")  # 지급 불가 아님
        self.assertEqual(v2["calculation"]["insurance_payment"], 9_500_000)
        out = self.confirm(2)
        self.assertIsNotNone(out["package"])


if __name__ == "__main__":
    unittest.main()
