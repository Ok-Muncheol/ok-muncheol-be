"""도표 선택 칸 검수 적용 — 합성 도표만 쓴다. 실제 도표 번호·비율·원문은 넣지 않는다."""

from copy import deepcopy

import pytest

from app.ingestion import chart_scope_review as csr
from app.workflow.candidates import candidate_from_record

DOC = {"document_id": "synthetic-standards", "content_hash": "0" * 64,
       "edition": "synthetic-1", "effective_from": "", "effective_to": None}


def record(chart_id="S-1", variants=((None, 20, 80),), page=10, issues=("unreviewed_source",)):
    return {
        "chart_id": chart_id, "parent_id": f"synthetic-standards:{chart_id}", "document": dict(DOC),
        "title": "synthetic", "page_start": page, "page_end": page + 1,
        "base_variants": [{"variant": v, "base_A": a, "base_B": b, "review_status": "unreviewed"}
                          for v, a, b in variants],
        "review_status": "unreviewed", "calculation_ready": False,
        "filter_status": "unreviewed_candidates_only", "issues": list(issues),
    }


def modifier(chart_id="S-1", n=1, party="A", delta=10, applicability="conditional"):
    return {"modifier_id": f"synthetic-standards:{chart_id}:modifier:{n}", "chart_id": chart_id,
            "party": party, "name": f"synthetic {n}", "delta_percentage_points": delta,
            "applicability": applicability, "review_status": "unreviewed"}


SIGNALIZED = {"accident_type": "car_to_car", "location_type": "signalized_intersection",
              "movement_A": "straight", "movement_B": "left_turn",
              "signal_A": "green", "signal_B": "red"}
UNSIGNALIZED = {"accident_type": "car_to_car", "location_type": "unsignalized_intersection",
                "road_priority": "same_width", "movement_A": "straight", "movement_B": "straight"}


def review(charts):
    return {"schema": csr.REVIEW_SCHEMA, "document_id": DOC["document_id"],
            "content_hash": DOC["content_hash"], "charts": charts}


ONE_VARIANT = {"variant": None, "base_A": 20, "base_B": 80, "conditions": {}}


def entry(attributes=SIGNALIZED, variants=None, page=10):
    return {
        "source_check": {"pdf_page": page, "checked_at": "2026-11-10"},
        "scope_attributes": dict(attributes),
        "scope_variants": deepcopy(variants or [ONE_VARIANT]),  # 공용 상수가 바뀌지 않게
    }


def test_apply_marks_record_reviewed_and_candidate_ready():
    records = [record(), record("S-2")]
    modifiers = [modifier(), modifier(n=2, party="B", delta=None, applicability="not_applicable"),
                 modifier("S-2")]
    result = csr.apply_scope_review(records, modifiers, review({"S-1": entry()}))
    assert result == {"reviewed_charts": 1}
    reviewed, untouched = records
    assert reviewed["review_status"] == "reviewed" and reviewed["calculation_ready"] is True
    assert reviewed["filter_status"] == csr.FILTER_STATUS_REVIEWED
    assert reviewed["issues"] == []
    # 비적용 행은 빠지고, 자동 적용 코드가 없으므로 code·conditions는 null
    assert reviewed["scope_modifiers"] == [{
        "modifier_id": "synthetic-standards:S-1:modifier:1", "party": "A", "name": "synthetic 1",
        "code": None, "delta": 10, "conditions": None,
    }]
    assert reviewed["scope_review"]["reviewed_by"] is None
    assert modifiers[0]["review_status"] == "reviewed"
    assert modifiers[2]["review_status"] == "unreviewed"
    assert untouched["review_status"] == "unreviewed" and "scope_attributes" not in untouched

    hits = [{"chunk": {"document": dict(DOC)}, "score": 0.5}]
    candidate = candidate_from_record(reviewed, hits)  # type: ignore[arg-type]
    assert candidate["calculation_ready"] is True
    assert candidate["attributes"] == SIGNALIZED
    assert candidate["base_variants"] == [ONE_VARIANT]


def test_variants_split_required_field_and_any_value_is_allowed():
    records = [record(variants=(("가", 40, 60), ("나", 30, 70)))]
    variants = [
        {"variant": "가", "base_A": 40, "base_B": 60,
         "conditions": {"entry_order": "simultaneous"}},
        {"variant": "나", "base_A": 30, "base_B": 70,
         "conditions": {"entry_order": "first"}},
    ]
    attributes = {**UNSIGNALIZED, "road_priority": csr.ANY_VALUE}
    csr.apply_scope_review(records, [modifier()], review({"S-1": entry(attributes, variants)}))
    assert [v["conditions"] for v in records[0]["scope_variants"]] == [
        {"entry_order": "simultaneous"}, {"entry_order": "first"}]
    assert records[0]["scope_attributes"]["road_priority"] == "*"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda e: e["scope_variants"][0].update(base_A=30, base_B=70), "원문 표 20:80"),
        (lambda e: e["scope_variants"][0].update(base_B=81), "합이 100"),
        (lambda e: e["scope_attributes"].update(signal_A="blue"), "허용값이 아니다"),
        (lambda e: e["scope_attributes"].update(lane="left"), "FIELD_OPTIONS에 없다"),
        (lambda e: e["scope_attributes"].pop("signal_B"), r"필수 칸 \['signal_B'\]"),
        (lambda e: e["scope_attributes"].update(location_type="roundabout"), "MVP 범위가 아니다"),
        (lambda e: e["source_check"].update(pdf_page=11), "도표 시작 쪽"),
        (lambda e: e["scope_variants"][0].update(variant="가"), "원문 표 변형"),
        (lambda e: e["scope_variants"][0].update(conditions={"signal_A": "green"}), "겹친다"),
    ],
)
def test_mismatch_with_source_table_fails_without_partial_apply(change, message):
    records = [record(), record("S-2")]
    good, bad = entry(), entry()
    change(bad)
    before = deepcopy(records)
    with pytest.raises(ValueError, match=message):
        csr.apply_scope_review(records, [modifier()], review({"S-2": good, "S-1": bad}))
    assert records == before


def test_one_source_variant_can_split_by_condition_with_same_ratio():
    records = [record()]
    sides = [{**ONE_VARIANT, "conditions": {"relative_position": side}}
             for side in ("left", "right")]
    csr.apply_scope_review(records, [modifier()], review({"S-1": entry(variants=sides)}))
    assert [v["conditions"] for v in records[0]["scope_variants"]] == [
        {"relative_position": "left"}, {"relative_position": "right"}]
    hits = [{"chunk": {"document": dict(DOC)}, "score": 0.5}]
    candidate = candidate_from_record(records[0], hits)  # type: ignore[arg-type]
    assert len(candidate["base_variants"]) == 2


@pytest.mark.parametrize(
    ("variants", "message"),
    [
        # 나눈 변형의 비율이 원문과 다르다
        ([{**ONE_VARIANT, "conditions": {"relative_position": "left"}},
          {**ONE_VARIANT, "base_A": 30, "base_B": 70,
           "conditions": {"relative_position": "right"}}], "원문 표 20:80"),
        # 나눈 변형의 조건이 같다
        ([{**ONE_VARIANT, "conditions": {"relative_position": "left"}},
          {**ONE_VARIANT, "conditions": {"relative_position": "left"}}], "다른 변형과 같다"),
    ],
)
def test_split_variants_keep_source_ratio_and_distinct_conditions(variants, message):
    with pytest.raises(ValueError, match=message):
        csr.apply_scope_review([record()], [modifier()], review({"S-1": entry(variants=variants)}))


def test_every_source_variant_must_be_covered():
    records = [record(variants=(("가", 40, 60), ("나", 30, 70)))]
    only_ga = [{"variant": "가", "base_A": 40, "base_B": 60,
                "conditions": {"entry_order": "simultaneous"}}]
    with pytest.raises(ValueError, match="원문 표 변형"):
        csr.apply_scope_review(records, [modifier()], review({"S-1": entry(UNSIGNALIZED, only_ga)}))


def test_multiple_variants_need_distinct_conditions():
    records = [record(variants=(("가", 40, 60), ("나", 30, 70)))]
    variants = [
        {"variant": "가", "base_A": 40, "base_B": 60, "conditions": {}},
        {"variant": "나", "base_A": 30, "base_B": 70, "conditions": {}},
    ]
    with pytest.raises(ValueError, match="조건이 있어야 한다"):
        csr.apply_scope_review(records, [modifier()], review({"S-1": entry(SIGNALIZED, variants)}))


def test_unresolved_modifier_cells_and_wrong_document_are_rejected():
    records = [record(issues=("modifier_value_or_merged_cell_unresolved",))]
    with pytest.raises(ValueError, match="수정요소 값이 추출되지 않은"):
        csr.apply_scope_review(records, [modifier()], review({"S-1": entry()}))
    wrong = review({"S-1": entry()}) | {"content_hash": "1" * 64}
    with pytest.raises(ValueError, match="문서·해시"):
        csr.apply_scope_review([record()], [modifier()], wrong)
    with pytest.raises(ValueError, match="schema"):
        csr.apply_scope_review([record()], [modifier()], {"charts": {}})
