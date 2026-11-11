"""C: 검수한 도표 선택 칸(scope_*)을 인정기준 도표 레코드에 붙인다.

검수 파일은 원문 비율을 담으므로 data/private에만 둔다. 이 모듈은 검수 값을 그대로 믿지 않고
원문 표에서 추출한 값과 기계 대조한다. 하나라도 어긋나면 빌드를 멈춘다(부분 적용 없음).

- `scope_attributes`: 도표 전체에 걸리는 카드 칸 값. 칸당 값 하나
  (`chart_scope.FIELD_OPTIONS`).
- `scope_variants`: 기본 비율 변형. 변형 이름·비율이 추출값과 같아야 하고,
  변형이 둘 이상이면 조건으로 갈린다. 원문 변형 하나를 같은 비율로 나눌 수 있다
  (값이 여럿인 칸, 예: 측면 도표의 `relative_position` left·right).
- `scope_modifiers`: 표의 수정요소 행 그대로. 자동 적용 코드가 없으므로 `code`·`conditions`는 null.
- `"*"`: 도표가 그 칸으로 가르지 않는다(무관). 빈 값·미정의와 구분하려고 명시한다.
"""

from app.schemas.chart_scope import (
    ANY_VALUE,
    FIELD_OPTIONS,
    IN_SCOPE_LOCATION_TYPES,
    REQUIRED_CONFIRMED_FIELDS,
)

__all__ = ["ANY_VALUE", "apply_scope_review", "check_scope_review", "scope_modifiers"]
REVIEW_SCHEMA = "chart-scope-review-v1"
REVIEW_METHOD = "raw_chart_table_crosscheck+source_page_visual_check"
FILTER_STATUS_REVIEWED = "reviewed_scope"


def _check_values(chart_id: str, where: str, values: object, errors: list[str]) -> dict[str, str]:
    if not isinstance(values, dict):
        errors.append(f"{chart_id}: {where}는 객체여야 한다")
        return {}
    for key, value in values.items():
        if key not in FIELD_OPTIONS:
            errors.append(f"{chart_id}: {where}의 칸 {key!r}는 chart_scope.FIELD_OPTIONS에 없다")
        elif value != ANY_VALUE and value not in FIELD_OPTIONS[key]:
            errors.append(f"{chart_id}: {where}의 {key}={value!r}는 허용값이 아니다")
    return values


def _variant_label(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _check_variants(
    record: dict, variants: object, attributes: dict[str, str], required: set[str],
    errors: list[str],
) -> None:
    chart_id = record["chart_id"]
    if not isinstance(variants, list) or not variants:
        errors.append(f"{chart_id}: scope_variants가 비었다")
        return
    extracted = {_variant_label(v["variant"]): v for v in record["base_variants"]}
    # 원문 변형 하나를 조건별로 나눌 수 있다(예: 측면 도표의 left·right). 원문 변형은 빠짐없이,
    # 원문에 없는 변형은 없이. 나눈 변형끼리는 비율이 원문과 같고 조건이 달라야 한다(아래 검사)
    labels = [_variant_label(v.get("variant")) for v in variants if isinstance(v, dict)]
    if set(labels) != set(extracted):
        errors.append(
            f"{chart_id}: 변형 {sorted(set(labels), key=str)}가 "
            f"원문 표 변형 {sorted(extracted, key=str)}과 다르다"
        )
    seen: list[dict] = []
    for variant in variants:
        if not isinstance(variant, dict):
            errors.append(f"{chart_id}: 변형은 객체여야 한다")
            continue
        label = _variant_label(variant.get("variant"))
        base_a, base_b = variant.get("base_A"), variant.get("base_B")
        valid = (isinstance(base_a, int) and isinstance(base_b, int)
                 and 0 <= base_a <= 100 and base_a + base_b == 100)
        if not valid:
            errors.append(
                f"{chart_id}: 변형 {label} 비율 {base_a}:{base_b}는 합이 100인 정수여야 한다"
            )
        source = extracted.get(label)
        if source and (source["base_A"], source["base_B"]) != (base_a, base_b):
            errors.append(
                f"{chart_id}: 변형 {label} 비율 {base_a}:{base_b}가 "
                f"원문 표 {source['base_A']}:{source['base_B']}와 다르다"
            )
        where = f"변형 {label} conditions"
        conditions = _check_values(chart_id, where, variant.get("conditions"), errors)
        overlap = sorted(set(conditions) & set(attributes))
        if overlap:
            errors.append(f"{chart_id}: 변형 {label} 조건 {overlap}이 scope_attributes와 겹친다")
        missing = sorted(required - set(attributes) - set(conditions))
        if missing:
            errors.append(f"{chart_id}: 변형 {label}이 필수 칸 {missing}을 정의하지 않는다")
        if len(variants) > 1 and not conditions:
            errors.append(f"{chart_id}: 변형이 여럿이면 변형 {label}에 조건이 있어야 한다")
        if conditions in seen:
            errors.append(f"{chart_id}: 변형 {label} 조건이 다른 변형과 같다")
        seen.append(conditions)


def check_scope_review(record: dict, modifier_rows: list[dict], review: dict) -> list[str]:
    """검수 값 하나를 도표 레코드·수정요소 추출값과 대조한다. 오류 문구 목록(비면 통과)."""
    chart_id = record["chart_id"]
    errors: list[str] = []
    source = review.get("source_check") or {}
    if source.get("pdf_page") != record["page_start"]:
        errors.append(
            f"{chart_id}: source_check.pdf_page가 도표 시작 쪽({record['page_start']})과 다르다"
        )
    if not source.get("checked_at"):
        errors.append(f"{chart_id}: source_check.checked_at이 없다")
    if "modifier_value_or_merged_cell_unresolved" in record.get("issues", []):
        errors.append(f"{chart_id}: 수정요소 값이 추출되지 않은 도표는 검수 처리하지 않는다")
    if modifier_rows and all(m["applicability"] == "not_applicable" for m in modifier_rows):
        errors.append(f"{chart_id}: 적용할 수정요소 행이 없다")

    where = "scope_attributes"
    attributes = _check_values(chart_id, where, review.get(where), errors)
    if attributes.get("accident_type") != "car_to_car":
        errors.append(f"{chart_id}: scope_attributes.accident_type은 car_to_car여야 한다")
    location = attributes.get("location_type")
    if location not in IN_SCOPE_LOCATION_TYPES:
        errors.append(
            f"{chart_id}: scope_attributes.location_type {location!r}는 MVP 범위가 아니다"
        )
    required = set(REQUIRED_CONFIRMED_FIELDS.get(location, ()))  # type: ignore[call-overload]
    _check_variants(record, review.get("scope_variants"), attributes, required, errors)
    return errors


def scope_modifiers(modifier_rows: list[dict]) -> list[dict]:
    """표의 수정요소 행을 후보 계약(ChartModifier) 모양으로 옮긴다. 비적용 행은 뺀다."""
    return [
        {
            "modifier_id": m["modifier_id"], "party": m["party"], "name": m["name"],
            "code": None, "delta": m["delta_percentage_points"], "conditions": None,
        }
        for m in modifier_rows
        if m["applicability"] != "not_applicable"
    ]


def apply_scope_review(records: list[dict], modifiers: list[dict], review: dict) -> dict[str, int]:
    """검수 파일을 레코드·수정요소에 적용한다.

    오류가 하나라도 있으면 ValueError를 내고 아무것도 바꾸지 않는다.
    """
    if review.get("schema") != REVIEW_SCHEMA:
        raise ValueError(f"검수 파일 schema는 {REVIEW_SCHEMA}여야 한다")
    by_chart = {r["chart_id"]: r for r in records}
    rows_by_chart: dict[str, list[dict]] = {}
    for row in modifiers:
        rows_by_chart.setdefault(row["chart_id"], []).append(row)
    charts = review.get("charts") or {}
    identity = (review.get("document_id"), review.get("content_hash"))
    errors: list[str] = []
    for chart_id, entry in charts.items():
        record = by_chart.get(chart_id)
        if record is None:
            errors.append(f"{chart_id}: 도표 레코드가 없다")
            continue
        document = record["document"]
        if identity != (document["document_id"], document["content_hash"]):
            errors.append(f"{chart_id}: 검수 파일의 문서·해시가 도표 레코드와 다르다")
        errors += check_scope_review(record, rows_by_chart.get(chart_id, []), entry)
    if errors:
        raise ValueError("도표 검수 값 오류:\n" + "\n".join(errors))

    for chart_id, entry in charts.items():
        record = by_chart[chart_id]
        rows = rows_by_chart.get(chart_id, [])
        record["scope_attributes"] = dict(entry["scope_attributes"])
        record["scope_variants"] = [
            {"variant": v.get("variant"), "base_A": v["base_A"], "base_B": v["base_B"],
             "conditions": dict(v.get("conditions") or {})}
            for v in entry["scope_variants"]
        ]
        record["scope_modifiers"] = scope_modifiers(rows)
        record["scope_review"] = {
            "method": REVIEW_METHOD,
            "source_pdf_page": entry["source_check"]["pdf_page"],
            "checked_at": entry["source_check"]["checked_at"],
            "reviewed_by": entry.get("reviewed_by"),
            "notes": entry.get("notes", []),
        }
        record["review_status"] = "reviewed"
        record["calculation_ready"] = True
        record["filter_status"] = FILTER_STATUS_REVIEWED
        record["issues"] = [i for i in record["issues"] if i != "unreviewed_source"]
        for variant in record["base_variants"]:
            variant["review_status"] = "reviewed"
        for row in rows:
            row["review_status"] = "reviewed"
    return {"reviewed_charts": len(charts)}
