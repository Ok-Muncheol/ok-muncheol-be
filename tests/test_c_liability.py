"""C: 대물·대인 1차 판정 초안(app/calculation/liability.py). 합성 숫자만 쓴다(약관 금액 아님)."""

import unittest

from app.calculation.liability import (
    CLAUSE_OFFSET_FROM_2023,
    CLAUSE_OFFSET_UNTIL_2022,
    NO_DATE,
    VT_CONSOLATION,
    VT_DEPRECIATION,
    VT_LIMIT,
    ScheduleValues,
    calculate_injury,
    calculate_liability,
    calculate_property,
)


def row(value_type, grade, amount, *, start=None, end=None, ready=True, rid=None):
    return {
        "record_id": rid or f"synthetic:{value_type}:{grade}:{start}",
        "value_type": value_type,
        "grade": grade,
        "amount_krw": amount,
        "accident_date_from": start,
        "accident_date_to": end,
        "calculation_ready": ready,
    }


# 합성 급수표: 한도는 판이 둘(사고일로 고름), 위자료는 하나
VALUES = [
    row(VT_LIMIT, 3, 1_000_000, end="2016-03-31"),
    row(VT_LIMIT, 3, 2_000_000, start="2016-04-01"),
    row(VT_LIMIT, 14, 300_000, start="2016-04-01"),
    row(VT_CONSOLATION, 3, 400_000),
    row(VT_CONSOLATION, 14, 100_000),
]
DEPRECIATION = [
    {
        "record_id": "synthetic:dep:1",
        "value_type": VT_DEPRECIATION,
        "age_years_min": 0,
        "age_years_max": 1,
        "repair_ratio_percent_min": 20,
        "percent": 20,
        "calculation_ready": True,
    },
    {
        "record_id": "synthetic:dep:2",
        "value_type": VT_DEPRECIATION,
        "age_years_min": 1,
        "age_years_max": 2,
        "repair_ratio_percent_min": 20,
        "percent": 15,
        "calculation_ready": True,
    },
    {
        "record_id": "synthetic:dep:3",
        "value_type": VT_DEPRECIATION,
        "age_years_min": 2,
        "age_years_max": 5,
        "repair_ratio_percent_min": 20,
        "percent": 10,
        "calculation_ready": True,
    },
]


def estimate(amount, party="cp", read_by="pdf_extract", status="unverified", doc="est-1"):
    return {
        "kind": "repair_estimate",
        "document_id": doc,
        "party_id": party,
        "amount": amount,
        "read_by": read_by,
        "verification_status": status,
        "received_at": "2026-11-12T10:00:00+09:00",
        "storage_path": None,
    }


def keys(result):
    return [f["key"] for f in result["flags"]]


class PropertyTest(unittest.TestCase):
    def test_repair_times_our_fault_with_limit(self):
        r = calculate_property(
            [estimate(1_000_001)],
            {"counterparty_id": "cp", "property_limit": 500_000},
            70,
            accident_date="2026-11-01",
        )
        self.assertEqual(r["status"], "draft")
        self.assertEqual(r["repair_cost"], 1_000_001)
        self.assertEqual(r["amount"], 500_000)  # 700,000(원 미만 버림) → 한도
        self.assertTrue(r["limit_applied"])
        self.assertIn("rental", keys(r))
        self.assertIn(CLAUSE_OFFSET_FROM_2023, r["clause_ids"])

    def test_no_limit_input_is_an_assumption_not_a_cap(self):
        r = calculate_property([estimate(1_000_001)], {"counterparty_id": "cp"}, 70)
        self.assertEqual(r["amount"], 700_000)
        self.assertEqual(r["assumptions"], ["대물 한도 미입력 — 한도 검사 생략"])
        self.assertFalse(r["limit_applied"])

    def test_only_counterparty_estimate_and_unverified_ocr_gives_no_amount(self):
        ours = estimate(9_999, party="insured", doc="ours")
        r = calculate_property([ours], {"counterparty_id": "cp"}, 50)
        self.assertEqual((r["status"], r["amount"]), ("needs_input", None))
        ocr = estimate(100_000, read_by="ocr")
        r = calculate_property([ocr], {"counterparty_id": "cp"}, 50)
        self.assertIsNone(r["amount"])
        self.assertIn("OCR로 읽은 견적 금액 — 담당자 확인 전", r["reasons"])

    def test_total_loss_and_depreciation_are_flags_only(self):
        inp = {"counterparty_id": "cp", "vehicle_value": 900_000, "release_date": "2026-03-01"}
        r = calculate_property(
            [estimate(1_000_000)],
            inp,
            100,
            accident_date="2026-11-01",
            values=ScheduleValues(DEPRECIATION),
        )
        self.assertEqual(r["amount"], 1_000_000)  # 표시만 하고 금액은 그대로
        self.assertIn("total_loss", keys(r))
        dep = next(f for f in r["flags"] if f["key"] == "depreciation")
        self.assertEqual(dep["label"], "시세하락손해 대상 가능")
        self.assertIn("synthetic:dep:1", dep["clause_ids"])

    def test_missing_vehicle_value_asks_instead_of_guessing(self):
        r = calculate_property([estimate(100_000)], {"counterparty_id": "cp"}, 100)
        labels = {f["key"]: f["label"] for f in r["flags"]}
        self.assertEqual(labels["total_loss"], "전손 여부 확인 필요")
        self.assertEqual(labels["depreciation"], "시세하락손해 확인 필요")

    def test_old_car_or_small_repair_is_not_depreciation(self):
        inp = {"counterparty_id": "cp", "vehicle_value": 10_000_000, "release_date": "2026-03-01"}
        r = calculate_property(
            [estimate(100_000)],
            inp,
            100,
            accident_date="2026-11-01",
            values=ScheduleValues(DEPRECIATION),
        )
        self.assertNotIn("depreciation", keys(r))

    def test_depreciation_band_uses_calendar_years(self):
        # 윤일(2024-02-29)이 낀 기간: 일수 / 365.25로는 정확히 1·2년이 다음 구간으로 넘어간다
        cases = [
            ("2023-06-01", "2024-06-01", "synthetic:dep:1"),  # 정확히 1년 → 1년 이하
            ("2023-06-01", "2024-06-02", "synthetic:dep:2"),
            ("2022-06-01", "2024-06-01", "synthetic:dep:2"),  # 정확히 2년 → 2년 이하
            ("2022-06-01", "2024-06-02", "synthetic:dep:3"),
            ("2019-06-01", "2024-06-01", "synthetic:dep:3"),  # 정확히 5년 → 5년 이하
            ("2019-06-01", "2024-06-02", None),  # 5년 초과 → 대상 아님
            ("2024-02-29", "2025-02-28", "synthetic:dep:1"),  # 2월 29일 출고 + 1년 = 2월 28일
            ("2024-02-29", "2025-03-01", "synthetic:dep:2"),
        ]
        for release, accident, record in cases:
            with self.subTest(release=release, accident=accident):
                inp = {
                    "counterparty_id": "cp",
                    "vehicle_value": 10_000_000,
                    "release_date": release,
                }
                r = calculate_property(
                    [estimate(3_000_000)],
                    inp,
                    100,
                    accident_date=accident,
                    values=ScheduleValues(DEPRECIATION),
                )
                dep = [f for f in r["flags"] if f["key"] == "depreciation"]
                if record is None:
                    self.assertEqual(dep, [])
                else:
                    self.assertIn(record, dep[0]["clause_ids"])

    def test_zero_fault_needs_review_without_amount(self):
        r = calculate_property([estimate(100_000)], {"counterparty_id": "cp"}, 0)
        self.assertEqual((r["status"], r["amount"]), ("needs_review", None))
        self.assertEqual(r["repair_cost"], 100_000)
        self.assertIn("우리 과실 0% — 배상책임 여부를 담당자가 먼저 확인", r["reasons"])

    def test_unknown_fault_keeps_repair_but_no_amount(self):
        r = calculate_property([estimate(100_000)], {"counterparty_id": "cp"}, None)
        self.assertEqual((r["repair_cost"], r["amount"]), (100_000, None))
        self.assertIn("우리 과실 미정 — 과실비율 확정 후 산정", r["reasons"])


class InjuryTest(unittest.TestCase):
    def person(self, grade=3, treatment=600_000, **kw):
        return {"person_id": "p1", "grade": grade, "treatment_cost": treatment, **kw}

    def test_offset_then_split_bodily_1_and_2(self):
        r = calculate_injury(
            self.person(treatment=3_000_000),
            95,
            accident_date="2026-11-01",
            values=ScheduleValues(VALUES),
        )
        # (3,000,000 + 400,000) × 95% = 3,230,000 → 대인Ⅰ 한도 2,000,000, 대인Ⅱ 1,230,000
        self.assertEqual(r["status"], "draft")
        self.assertEqual(
            (r["consolation"], r["damage"], r["after_offset"]), (400_000, 3_400_000, 3_230_000)
        )
        self.assertEqual(
            (r["bodily_1_limit"], r["bodily_1"], r["bodily_2"]), (2_000_000, 2_000_000, 1_230_000)
        )
        self.assertFalse(r["treatment_guarantee"])
        self.assertIn("synthetic:injury_liability_limit_by_grade:3:2016-04-01", r["clause_ids"])

    def test_treatment_guarantee_also_reaches_bodily_2(self):
        # 별표3 1. 가. (3): 대인Ⅱ도 과실상계 후가 치료관계비에 못 미치면 치료관계비(대인Ⅰ 공제)
        r = calculate_injury(
            self.person(treatment=3_000_000),
            80,
            accident_date="2026-11-01",
            values=ScheduleValues(VALUES),
        )
        self.assertEqual(r["after_offset"], 2_720_000)  # < 치료관계비 3,000,000
        self.assertTrue(r["treatment_guarantee"])
        self.assertEqual((r["bodily_1"], r["bodily_2"]), (2_000_000, 1_000_000))

    def test_treatment_guarantee_when_offset_falls_below_treatment(self):
        r = calculate_injury(
            self.person(treatment=600_000),
            20,
            accident_date="2026-11-01",
            values=ScheduleValues(VALUES),
        )
        # (600,000 + 400,000) × 20% = 200,000 < 치료관계비 600,000 → 600,000 보상
        self.assertEqual(r["after_offset"], 200_000)
        self.assertTrue(r["treatment_guarantee"])
        self.assertEqual((r["bodily_1"], r["bodily_2"]), (600_000, 0))

    def test_accident_date_picks_the_limit_edition(self):
        r = calculate_injury(
            self.person(treatment=3_000_000),
            100,
            accident_date="2015-06-01",
            values=ScheduleValues(VALUES),
        )
        self.assertEqual(r["bodily_1_limit"], 1_000_000)
        self.assertIn(CLAUSE_OFFSET_UNTIL_2022, r["clause_ids"])
        # 2022-12-31 이전 사고는 별표3 이전 판이라 금액 없이 담당자 확인
        self.assertEqual((r["status"], r["bodily_1"]), ("needs_review", None))

    def test_no_accident_date_assumes_latest_edition(self):
        r = calculate_injury(self.person(), 100, values=ScheduleValues(VALUES))
        self.assertEqual(r["bodily_1_limit"], 2_000_000)
        self.assertEqual(r["assumptions"], [NO_DATE])

    def test_missing_inputs_and_unreviewed_values_give_no_amount(self):
        r = calculate_injury(
            self.person(grade=None, treatment=None), 50, values=ScheduleValues(VALUES)
        )
        self.assertEqual((r["status"], r["bodily_1"]), ("needs_input", None))
        self.assertEqual(len(r["reasons"]), 2)
        unready = [dict(v, calculation_ready=False) for v in VALUES]
        r = calculate_injury(
            self.person(), 50, accident_date="2026-11-01", values=ScheduleValues(unready)
        )
        self.assertIsNone(r["bodily_1"])
        self.assertTrue(any("검수 전 값만 있음" in x for x in r["reasons"]))

    def test_minor_driver_holds_only_when_treatment_exceeds_limit(self):
        # 14급 대인Ⅰ 한도 300,000(합성). 넘으면 차량운전자 특례로 금액 보류, 아니면 계산
        over = calculate_injury(
            self.person(grade=14, treatment=400_000),
            50,
            accident_date="2026-11-01",
            values=ScheduleValues(VALUES),
        )
        self.assertEqual((over["status"], over["bodily_1"]), ("needs_review", None))
        self.assertTrue(any("차량운전자 특례" in x for x in over["reasons"]))
        self.assertEqual(over["bodily_1_limit"], 300_000)
        under = calculate_injury(
            self.person(grade=14, treatment=300_000),
            50,
            accident_date="2026-11-01",
            values=ScheduleValues(VALUES),
        )
        self.assertEqual(under["status"], "draft")
        passenger = calculate_injury(
            self.person(grade=14, treatment=400_000, is_driver=False),
            50,
            accident_date="2026-11-01",
            values=ScheduleValues(VALUES),
        )
        self.assertEqual(passenger["status"], "draft")

    def test_zero_fault_needs_review(self):
        r = calculate_injury(
            self.person(), 0, accident_date="2026-11-01", values=ScheduleValues(VALUES)
        )
        self.assertEqual((r["status"], r["bodily_1"]), ("needs_review", None))

    def test_flags_always_warn_and_grade_specific_ones_appear(self):
        r = calculate_injury(
            self.person(grade=14, treatment=100_000),
            50,
            accident_date="2026-11-01",
            values=ScheduleValues(VALUES),
        )
        for k in (
            "final_diagnosis",
            "lost_income",
            "disability_death",
            "minor_treatment",
            "minor_driver",
        ):
            self.assertIn(k, keys(r))
        self.assertNotIn("nursing", keys(r))
        passenger = calculate_injury(
            self.person(grade=14, treatment=100_000, is_driver=False),
            50,
            accident_date="2026-11-01",
            values=ScheduleValues(VALUES),
        )
        self.assertNotIn("minor_driver", keys(passenger))
        severe = calculate_injury(self.person(grade=3), 50, values=ScheduleValues(VALUES))
        self.assertIn("nursing", keys(severe))


class LiabilityTest(unittest.TestCase):
    def test_bundle_property_and_each_person(self):
        result = calculate_liability(
            [estimate(1_000_000)],
            insured_fault=60,
            accident_date="2026-11-01",
            property_input={"counterparty_id": "cp"},
            injured=[
                {"person_id": "a", "grade": 3, "treatment_cost": 600_000},
                {"person_id": "b", "grade": None, "treatment_cost": 100_000},
            ],
            schedule_values=VALUES,
        )
        self.assertEqual(result["property"]["amount"], 600_000)
        self.assertEqual([i["status"] for i in result["injuries"]], ["draft", "needs_input"])
        self.assertTrue(result["notice"].startswith("1차 판정 초안"))


if __name__ == "__main__":
    unittest.main()
