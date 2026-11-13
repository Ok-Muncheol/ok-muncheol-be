"""C: 대물·대인 1차 판정을 D 정답 예시(tests/fixtures/d_injury_property_cases.json)로 확인한다.

숫자 자료는 app.ingestion.injury_property_values.build()의 레코드(PDF 대조 검수, 52개)를 쓴다.
원문 텍스트가 필요 없어 공개 CI에서도 돈다.
"""

import json
import unittest
from pathlib import Path

from app.calculation.liability import ScheduleValues, calculate_injury, calculate_property
from app.ingestion import injury_property_values

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "d_injury_property_cases.json").read_text("utf-8")
)
VALUES = ScheduleValues(injury_property_values.build({"document_id": CASES["document_id"]}))
DATE = CASES["accident_date"]
STATUS = {"계산": "draft", "배상책임 확인": "needs_review"}
# 정답 예시의 대물 "확인 필요" 이름 → 계산 결과 flags[].key (대차료·휴차료는 한 표시)
PROPERTY_REVIEW_KEYS = {
    "전부손해": "total_loss",
    "시세하락손해": "depreciation",
    "대차료": "rental",
    "휴차료": "rental",
}


def estimate(amount):
    return {
        "kind": "repair_estimate",
        "document_id": "est-1",
        "party_id": "cp",
        "amount": amount,
        "read_by": "pdf_extract",
        "verification_status": "unverified",
        "received_at": "2026-11-12T10:00:00+09:00",
        "storage_path": None,
    }


def cases(kind):
    return [c for c in CASES["cases"] if c["kind"] == kind]


class PropertyCasesTest(unittest.TestCase):
    def test_property_cases(self):
        for case in cases("property"):
            with self.subTest(case["case_id"]):
                i, e = case["input"], case["expected"]
                r = calculate_property(
                    [estimate(i["repair_cost_krw"])],
                    {"counterparty_id": "cp", "property_limit": i["property_damage_limit_krw"]},
                    i["our_fault_percent"],
                    accident_date=DATE,
                    values=VALUES,
                )
                self.assertEqual(r["status"], STATUS[e["status"]])
                self.assertEqual(r["amount"], e["amount_krw"])
                self.assertEqual(r["limit_applied"], e["capped_by_limit"])
                want = {PROPERTY_REVIEW_KEYS[n] for n in e["needs_review"]}
                self.assertEqual({f["key"] for f in r["flags"]}, want)
                cited = {x for f in r["flags"] for x in f["clause_ids"]}
                self.assertLessEqual(set(case["basis_record_ids"]), cited)


class InjuryCasesTest(unittest.TestCase):
    def test_injury_cases(self):
        for case in cases("injury"):
            i, e = case["input"], case["expected"]
            results = []
            for p, ep in zip(i["persons"], e["persons"], strict=True):
                with self.subTest(case["case_id"], person=p["person_id"]):
                    r = calculate_injury(
                        {
                            "person_id": p["person_id"],
                            "grade": p["grade"],
                            "treatment_cost": p["treatment_cost_krw"],
                        },
                        i["our_fault_percent"],
                        accident_date=DATE,
                        values=VALUES,
                    )
                    results.append(r)
                    self.assertEqual(r["status"], STATUS[e["status"]])
                    got = (
                        r["consolation"],
                        r["damage"],
                        r["after_offset"],
                        r["treatment_guarantee"],
                        r["bodily_1_limit"],
                        r["bodily_1"],
                        r["bodily_2"],
                    )
                    want = (
                        ep["consolation_krw"],
                        ep["before_offset_krw"],
                        ep["after_offset_krw"],
                        ep["treatment_floor_applied"],
                        ep["coverage_i_limit_krw"],
                        ep["coverage_i_krw"],
                        ep["coverage_ii_krw"],
                    )
                    self.assertEqual(got, want)
                    # 이 사람 급수의 근거 레코드와 별표3 과실상계 규칙이 근거에 있어야 한다
                    grade = f":{p['grade']}급"
                    mine = {
                        b
                        for b in case["basis_record_ids"]
                        if f"{grade}:" in b or b.endswith(grade) or "별표3" in b
                    }
                    self.assertLessEqual(mine, set(r["clause_ids"]))
            with self.subTest(case["case_id"], total=True):
                self.assertEqual(sum(r["bodily_1"] for r in results), e["coverage_i_total_krw"])
                self.assertEqual(sum(r["bodily_2"] for r in results), e["coverage_ii_total_krw"])


if __name__ == "__main__":
    unittest.main()
