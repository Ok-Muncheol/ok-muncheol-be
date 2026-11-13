"""C: 대물·대인 1차 판정 초안. 금액은 이 파일의 코드만 계산한다(LLM 없음).

대물 초안 = 상대 차 견적 수리비 × 우리 과실 (대물 한도 안)
대인 초안(다친 사람마다) = (치료관계비 + 급수별 위자료) × 우리 과실
    → 과실상계 후가 치료관계비에 못 미치면 치료관계비(별표3 1. 가. (2)·(3))
    → 급수별 대인Ⅰ 한도까지 대인Ⅰ, 넘는 금액은 대인Ⅱ

계산하지 않고 "확인 필요"로만 표시: 전손(교환가액)·수리비 한도, 시세하락손해, 대차료·휴차료,
휴업손해, 간병비·그 밖의 손해배상금, 후유장애·사망, 최종 진단, 12~14급 차량운전자 특례.
우리 과실 0%, 2022-12-31 이전 사고의 대인, 12~14급 치료관계비가 대인Ⅰ 한도를 넘는 경우(차량운전자
특례가 실제로 걸림, 운전자가 아니라고 입력하면 계산)는 금액 없이 담당자 확인(needs_review)으로 낸다.
시세하락 구간(출고 후 N년)은 달력 기준(사고일 <= 출고일 + N년)으로 본다.

숫자 자료(급수별 대인Ⅰ 한도·위자료, 시세하락 조건)는 policy_terms의 schedule_value 레코드에서
읽는다. calculation_ready가 true인(PDF와 대조 검수한) 값만 쓰고, 없으면 금액을 내지 않는다.
사고일로 판(accident_date_from·accident_date_to)을 고르고, 사고일이 없으면 최신 판으로 가정한다.
"""

from collections.abc import Iterable, Mapping
from datetime import date

from app.calculation.damages import calculate_damages, received_at
from app.calculation.rules import share
from app.schemas.liability import (
    InjuredPersonInput,
    InjuryResult,
    LiabilityFlag,
    LiabilityResult,
    PropertyDamageInput,
    PropertyDamageResult,
)

DOC = "samsung-personal-auto-2026-11-11"
CLAUSE_PROPERTY_REPAIR = f"{DOC}:payment_standard:별표2:1"  # 수리비용(사고 직전 가액 120% 한도)
CLAUSE_PROPERTY_EXCHANGE = f"{DOC}:payment_standard:별표2:2"  # 교환가액(전손)
CLAUSE_DEPRECIATION = f"{DOC}:payment_standard:별표2:6"  # 자동차시세 하락손해
CLAUSE_INJURY_OVERVIEW = f"{DOC}:payment_standard:별표1:나:개요"  # 대인Ⅰ은 급별 한도 안
CLAUSE_TREATMENT = f"{DOC}:payment_standard:별표1:나:1"  # 치료관계비(12~14급 4주 규정)
CLAUSE_CONSOLATION = f"{DOC}:payment_standard:별표1:나:2"  # 부상 위자료
CLAUSE_LOST_INCOME = f"{DOC}:payment_standard:별표1:나:3"  # 휴업손해
CLAUSE_NURSING = f"{DOC}:payment_standard:별표1:나:4"  # 간병비
CLAUSE_OTHER_INJURY = f"{DOC}:payment_standard:별표1:나:5"  # 그 밖의 손해배상금
CLAUSE_LIMIT_TABLE = f"{DOC}:cited_schedule:자배법시행령별표1"  # 상해 구분과 한도금액
OFFSET_2023 = "2023-01-01"
CLAUSE_OFFSET_FROM_2023 = f"{DOC}:payment_standard:별표3:1:from-2023-01-01"
CLAUSE_OFFSET_UNTIL_2022 = f"{DOC}:payment_standard:별표3:1:until-2022-12-31"

VT_LIMIT = "injury_liability_limit_by_grade"
VT_CONSOLATION = "injury_consolation_by_grade"
VT_DEPRECIATION = "depreciation_condition"
VT_TOTAL_LOSS = "total_loss_condition"
VT_OFFSET = "injury_fault_offset_rule"

NOTICE = "1차 판정 초안 — 확실히 계산되는 항목만 계산했고, 나머지는 '확인 필요'로 표시했습니다."
NO_DATE = "사고일 미입력 — 최신 판 기준으로 가정"
ZERO_FAULT = "우리 과실 0% — 배상책임 여부를 담당자가 먼저 확인"
OLD_OFFSET = "2022-12-31 이전 사고 — 별표3 이전 판(대인Ⅰ 한도 문구 없음) 기준이라 담당자 확인"
MINOR_DRIVER_HOLD = (
    "12~14급 차량운전자 특례 확인 — 치료관계비가 대인Ⅰ 한도를 넘음(별표3 1.가.(3) 단서)"
)


def _flag(key: str, label: str, detail: str, *clauses: str) -> LiabilityFlag:
    return {"key": key, "label": label, "detail": detail, "clause_ids": list(clauses)}


def _won(v: int) -> str:
    return f"{v:,}원"


class ScheduleValues:
    """schedule_value 레코드 body 목록에서 계산값을 고른다. 검수한 값만(calculation_ready)."""

    def __init__(self, records: Iterable[Mapping[str, object]]) -> None:
        self.records = [dict(r) for r in records]

    def pick(
        self, value_type: str, accident_date: str | None, **match: object
    ) -> tuple[dict | None, list[str]]:
        """(레코드, 가정·사유). 레코드가 없으면 None과 사유."""
        rows = [
            r
            for r in self.records
            if r.get("value_type") == value_type and all(r.get(k) == v for k, v in match.items())
        ]
        ready = [r for r in rows if r.get("calculation_ready") is True]
        if not ready:
            why = "검수 전 값만 있음 — 계산하지 않음" if rows else "자료 없음"
            return None, [f"{value_type}({match}): {why}"]
        if accident_date is None:
            latest = max(ready, key=lambda r: str(r.get("accident_date_from") or ""))
            same = {r.get("amount_krw") for r in ready} == {latest.get("amount_krw")}
            return latest, [] if same else [NO_DATE]
        hits = [
            r
            for r in ready
            if str(r.get("accident_date_from") or "") <= accident_date
            and (
                r.get("accident_date_to") in (None, "")
                or accident_date <= str(r["accident_date_to"])
            )
        ]
        if len(hits) != 1:
            return None, [
                f"{value_type}({match}): 사고일 {accident_date}에 맞는 판이 {len(hits)}개"
            ]
        return hits[0], []

    def ready_ids(self, value_type: str, accident_date: str | None = None) -> list[str]:
        """근거로 붙일 검수 레코드 ID(사고일이 있으면 그 판만)."""
        return [
            str(r["record_id"])
            for r in self.records
            if r.get("value_type") == value_type
            and r.get("calculation_ready") is True
            and (
                accident_date is None
                or (
                    str(r.get("accident_date_from") or "") <= accident_date
                    and (
                        r.get("accident_date_to") in (None, "")
                        or accident_date <= str(r["accident_date_to"])
                    )
                )
            )
        ]


def _years_between(start: str, end: str) -> float:
    """표시용 대략 연수. 구간 판정은 _within_years(달력 기준)로 한다."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return (b - a).days / 365.25


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2월 29일 출고 → 그해 2월 28일
        return d.replace(year=d.year + years, day=28)


def _within_years(release: str, accident: str, years: int) -> bool:
    """사고일이 출고 후 years년 이하인가(달력 기준: 사고일 <= 출고일 + years년)."""
    return date.fromisoformat(accident) <= _add_years(date.fromisoformat(release), years)


def _depreciation_flag(
    repair: int, inp: PropertyDamageInput, accident_date: str | None, values: ScheduleValues
) -> LiabilityFlag | None:
    value, release = inp.get("vehicle_value"), inp.get("release_date")
    label = "시세하락손해 대상 가능"
    if value is None or release is None or accident_date is None:
        missing = [
            n
            for n, v in (("차량가액", value), ("출고일", release), ("사고일", accident_date))
            if v is None
        ]
        return _flag(
            "depreciation",
            "시세하락손해 확인 필요",
            f"{'·'.join(missing)} 미입력 — 시세하락손해 대상 여부를 보지 못함",
            CLAUSE_DEPRECIATION,
            *values.ready_ids(VT_DEPRECIATION),
        )
    age = _years_between(release, accident_date)
    rules = [
        r
        for r in values.records
        if r.get("value_type") == VT_DEPRECIATION and r.get("calculation_ready") is True
    ]
    for r in rules:
        # 구간은 "출고 후 lo년 초과 hi년 이하"(첫 구간은 lo=0이라 0년부터). 달력 기준
        lo, hi = int(r.get("age_years_min") or 0), r.get("age_years_max")
        in_band = (lo == 0 or not _within_years(release, accident_date, lo)) and (
            hi is None or _within_years(release, accident_date, int(hi))
        )
        ratio = r.get("repair_ratio_percent_min")
        if in_band and ratio is not None and repair * 100 > value * ratio:
            return _flag(
                "depreciation",
                label,
                f"출고 후 {age:.1f}년, 수리비가 차량가액의 {ratio}% 초과 — 인정 비율 "
                f"{r.get('percent')}%(수리비 기준) 검토",
                CLAUSE_DEPRECIATION,
                str(r["record_id"]),
            )
    if rules:
        return None  # 검수한 조건에 맞지 않음 → 대상 아님(표시 없음)
    # 조건 레코드 전: 원문 문턱(5년·20%)으로 표시만
    if _within_years(release, accident_date, 5) and repair * 5 > value:
        return _flag(
            "depreciation",
            label,
            f"출고 후 {age:.1f}년, 수리비가 차량가액의 20% 초과 — 대상 조건 검토(조건 값 검수 전)",
            CLAUSE_DEPRECIATION,
        )
    return None


def calculate_property(
    documents: list[dict[str, object]],
    inp: PropertyDamageInput,
    insured_fault: int | None,
    *,
    accident_date: str | None = None,
    values: ScheduleValues | None = None,
) -> PropertyDamageResult:
    values = values or ScheduleValues([])
    damage = calculate_damages(documents, party_id=inp["counterparty_id"])
    repair = damage["amount"] if damage["status"] == "confirmed" else None
    flags: list[LiabilityFlag] = [
        _flag(
            "rental",
            "대차료·휴차료 별도 확인",
            "대차(렌트) 또는 휴차료 청구가 있으면 별도 산정",
            f"{DOC}:payment_standard:별표2:3",
            f"{DOC}:payment_standard:별표2:4",
        ),
    ]
    assumptions: list[str] = []
    reasons = list(damage["reasons"])
    clauses = [CLAUSE_PROPERTY_REPAIR, _offset_clause(accident_date)]
    out: PropertyDamageResult = {
        "status": "needs_input",
        "counterparty_id": inp["counterparty_id"],
        "repair_cost": repair,
        "amount": None,
        "limit_applied": False,
        "formula": "상대 차 수리비 × 우리 과실",
        "flags": flags,
        "assumptions": assumptions,
        "reasons": reasons,
        "clause_ids": clauses,
        "source_document_ids": list(damage["source_document_ids"]),
    }
    if repair is None:
        return out
    value = inp.get("vehicle_value")
    if value is None:
        flags.append(
            _flag(
                "total_loss",
                "전손 여부 확인 필요",
                "차량가액 미입력 — 수리비가 사고 직전 가액을 넘는지(교환가액) 보지 못함",
                CLAUSE_PROPERTY_EXCHANGE,
                *values.ready_ids(VT_TOTAL_LOSS),
            )
        )
    elif repair > value:
        flags.append(
            _flag(
                "total_loss",
                "전손 가능, 교환가액 검토 필요",
                f"수리비 {_won(repair)} > 사고 직전 차량가액 {_won(value)}",
                CLAUSE_PROPERTY_EXCHANGE,
                CLAUSE_PROPERTY_REPAIR,
                *values.ready_ids(VT_TOTAL_LOSS),
            )
        )
    dep = _depreciation_flag(repair, inp, accident_date, values)
    if dep:
        flags.append(dep)
    if insured_fault is None:
        reasons.append("우리 과실 미정 — 과실비율 확정 후 산정")
        return out
    if insured_fault == 0:
        reasons.append(ZERO_FAULT)
        flags.clear()  # 배상책임부터 확인 — 전손·시세하락·대차료는 그 뒤의 일
        out["status"] = "needs_review"
        return out
    amount = share(repair, insured_fault)
    formula = f"{_won(repair)} × {insured_fault}% = {_won(amount)}"
    limit = inp.get("property_limit")
    if limit is None:
        assumptions.append("대물 한도 미입력 — 한도 검사 생략")
    elif amount > limit:
        amount, out["limit_applied"] = limit, True
        formula += f" → 대물 한도 {_won(limit)}"
    out.update(status="draft", amount=amount, formula=formula)
    return out


def _offset_clause(accident_date: str | None) -> str:
    if accident_date is not None and accident_date < OFFSET_2023:
        return CLAUSE_OFFSET_UNTIL_2022
    return CLAUSE_OFFSET_FROM_2023


def _injury_flags(p: InjuredPersonInput, accident_date: str | None) -> list[LiabilityFlag]:
    grade = p["grade"]
    flags = [
        _flag(
            "final_diagnosis",
            "최종 진단 확인",
            "진단 급수가 바뀌면 위자료·대인Ⅰ 한도가 달라짐",
            CLAUSE_LIMIT_TABLE,
        ),
        _flag(
            "lost_income",
            "휴업손해: 소득 자료 확인 필요",
            "수입 감소를 관계 서류로 증명한 경우에만 실제 감소액의 85%",
            CLAUSE_LOST_INCOME,
        ),
        _flag(
            "disability_death",
            "후유장애·사망: 범위 밖, 별도 산정",
            "1차 판정은 부상(치료관계비·위자료)만 계산",
            f"{DOC}:payment_standard:별표1:다:1",
        ),
        _flag(
            "other_injury",
            "식대·통원비 등 그 밖의 손해배상금 미포함",
            "입원 식대·통원 교통비는 계산하지 않음",
            CLAUSE_OTHER_INJURY,
        ),
    ]
    if grade is not None and grade <= 5:
        flags.append(
            _flag(
                "nursing",
                "간병비 확인 필요",
                f"{grade}급 — 증빙이 있으면 간병비 인정 대상",
                CLAUSE_NURSING,
            )
        )
    if grade is not None and grade >= 12:
        flags.append(
            _flag(
                "minor_treatment",
                "12~14급 치료관계비 기간 확인",
                "상해일부터 4주를 넘는 치료는 진단서 소견 범위 안만 인정",
                CLAUSE_TREATMENT,
            )
        )
        if (accident_date is None or accident_date >= OFFSET_2023) and p.get(
            "is_driver"
        ) is not False:
            flags.append(
                _flag(
                    "minor_driver",
                    "12~14급 차량운전자 특례 확인",
                    "치료관계비가 대인Ⅰ 한도를 넘으면 과실상계 없이 우선 보상 후 초과액의 "
                    "피해자 과실분 청구(운전자 여부 확인)",
                    CLAUSE_OFFSET_FROM_2023,
                )
            )
    return flags


def calculate_injury(
    p: InjuredPersonInput,
    insured_fault: int | None,
    *,
    accident_date: str | None = None,
    values: ScheduleValues | None = None,
) -> InjuryResult:
    values = values or ScheduleValues([])
    grade, treatment = p["grade"], p["treatment_cost"]
    out: InjuryResult = {
        "status": "needs_input",
        "person_id": p["person_id"],
        "label": p.get("label"),
        "grade": grade,
        "treatment_cost": treatment,
        "consolation": None,
        "damage": None,
        "after_offset": None,
        "treatment_guarantee": False,
        "bodily_1_limit": None,
        "bodily_1": None,
        "bodily_2": None,
        "formula": "(치료관계비 + 급수별 위자료) × 우리 과실 → 대인Ⅰ 한도 / 대인Ⅱ",
        "flags": _injury_flags(p, accident_date),
        "assumptions": [],
        "reasons": [],
        "clause_ids": [CLAUSE_CONSOLATION, CLAUSE_INJURY_OVERVIEW, _offset_clause(accident_date)],
    }
    if grade is None or not 1 <= grade <= 14:
        out["reasons"].append("상해 급수 미입력(1~14급) — 담당자 입력 필요")
    if treatment is None:
        out["reasons"].append("치료관계비 미입력 — 담당자 입력 필요")
    if insured_fault is None:
        out["reasons"].append("우리 과실 미정 — 과실비율 확정 후 산정")
    if out["reasons"]:
        return out
    if accident_date is None:
        out["assumptions"].append(NO_DATE)
    consolation_row, why_c = values.pick(VT_CONSOLATION, accident_date, grade=grade)
    limit_row, why_l = values.pick(VT_LIMIT, accident_date, grade=grade)
    for why in why_c + why_l:
        (out["assumptions"] if why == NO_DATE else out["reasons"]).append(why)
    out["assumptions"] = list(dict.fromkeys(out["assumptions"]))
    if consolation_row is None or limit_row is None:
        return out
    consolation, limit = int(consolation_row["amount_krw"]), int(limit_row["amount_krw"])
    out["clause_ids"] += [str(consolation_row["record_id"]), str(limit_row["record_id"])]
    out["clause_ids"] += values.ready_ids(VT_OFFSET, accident_date or OFFSET_2023)
    old = accident_date is not None and accident_date < OFFSET_2023
    # 12~14급 차량운전자 특례는 치료관계비가 대인Ⅰ 한도를 넘을 때만 금액이 달라진다 → 그때만 보류
    minor_driver = grade >= 12 and treatment > limit and p.get("is_driver") is not False  # type: ignore[operator]
    if insured_fault == 0 or old or minor_driver:
        why = ZERO_FAULT if insured_fault == 0 else OLD_OFFSET if old else MINOR_DRIVER_HOLD
        out["reasons"].append(why)
        out.update(status="needs_review", consolation=consolation, bodily_1_limit=limit)
        return out
    damage = treatment + consolation  # type: ignore[operator]
    after = share(damage, insured_fault)
    payable = max(after, treatment)  # type: ignore[type-var]
    bodily_1 = min(payable, limit)
    formula = f"({_won(treatment)} + 위자료 {_won(consolation)}) × {insured_fault}% = {_won(after)}"  # type: ignore[arg-type]
    if payable > after:
        formula += f" < 치료관계비 → {_won(payable)}"
    formula += f" → 대인Ⅰ {_won(bodily_1)}(한도 {_won(limit)}) / 대인Ⅱ {_won(payable - bodily_1)}"
    out.update(
        status="draft",
        consolation=consolation,
        damage=damage,
        after_offset=after,
        treatment_guarantee=payable > after,
        bodily_1_limit=limit,
        bodily_1=bodily_1,
        bodily_2=payable - bodily_1,
        formula=formula,
    )
    return out


def calculate_liability(
    documents: list[dict[str, object]],
    *,
    insured_fault: int | None,
    accident_date: str | None,
    property_input: PropertyDamageInput | None,
    injured: list[InjuredPersonInput],
    schedule_values: Iterable[Mapping[str, object]] = (),
) -> LiabilityResult:
    values = ScheduleValues(schedule_values)
    return {
        "insured_fault": insured_fault,
        "accident_date": accident_date,
        "property": None
        if property_input is None
        else calculate_property(
            documents, property_input, insured_fault, accident_date=accident_date, values=values
        ),
        "injuries": [
            calculate_injury(p, insured_fault, accident_date=accident_date, values=values)
            for p in injured
        ],
        "notice": NOTICE,
    }


def _latest(docs: list[dict[str, object]]) -> dict[str, object]:
    return max(docs, key=lambda d: (received_at(d), str(d["document_id"])))


def _report_accident_date(report: Mapping[str, object]) -> str | None:
    for section in report.get("sections") or []:  # type: ignore[attr-defined]
        if section.get("no") == 2 and isinstance(section.get("body"), dict):
            value = section["body"].get("사고 일자")
            return str(value) if value else None
    return None


def liability_from_report(
    report: Mapping[str, object],
    documents: list[dict[str, object]],
    schedule_values: Iterable[Mapping[str, object]] = (),
    *,
    agreed_counterparty_fault: int | None = None,
) -> LiabilityResult | None:
    """보고서(과실비율·당사자 매핑·사고 일자)와 서류로 대물·대인 1차 판정. 입력이 없으면 None.

    입력: 상대 차 견적서(repair_estimate, party_id=상대), property_damage_input, injured_person.
    같은 종류·같은 사람 서류가 여러 번 오면 가장 늦게 받은 것을 쓴다. 우리 과실은 판단의
    보험가입자 쪽 과실(fault_A/B)이다. 협의 과실(9번 구상과 같은 값)이 있으면 우리 과실 =
    100 - 협의 상대 과실. 매핑이 없으면 상대 차·우리 과실을 알 수 없어 금액이 없다.
    """
    mapping = report.get("party_mapping") or {}
    judgment = report.get("judgment") or {}
    insured = mapping.get("insured_party_id")  # type: ignore[union-attr]
    side = next(
        (s for s in ("A", "B") if insured and mapping.get(f"chart_party_{s}_id") == insured),  # type: ignore[union-attr]
        None,
    )
    other = {"A": "B", "B": "A"}.get(side or "")
    counterparty = str(mapping.get(f"chart_party_{other}_id") or "") if other else ""  # type: ignore[union-attr]
    injured_docs = [d for d in documents if d.get("kind") == "injured_person"]
    property_docs = [d for d in documents if d.get("kind") == "property_damage_input"]
    estimates = counterparty and any(
        d.get("kind") == "repair_estimate" and d.get("party_id") == counterparty for d in documents
    )
    if not (injured_docs or property_docs or estimates):
        return None
    fault = judgment.get(f"fault_{side}") if side else None  # type: ignore[union-attr]
    agreed = side is not None and agreed_counterparty_fault is not None
    if agreed:
        fault = 100 - int(agreed_counterparty_fault)  # type: ignore[arg-type]
    property_input: PropertyDamageInput | None = None
    if property_docs or estimates:
        latest = _latest(property_docs) if property_docs else {}
        property_input = {
            "counterparty_id": counterparty,
            "vehicle_value": latest.get("vehicle_value"),  # type: ignore[typeddict-item]
            "release_date": latest.get("release_date"),  # type: ignore[typeddict-item]
            "property_limit": latest.get("property_damage_limit"),  # type: ignore[typeddict-item]
        }
    people: dict[str, list[dict[str, object]]] = {}
    for d in injured_docs:
        people.setdefault(str(d.get("person_id")), []).append(d)
    injured: list[InjuredPersonInput] = []
    for person_id, docs in people.items():
        d = _latest(docs)
        injured.append(
            {
                "person_id": person_id,
                "label": d.get("label"),  # type: ignore[typeddict-item]
                "grade": d.get("grade"),  # type: ignore[typeddict-item]
                "treatment_cost": d.get("treatment_cost"),  # type: ignore[typeddict-item]
                "is_driver": d.get("is_driver"),  # type: ignore[typeddict-item]
            }
        )
    result = calculate_liability(
        documents,
        insured_fault=fault,  # type: ignore[arg-type]
        accident_date=_report_accident_date(report),
        property_input=property_input,
        injured=injured,
        schedule_values=schedule_values,
    )
    result["fault_source"] = "agreed" if agreed else "judgment"
    if not side:
        why = "당사자 매핑 없음 — 우리 차·상대 차를 알 수 없어 금액 없음"
        for part in ([result["property"]] if result["property"] else []) + result["injuries"]:
            part["reasons"].insert(0, why)
    return result
