"""B: 검증 ③ DB 기반 과실 계산·코드 검증. 금액 산정은 D 소유다."""

from collections.abc import Mapping

from app.schemas.judgment import JudgmentResult, ValidationCheck

FINAL_LINE = "fault_A = clip({base} + {plus} - {minus}, 0, 100) = {a}; fault_B = {b}"


def calculate_fault(base_A: int, modifiers: list[Mapping[str, object]]) -> tuple[int, int]:
    """검수한 구조 레코드만 사용한다.

    A = clip(base_A + A측 가감합 - B측 가감합, 0, 100), B = 100 - A.
    수정요소는 {modifier_id, party, delta, code}다. 같은 요소의 중복과 같은 당사자의 같은 코드
    중복(상호배타), 정수가 아닌 가감치, 범위 밖 값은 거절한다.
    """
    if isinstance(base_A, bool) or not isinstance(base_A, int) or not 0 <= base_A <= 100:
        raise ValueError(f"기본 과실은 0~100 정수여야 한다: {base_A!r}")
    seen_ids: set[object] = set()
    seen_codes: set[tuple[object, object]] = set()
    plus = minus = 0
    for item in modifiers:
        party, delta = item.get("party"), item.get("delta")
        if party not in ("A", "B"):
            raise ValueError(f"수정요소 당사자가 A/B가 아니다: {item.get('modifier_id')!r}")
        if isinstance(delta, bool) or not isinstance(delta, int) or not -100 <= delta <= 100:
            raise ValueError(f"가감치가 정수(-100~100)가 아니다: {item.get('modifier_id')!r}")
        modifier_id = item.get("modifier_id")
        if not modifier_id or modifier_id in seen_ids:
            raise ValueError(f"수정요소가 비었거나 중복 적용됐다: {modifier_id!r}")
        seen_ids.add(modifier_id)
        code_key = (party, item.get("code"))
        if item.get("code") and code_key in seen_codes:
            raise ValueError(f"같은 당사자에 같은 수정요소가 두 번 적용됐다: {code_key}")
        seen_codes.add(code_key)
        if party == "A":
            plus += delta
        else:
            minus += delta
    fault_a = min(100, max(0, base_A + plus - minus))
    return fault_a, 100 - fault_a


def calculation_lines(
    base_A: int, base_B: int, modifiers: list[Mapping[str, object]]
) -> tuple[list[str], tuple[int, int]]:
    """계산 로그 줄과 결과. 마지막 줄은 FINAL_LINE 형식이라 check_numerical이 대조한다."""
    fault = calculate_fault(base_A, modifiers)
    plus = sum(int(m["delta"]) for m in modifiers if m["party"] == "A")  # type: ignore[call-overload]
    minus = sum(int(m["delta"]) for m in modifiers if m["party"] == "B")  # type: ignore[call-overload]
    lines = [f"기본 과실 A {base_A} : B {base_B}"]
    lines += [
        f"수정요소 적용 {m['party']} {m['name']} {int(m['delta']):+d} ({m['modifier_id']})"  # type: ignore[call-overload]
        for m in modifiers
    ]
    lines.append(FINAL_LINE.format(base=base_A, plus=plus, minus=minus, a=fault[0], b=fault[1]))
    return lines, fault


def check_numerical(
    result: JudgmentResult,
    base: tuple[int, int] | None = None,
    modifiers: list[Mapping[str, object]] | None = None,
) -> ValidationCheck:
    """0~100·A+B=100·계산 로그 마지막 줄·재계산 일치를 검사한다.

    base·modifiers를 주면 같은 입력으로 다시 계산해 결과와 대조한다(서술/수치 일치).
    """
    reasons: list[str] = []
    fault_a, fault_b = result["fault_A"], result["fault_B"]
    if result["status"] != "draft_ready":
        if fault_a is not None or fault_b is not None:
            reasons.append("초안이 아닌 판정에 과실비율이 들어 있다")
        return {"layer": "numerical", "passed": not reasons, "reasons": reasons}
    for name, value in (("fault_A", fault_a), ("fault_B", fault_b)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            reasons.append(f"{name}가 0~100 정수가 아니다: {value!r}")
    if not reasons and fault_a + fault_b != 100:  # type: ignore[operator]
        reasons.append(f"A+B가 100이 아니다: {fault_a}+{fault_b}")
    if not result["chart_id"] or not result["edition"]:
        reasons.append("도표 ID·적용판이 없다")
    log = result["calculation_log"]
    expected_tail = f"= {fault_a}; fault_B = {fault_b}"
    if not log or not log[-1].startswith("fault_A = clip(") or not log[-1].endswith(expected_tail):
        reasons.append("계산 로그 마지막 줄이 결과와 다르다")
    if base is not None and modifiers is not None and not reasons:
        try:
            lines, recomputed = calculation_lines(base[0], base[1], modifiers)
        except ValueError as exc:
            reasons.append(f"재계산 실패: {exc}")
        else:
            if recomputed != (fault_a, fault_b):
                reasons.append(f"재계산 결과가 다르다: {recomputed} ≠ {(fault_a, fault_b)}")
            if lines != log:
                reasons.append("계산 로그가 재계산 로그와 다르다")
    return {"layer": "numerical", "passed": not reasons, "reasons": reasons}
