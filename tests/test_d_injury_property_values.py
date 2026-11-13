"""대인·대물 숫자 자료(D). 합성 부모 텍스트는 상수로, 실제 코퍼스 대조는 파일이 있을 때만."""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.ingestion import injury_property_values as ipv

ROOT = Path(__file__).resolve().parents[1]
CORPUS_RECORDS = ROOT / "data/private/corpus-db-import/db_import/corpus_records.jsonl"
CASES = ROOT / "tests/fixtures/d_injury_property_cases.json"
REAL_DOC = {"document_id": "samsung-personal-auto-2026-11-11"}
DOC = {"document_id": "synthetic-policy"}


def synthetic_parents(document_id: str = DOC["document_id"]) -> dict[str, str]:
    ids = ipv._parent_ids(document_id)
    limit = ["등급 2016. 3. 31 2016. 4. 1", "이전 사고 이후 사고"]
    limit += [f"{g}급 {a:,}만 원 {b:,}만 원" for g, (a, b) in ipv.LIABILITY_LIMITS_MANWON.items()]
    cited = ["1. 상해 구분별 한도금액"]
    for g, (a, b) in ipv.LIABILITY_LIMITS_MANWON.items():
        cited += [f"{g}급 1. 합성 상해 {a:,} {b:,}", "만 원 만 원"]
    consolation = ["나. 지급기준 : 책임보험상해 구분에 따라 급별로 인정함."]
    consolation += [f"{g}급 {a}만 원" for g, a in ipv.CONSOLATION_MANWON.items()]
    depreciation = [ipv.DEPRECIATION_CONDITION] + [row for *_, row in ipv.DEPRECIATION_ROWS]
    offset = {"until-2022-12-31": [], "from-2023-01-01": []}
    for key, _, _, sentence, *_ in ipv.FAULT_OFFSET_ROWS:
        offset[key].append(sentence)

    def item(n):
        return [
            text
            for row in ipv.TOTAL_LOSS_ROWS
            if row["item"] == n
            for text in (row["condition_text"], row.get("amount_text", ""))
        ]

    return {
        ids["limit"]: "\n".join(limit),
        ids["cited"]: "\n".join(cited),
        ids["consolation"]: "\n".join(consolation),
        ids["depreciation"]: "\n".join(depreciation),
        ids["repair"]: "\n".join(item("1")),
        ids["total_loss"]: "\n".join(item("2")),
        ids["offset:until-2022-12-31"]: "\n".join(offset["until-2022-12-31"]),
        ids["offset:from-2023-01-01"]: "\n".join(offset["from-2023-01-01"]),
    }


def test_records_have_agreed_shape_and_counts():
    records = ipv.records(synthetic_parents(), DOC)
    assert Counter(r["value_type"] for r in records) == {
        "injury_liability_limit_by_grade": 28,
        "injury_consolation_by_grade": 14,
        "depreciation_condition": 3,
        "total_loss_condition": 3,
        "injury_fault_offset_rule": 4,
    }
    assert len({r["record_id"] for r in records}) == 52
    for r in records:
        assert r["parent_id"] is None
        assert r["source_parent_id"].startswith(DOC["document_id"] + ":")
        assert {"accident_date_from", "accident_date_to", "source_pdf_page", "schedule"} <= set(r)
        assert r["calculation_ready"] is False
        assert r["review_status"] == "unreviewed"
    limits = {
        (r["grade"], r["accident_date_from"], r["accident_date_to"]): r["amount_krw"]
        for r in records
        if r["value_type"] == "injury_liability_limit_by_grade"
    }
    assert limits[(9, None, "2016-03-31")] == limits[(9, "2016-04-01", None)] == 2_400_000
    assert limits[(14, None, "2016-03-31")] == 800_000
    assert limits[(14, "2016-04-01", None)] == 500_000
    assert limits[(5, None, "2016-03-31")] == limits[(5, "2016-04-01", None)] == 9_000_000


def test_depreciation_rows_spell_out_boundaries():
    records = ipv.records(synthetic_parents(), DOC)
    rows = [r for r in records if r["value_type"] == "depreciation_condition"]
    assert [(r["age_years_min"], r["age_years_max"], r["percent"]) for r in rows] == [
        (0, 1, 20),
        (1, 2, 15),
        (2, 5, 10),
    ]
    assert [r["age_years_min_exclusive"] for r in rows] == [False, True, True]
    for r in rows:
        assert r["age_years_max_inclusive"] is True
        assert r["repair_ratio_percent_min"] == 20
        assert r["repair_ratio_percent_min_exclusive"] is True
        assert "20%를 초과" in r["condition_text"]


def test_fault_offset_editions_differ_where_text_differs():
    rows = {
        (r["clause"], r["accident_date_from"] or r["accident_date_to"]): r
        for r in ipv.records(synthetic_parents(), DOC)
        if r["value_type"] == "injury_fault_offset_rule"
    }
    assert rows[("1.가.(2)", "2022-12-31")]["within_coverage_i_limit"] is False
    assert rows[("1.가.(2)", "2023-01-01")]["within_coverage_i_limit"] is True
    assert rows[("1.가.(3)", "2023-01-01")]["driver_grade_12_14_exception"] is True
    assert rows[("1.가.(3)", "2022-12-31")]["driver_grade_12_14_exception"] is False
    assert all(rows[(c, d)]["deduct_coverage_i_payable"] for c, d in rows if c == "1.가.(3)")


@pytest.mark.parametrize(
    ("key", "old", "new"),
    [
        ("limit", "9급 240만 원 240만 원", "9급 240만 원 250만 원"),
        ("limit", "14급 80만 원 50만 원", "14급 50만 원 80만 원"),
        ("limit", "이전 사고 이후 사고", "이후 사고 이전 사고"),
        ("cited", "5급 1. 합성 상해 900 900", "5급 1. 합성 상해 900 1,000"),
        ("consolation", "7급 40만 원", "7급 45만 원"),
        ("depreciation", "20%를 초과하는", "20% 이상인"),
        ("depreciation", "1년 초과 2년 이하", "1년 이상 2년 미만"),
        ("repair", "120%를 한도로", "110%를 한도로"),
        ("offset:from-2023-01-01", "12급 내지 14급", "11급 내지 14급"),
    ],
)
def test_text_mismatch_raises(key, old, new):
    parents = synthetic_parents()
    pid = ipv._parent_ids(DOC["document_id"])[key]
    assert old in parents[pid]
    parents[pid] = parents[pid].replace(old, new)
    with pytest.raises(ValueError):
        ipv.records(parents, DOC)


def test_swapped_fault_offset_editions_raise():
    parents = synthetic_parents()
    ids = ipv._parent_ids(DOC["document_id"])
    until, after = ids["offset:until-2022-12-31"], ids["offset:from-2023-01-01"]
    parents[until] = parents[until] + "\n" + parents[after]
    with pytest.raises(ValueError, match="2023년 판"):
        ipv.records(parents, DOC)


def test_missing_parent_raises():
    parents = synthetic_parents()
    del parents[ipv._parent_ids(DOC["document_id"])["cited"]]
    with pytest.raises(ValueError, match="부모 원문이 없다"):
        ipv.records(parents, DOC)


def test_calculation_ready_only_for_reviewed_ids(monkeypatch):
    ids = {r["record_id"] for r in ipv.build(REAL_DOC)}
    assert ipv.REVIEWED_RECORD_IDS <= ids, ipv.REVIEWED_RECORD_IDS - ids
    for r in ipv.build(REAL_DOC):
        assert r["calculation_ready"] is (r["record_id"] in ipv.REVIEWED_RECORD_IDS)
        assert (r["review_status"] == "reviewed_against_pdf") is r["calculation_ready"]
    one = f"{REAL_DOC['document_id']}:별표1-나-2-부상위자료:3급"
    monkeypatch.setattr(ipv, "REVIEWED_RECORD_IDS", frozenset({one}))
    assert [r["record_id"] for r in ipv.build(REAL_DOC) if r["calculation_ready"]] == [one]


def test_reviewed_ids_cover_exactly_the_built_records():
    """2026-11-13 사용자 대조: 52개 전부. 레코드가 늘거나 바뀌면 다시 대조해야 한다."""
    built = [r["record_id"] for r in ipv.build(REAL_DOC)]
    assert len(ipv.REVIEWED_RECORD_IDS) == len(built) == 52
    assert ipv.REVIEWED_RECORD_IDS == set(built)
    assert all(r["calculation_ready"] for r in ipv.build(REAL_DOC))
    assert not any(r["calculation_ready"] for r in ipv.build(DOC))


def _reference(case: dict, records: dict[str, dict]) -> dict:
    """정답 예시를 레코드 값으로 다시 계산한다(김지은 님 계산 코드와 별개인 확인용)."""
    inp = case["input"]
    fault = inp["our_fault_percent"]
    if fault == 0:
        return {"status": "배상책임 확인"}
    if case["kind"] == "property":
        amount = inp["repair_cost_krw"] * fault // 100
        limit = inp["property_damage_limit_krw"]
        return {"amount_krw": min(amount, limit), "capped_by_limit": amount > limit}
    doc = REAL_DOC["document_id"]
    persons = []
    for p in inp["persons"]:
        g = p["grade"]
        con = records[f"{doc}:별표1-나-2-부상위자료:{g}급"]["amount_krw"]
        lim = records[f"{doc}:별표1-표1-부상한도:{g}급:from-2016-04-01"]["amount_krw"]
        after = (p["treatment_cost_krw"] + con) * fault // 100
        total = max(after, p["treatment_cost_krw"])
        persons.append((min(total, lim), total - min(total, lim), after < p["treatment_cost_krw"]))
    return {
        "coverage_i_total_krw": sum(i for i, _, _ in persons),
        "coverage_ii_total_krw": sum(ii for _, ii, _ in persons),
        "floors": [f for *_, f in persons],
    }


def test_answer_cases_match_records():
    data = json.loads(CASES.read_text(encoding="utf-8"))
    records = {r["record_id"]: r for r in ipv.build(REAL_DOC)}
    assert data["document_id"] == REAL_DOC["document_id"]
    assert data["accident_date"] >= "2023-01-01"
    by_id = {c["case_id"]: c for c in data["cases"]}
    assert sorted(by_id) == ["대물-1", "대물-2", "대물-3", "대인-1", "대인-2", "대인-3", "대인-4"]
    for case in data["cases"]:
        assert set(case["basis_record_ids"]) <= set(records), case["case_id"]
        expected, got = case["expected"], _reference(case, records)
        if got.get("status"):
            assert expected["status"] == got["status"] and expected["amount_krw"] is None
        elif case["kind"] == "property":
            assert (expected["amount_krw"], expected["capped_by_limit"]) == (
                got["amount_krw"],
                got["capped_by_limit"],
            )
        else:
            assert all(p["grade"] < 12 for p in case["input"]["persons"])
            assert expected["coverage_i_total_krw"] == got["coverage_i_total_krw"]
            assert expected["coverage_ii_total_krw"] == got["coverage_ii_total_krw"]
            floors = [p["treatment_floor_applied"] for p in expected["persons"]]
            assert floors == got["floors"]
    # 합의한 숫자(사용자 계획)를 그대로 고정한다.
    assert by_id["대물-1"]["expected"]["amount_krw"] == 2_419_752
    assert by_id["대물-2"]["expected"]["amount_krw"] == 20_000_000
    totals = {
        k: (
            by_id[k]["expected"]["coverage_i_total_krw"],
            by_id[k]["expected"]["coverage_ii_total_krw"],
        )
        for k in ("대인-1", "대인-2", "대인-3", "대인-4")
    }
    assert totals == {
        "대인-1": (3_616_000, 0),
        "대인-2": (3_000_000, 0),
        "대인-3": (14_475_000, 3_000_000),
        "대인-4": (12_000_000, 4_520_000),
    }
    assert [p["treatment_floor_applied"] for p in by_id["대인-3"]["expected"]["persons"]] == [
        True,
        False,
    ]


@pytest.mark.skipif(not CORPUS_RECORDS.exists(), reason="실제 코퍼스 없음(data/private)")
def test_real_corpus_parents_match_constants():
    parents, doc = {}, None
    with CORPUS_RECORDS.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("corpus") == "policy_terms" and row.get("is_parent"):
                parents[row["parent_id"]] = row["body"].get("text") or ""
                if row["parent_id"].endswith(":payment_standard:별표1:표1"):
                    doc = row["body"]["document"]
    records = ipv.records(parents, doc)
    assert len(records) == 52
    assert {r["source_parent_id"] for r in records} <= set(parents)
