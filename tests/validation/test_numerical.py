"""검증 ③: 과실 산식·수정요소 검사·후검증."""

import pytest

from app.validation.numerical import calculate_fault, calculation_lines, check_numerical


def mod(mid: str, party: str, delta: object, code: str | None = "night") -> dict:
    return {"modifier_id": mid, "party": party, "name": mid, "delta": delta, "code": code}


def test_formula_adds_a_side_and_subtracts_b_side():
    mods = [mod("a1", "A", 10), mod("b1", "B", 20, "signal_violation")]
    assert calculate_fault(30, mods) == (20, 80)


@pytest.mark.parametrize(("base", "mods", "expected"), [
    (95, [mod("a", "A", 20)], (100, 0)),
    (5, [mod("b", "B", 20)], (0, 100)),
    (0, [], (0, 100)),
])
def test_result_is_clipped_to_0_100(base, mods, expected):
    assert calculate_fault(base, mods) == expected


@pytest.mark.parametrize(("base", "mods"), [
    (101, []), (-1, []), (True, []),
    (50, [mod("a", "A", 10), mod("a", "A", 10)]),           # 같은 요소 중복
    (50, [mod("a", "A", 10), mod("b", "A", 10)]),           # 같은 당사자·같은 코드(배타)
    (50, [mod("a", "C", 10)]), (50, [mod("a", "A", True)]),
    (50, [mod("a", "A", 10.0)]), (50, [mod("a", "A", 150)]), (50, [mod("", "A", 10)]),
])
def test_invalid_inputs_are_rejected(base, mods):
    with pytest.raises(ValueError):
        calculate_fault(base, mods)


def test_same_code_on_different_parties_is_allowed():
    assert calculate_fault(50, [mod("a", "A", 10), mod("b", "B", 10)]) == (50, 50)


def result(status="draft_ready", fault=(20, 80), log=None, chart="S-1"):
    return {
        "versions": {"case_id": "c", "fact_card_version": 1, "judgment_version": 1,
                     "report_version": None},
        "status": status, "chart_id": chart, "edition": "e" if chart else None,
        "fault_A": fault[0], "fault_B": fault[1],
        "calculation_log": log if log is not None else
        ["기본 과실 A 20 : B 80", "fault_A = clip(20 + 0 - 0, 0, 100) = 20; fault_B = 80"],
        "evidence": [], "checks": [], "questions": [], "reasons": [],
    }


def test_check_passes_consistent_draft_and_recomputes():
    lines, fault = calculation_lines(20, 80, [mod("a", "A", 5)])
    check = check_numerical(result(fault=fault, log=lines), (20, 80), [mod("a", "A", 5)])
    assert check == {"layer": "numerical", "passed": True, "reasons": []}


@pytest.mark.parametrize("bad", [
    result(fault=(20, 70)), result(fault=(120, -20)), result(fault=(20, 80), chart=None),
    result(log=["fault_A = clip(20 + 0 - 0, 0, 100) = 30; fault_B = 70"]),
    result(status="deferred", fault=(20, 80)),
])
def test_check_fails_inconsistent_results(bad):
    assert check_numerical(bad)["passed"] is False


def test_check_detects_tampered_number_against_inputs():
    lines, _ = calculation_lines(20, 80, [])
    forged = "fault_A = clip(20 + 0 - 0, 0, 100) = 30; fault_B = 70"
    tampered = result(fault=(30, 70), log=[lines[0], forged])
    check = check_numerical(tampered, (20, 80), [])
    assert not check["passed"] and any("재계산" in r for r in check["reasons"])
