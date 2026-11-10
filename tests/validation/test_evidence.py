"""검증 ①: 선택한 도표·인용·수정요소가 검수 레코드와 원문에 실재하는지."""

from app.validation.evidence import PERIOD_UNKNOWN_WARNING, check_evidence
from tests.workflow.synthetic import candidates


def selection(cand, applied=()):
    return {
        "chart_id": cand["chart_id"], "parent_id": cand["parent_id"], "edition": cand["edition"],
        "variant": cand["base_variants"][0], "applied_modifiers": list(applied),
        "unresolved_modifiers": [], "alternatives": [], "reasons": [],
    }


def test_consistent_selection_passes():
    cands = candidates()
    applied = [{"modifier_id": cands[0]["modifiers"][0]["modifier_id"], "party": "A",
                "name": "야간", "code": "night", "delta": 5}]
    check = check_evidence(selection(cands[0], applied), cands, "2026-10-01")
    assert check == {"layer": "evidence", "passed": True, "reasons": []}


def test_each_defect_is_named():
    cands = candidates()
    sel = selection(cands[0], [{"modifier_id": "made-up", "party": "A", "name": "x",
                                "code": "night", "delta": 5}])
    cands[0]["hits"][0]["chunk"]["page_start"] = cands[0]["hits"][0]["chunk"]["page_end"] = 99
    cands[0]["effective_to"] = "2025-12-31"
    check = check_evidence(sel, cands, "2026-10-01")
    assert not check["passed"]
    joined = " ".join(check["reasons"])
    assert "유효하지 않다" in joined and "페이지" in joined and "made-up" in joined


def test_unknown_start_date_is_a_warning_not_a_failure():
    cands = candidates()
    cands[0]["effective_from"] = ""
    check = check_evidence(selection(cands[0]), cands, "2026-10-01")
    # 시행일을 지어내지 않는다. 통과시키되 보고서에 보이도록 사유에 경고를 남긴다
    assert check == {"layer": "evidence", "passed": True,
                     "reasons": [PERIOD_UNKNOWN_WARNING.format(edition="synthetic-1")]}


def test_known_end_date_still_applies_without_start_date():
    cands = candidates()
    cands[0]["effective_from"], cands[0]["effective_to"] = "", "2025-12-31"
    check = check_evidence(selection(cands[0]), cands, "2026-10-01")
    assert not check["passed"]
    assert check["reasons"] == ["적용판 synthetic-1은 사고 일자 2026-10-01에 유효하지 않다"]


def test_unknown_chart_or_unreviewed_or_tampered_delta_fails():
    cands = candidates()
    ghost = selection({**cands[0], "chart_id": "ghost"})
    assert not check_evidence(ghost, cands, "2026-10-01")["passed"]
    cands[0]["calculation_ready"] = False
    assert not check_evidence(selection(cands[0]), cands, "2026-10-01")["passed"]
    cands = candidates()
    bad = [{"modifier_id": cands[0]["modifiers"][0]["modifier_id"], "party": "A", "name": "야간",
            "code": "night", "delta": 50}]
    check = check_evidence(selection(cands[0], bad), cands, "2026-10-01")
    assert any("가감치" in r for r in check["reasons"])


def test_no_hits_fails():
    cands = candidates()
    cands[0]["hits"] = []
    check = check_evidence(selection(cands[0]), cands, "2026-10-01")
    assert any("원문 청크가 없다" in r for r in check["reasons"])
