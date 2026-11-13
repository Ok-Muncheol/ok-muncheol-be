"""대인·대물 1차 계산용 숫자 자료(D) — 약관 별표1·2·3에서 옮긴 `schedule_value` 레코드.

값은 사람이 약관 PDF에서 옮겨 적은 상수다. 레코드를 만들 때마다 코퍼스 부모 원문 텍스트와 다시
대조하고, 숫자·문장이 하나라도 다르면 ValueError를 낸다(별표4 `PASSENGER_REDUCTIONS`와 같은 방식).

- `calculation_ready=True`는 사람이 PDF 쪽 이미지와 대조한 `REVIEWED_RECORD_IDS`에 있는 것만.
- `parent_id`는 null(구조 레코드), 원문 위치는 `source_parent_id`.
- 빌드 연결(김지은 님 `scripts/build_policy_terms.py`)은 한 줄이다:
  `values += injury_property_values.records({r["parent_id"]: r["text"] for r in appendix}, doc)`
"""

import re
from collections.abc import Mapping

# 사람이 약관 PDF 쪽 이미지와 대조한 record_id. 여기에 있는 것만 calculation_ready=True.
# 2026-11-13 옥석현: 52개 전부 PDF(sha256 ca7f8fd3…)와 대조 완료.
REVIEWED_RECORD_IDS: frozenset[str] = frozenset(
    {
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:1급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:1급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:2급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:2급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:3급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:3급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:4급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:4급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:5급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:5급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:6급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:6급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:7급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:7급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:8급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:8급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:9급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:9급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:10급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:10급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:11급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:11급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:12급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:12급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:13급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:13급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:14급:until-2016-03-31",
        "samsung-personal-auto-2026-11-11:별표1-표1-부상한도:14급:from-2016-04-01",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:1급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:2급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:3급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:4급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:5급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:6급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:7급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:8급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:9급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:10급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:11급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:12급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:13급",
        "samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:14급",
        "samsung-personal-auto-2026-11-11:별표2-6-시세하락:1",
        "samsung-personal-auto-2026-11-11:별표2-6-시세하락:2",
        "samsung-personal-auto-2026-11-11:별표2-6-시세하락:3",
        "samsung-personal-auto-2026-11-11:별표2-1-수리비한도",
        "samsung-personal-auto-2026-11-11:별표2-1-수리비한도-130",
        "samsung-personal-auto-2026-11-11:별표2-2-전부손해",
        "samsung-personal-auto-2026-11-11:별표3-1-가-2:until-2022-12-31",
        "samsung-personal-auto-2026-11-11:별표3-1-가-3:until-2022-12-31",
        "samsung-personal-auto-2026-11-11:별표3-1-가-2:from-2023-01-01",
        "samsung-personal-auto-2026-11-11:별표3-1-가-3:from-2023-01-01",
    }
)

CHECK = "machine_matched_source_text"

# ── 별표1 표1(p.99): 대인배상Ⅰ 부상 급별 한도(만 원). 자배법 시행령 별표1(p.106~113)과 같아야 한다.
LIABILITY_EDITIONS = [
    {"key": "until-2016-03-31", "accident_date_from": None, "accident_date_to": "2016-03-31"},
    {"key": "from-2016-04-01", "accident_date_from": "2016-04-01", "accident_date_to": None},
]
LIABILITY_LIMITS_MANWON = {
    1: (2000, 3000),
    2: (1000, 1500),
    3: (1000, 1200),
    4: (900, 1000),
    5: (900, 900),
    6: (500, 700),
    7: (500, 500),
    8: (240, 300),
    9: (240, 240),
    10: (160, 200),
    11: (160, 160),
    12: (80, 120),
    13: (80, 80),
    14: (80, 50),
}
LIABILITY_PAGE = 99
CITED_PAGES = (106, 113)

# ── 별표1 나.2(p.95): 부상 위자료 급별 인정액(만 원).
CONSOLATION_MANWON = {
    1: 200,
    2: 176,
    3: 152,
    4: 128,
    5: 75,
    6: 50,
    7: 40,
    8: 30,
    9: 25,
    10: 20,
    11: 20,
    12: 15,
    13: 15,
    14: 15,
}
CONSOLATION_PAGE = 95

# ── 별표2 6.(p.102): 시세하락손해.
#    「출고 후 min년 초과 max년 이하」, 수리비용이 가액의 20% 「초과」.
DEPRECIATION_PAGE = 102
DEPRECIATION_CONDITION = (
    "사고로 인한 자동차(출고 후 5년 이하인 자동차에 한함)의 수리비용이 "
    "사고 직전 자동차가액의 20%를 초과하는 경우"
)
DEPRECIATION_ROWS = [
    (0, 1, 20, "(1) 출고 후 1년 이하인 자동차 : 수리비용의 20%"),
    (1, 2, 15, "(2) 출고 후 1년 초과 2년 이하인 자동차 : 수리비용의 15%"),
    (2, 5, 10, "(3) 출고 후 2년 초과 5년 이하인 자동차 : 수리비용의 10%"),
]
DEPRECIATION_REPAIR_RATIO_MIN = 20

# ── 별표2 1.(3)·2.(p.100): 수리비 한도 120%/130%, 전부손해.
TOTAL_LOSS_PAGE = 100
TOTAL_LOSS_ROWS = [
    {
        "key": "1-수리비한도",
        "item": "1",
        "rule": "repair_cost_limit",
        "compare_amount": "수리비 및 열처리 도장료의 합계액",
        "compare_basis": "피해물의 사고 직전가액",
        "ratio_percent": 120,
        "comparison": "<=",
        "condition_text": (
            "수리비 및 열처리 도장료의 합계액은 피해물의 사고 직전가액의 120%를 한도로 지급함."
        ),
    },
    {
        "key": "1-수리비한도-130",
        "item": "1",
        "rule": "repair_cost_limit_extended",
        "compare_amount": "수리비 및 열처리 도장료의 합계액",
        "compare_basis": "피해물의 사고 직전가액",
        "ratio_percent": 130,
        "comparison": "<=",
        "condition_text": (
            "다만, 피해물이 다음 중 어느 하나에 해당하는 경우에는 130%를 한도로 함. "
            "(가) 내용연수(*2)가 지난 경우 "
            "(나) 「여객자동차 운수사업법」 제84조 제2항에 의한 차량충당연한을 적용 받는 "
            "승용자동차나 승합자동차 "
            "(다) 「화물자동차 운수사업법」 제57조 제1항에 의한 차량충당연한을 적용 받는 "
            "화물자동차"
        ),
    },
    {
        "key": "2-전부손해",
        "item": "2",
        "rule": "total_loss",
        "compare_amount": "수리비용",
        "compare_basis": "피해물의 사고 직전 가액",
        "ratio_percent": 100,
        "comparison": ">",
        "condition_text": (
            "피해물이 다음 중 어느 하나에 해당하는 경우 "
            "(1) 수리비용이 피해물의 사고 직전 가액을 초과하여 수리하지 않고 폐차 하는 경우 "
            "(2) 원상회복이 불가능한 경우"
        ),
        "amount_text": "(1) 사고 직전 피해물의 가액 상당액",
    },
]

# ── 별표3 1.가.(2)·(3)(p.103): 대인 과실상계 치료비 특례. 두 판(2022-12-31 이전/2023-01-01 이후).
FAULT_OFFSET_PAGE = 103
OFFSET_EDITIONS = [
    {"key": "until-2022-12-31", "accident_date_from": None, "accident_date_to": "2022-12-31"},
    {"key": "from-2023-01-01", "accident_date_from": "2023-01-01", "accident_date_to": None},
]
_OFFSET_2_UNTIL = (
    "(2) 「대인배상Ⅰ」에서 사망보험금은 위 (1)에 의하여 상계한 후의 금액이 2,000만원에 미달하면 "
    "2,000만원을 보상하며, 부상보험금의 경우 위 (1)에 의하여 상계한 후의 금액이 치료관계비와 "
    "간병비의 합산액에 미달하면 치료관계비(입원환자 식대를 포함)와 간병비를 보상함."
)
_OFFSET_2_FROM = (
    "(2) 「대인배상Ⅰ」에서 사망보험금은 위 (1)에 의하여 상계한 후의 금액이 2,000만원에 미달하면 "
    "2,000만원을 보상하며, 부상보험금의 경우 위 (1)에 의하여 상계한 후의 금액이 치료관계비와 "
    "간병비의 합산액에 미달하면 「대인배상Ⅰ」 한도 내에서 치료관계비(입원환자 식대를 포함)와 "
    "간병비를 보상함."
)
_OFFSET_3 = (
    "(3) 「대인배상Ⅱ」 또는 「무보험자동차에 의한 상해」에서 사망보험금, 부상보험금 및 "
    "후유장애보험금을 합산한 금액을 기준으로 위 (1)에 의하여 상계한 후의 금액이 치료관계비와 "
    "간병비의 합산액에 미달하면 치료관계비(입원환자 식대를 포함하며, 「대인배상Ⅰ」에서 지급될 수 "
    "있는 금액을 공제)와 간병비를 보상함."
)
_OFFSET_3_DRIVER = (
    "다만, 차량운전자(*1)가 ‘자동차손해배상보장법 시행령’ <별표1> 에서 정한 상해급별 구분 중 "
    "12급 내지 14급의 상해를 입은 경우 위 (1)에 의하여 상계하기 전의 치료관계비가 「대인배상Ⅰ」 "
    "한도를 초과할 경우 보험회사는 과실상계 없이 우선 보상한 후, 그 초과액에 대하여 피해자 측의 "
    "과실비율에 해당하는 금액을 청구할 수 있음."
)
FAULT_OFFSET_ROWS = [
    # (판 key, 절, 담보, 원문 문장, 대인Ⅰ 한도 내, 대인Ⅰ 지급분 공제, 12~14급 차량운전자 특례)
    ("until-2022-12-31", "2", "대인배상Ⅰ", _OFFSET_2_UNTIL, False, False, False),
    ("until-2022-12-31", "3", "대인배상Ⅱ", _OFFSET_3, False, True, False),
    ("from-2023-01-01", "2", "대인배상Ⅰ", _OFFSET_2_FROM, True, False, False),
    ("from-2023-01-01", "3", "대인배상Ⅱ", _OFFSET_3 + " " + _OFFSET_3_DRIVER, False, True, True),
]


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _parent_ids(document_id: str) -> dict[str, str]:
    base = f"{document_id}:payment_standard"
    return {
        "limit": f"{base}:별표1:표1",
        "cited": f"{document_id}:cited_schedule:자배법시행령별표1",
        "consolation": f"{base}:별표1:나:2",
        "repair": f"{base}:별표2:1",
        "total_loss": f"{base}:별표2:2",
        "depreciation": f"{base}:별표2:6",
        "offset:until-2022-12-31": f"{base}:별표3:1:until-2022-12-31",
        "offset:from-2023-01-01": f"{base}:별표3:1:from-2023-01-01",
    }


def _text(parents: Mapping[str, str], parent_id: str) -> str:
    if parent_id not in parents:
        raise ValueError(f"부모 원문이 없다: {parent_id}")
    return parents[parent_id]


def _manwon(text: str) -> int:
    return int(text.replace(",", ""))


def _check_limit_table(text: str) -> None:
    """p.99 표1: 「N급 A만 원 B만 원」(이전·이후 순서)이 상수와 같아야 한다."""
    header = _compact(text)
    if "2016.3.312016.4.1" not in header or "이전사고이후사고" not in header:
        raise ValueError("별표1 표1: 2016. 3. 31 이전/2016. 4. 1 이후 열 머리가 없다")
    found: dict[int, tuple[int, int]] = {}
    for grade, before, after in re.findall(
        r"(\d+)급\s+(\d[\d,]*)만\s*원\s+(\d[\d,]*)만\s*원", text
    ):
        found[int(grade)] = (_manwon(before), _manwon(after))
    if found != LIABILITY_LIMITS_MANWON:
        diff = sorted(
            g
            for g in set(found) | set(LIABILITY_LIMITS_MANWON)
            if found.get(g) != LIABILITY_LIMITS_MANWON.get(g)
        )
        raise ValueError(f"별표1 표1 급별 한도가 원문과 다르다: {diff}급")


def _check_cited_table(text: str) -> None:
    """자배법 시행령 별표1: 급 첫 줄(또는 바로 윗줄)에 「이전 이후」 숫자 두 개(만 원)."""
    if "상해구분별한도금액" not in _compact(text):
        raise ValueError("자배법 시행령 별표1: 「상해 구분별 한도금액」 표가 없다")
    lines = [line.strip() for line in text.splitlines()]
    for grade, (before, after) in LIABILITY_LIMITS_MANWON.items():
        start = next((i for i, line in enumerate(lines) if line.startswith(f"{grade}급 ")), None)
        if start is None:
            raise ValueError(f"자배법 시행령 별표1: {grade}급 줄이 없다")
        pair = f"{before:,} {after:,}"
        same_line = re.search(r"(\d[\d,]*) (\d[\d,]*)$", lines[start])
        above = lines[start - 1] if start else ""
        if not (
            (same_line and same_line.group(0) == pair)
            or (re.fullmatch(r"\d[\d,]* \d[\d,]*", above) and above == pair)
        ):
            raise ValueError(f"자배법 시행령 별표1 {grade}급 한도가 원문과 다르다: {pair}")


def _check_consolation(text: str) -> None:
    if _compact("책임보험상해 구분에 따라") not in _compact(text):
        raise ValueError("별표1 나.2: 급별 인정액 문장이 없다")
    found = {int(g): int(a) for g, a in re.findall(r"(\d+)급\s+(\d+)만\s*원", text)}
    if found != CONSOLATION_MANWON:
        diff = sorted(
            g
            for g in set(found) | set(CONSOLATION_MANWON)
            if found.get(g) != CONSOLATION_MANWON.get(g)
        )
        raise ValueError(f"별표1 나.2 부상 위자료가 원문과 다르다: {diff}급")


def _check_sentence(text: str, sentence: str, where: str) -> None:
    if _compact(sentence) not in _compact(text):
        raise ValueError(f"{where}: 원문에 없는 문장 — {sentence[:40]}…")


def verify(parents: Mapping[str, str], document_id: str) -> None:
    """상수 전부를 부모 원문과 대조한다. 하나라도 다르면 ValueError."""
    ids = _parent_ids(document_id)
    _check_limit_table(_text(parents, ids["limit"]))
    _check_cited_table(_text(parents, ids["cited"]))
    _check_consolation(_text(parents, ids["consolation"]))
    depreciation = _text(parents, ids["depreciation"])
    _check_sentence(depreciation, DEPRECIATION_CONDITION, "별표2 6.")
    for _, _, _, row in DEPRECIATION_ROWS:
        _check_sentence(depreciation, row, "별표2 6.")
    for row in TOTAL_LOSS_ROWS:
        source = ids["repair"] if row["item"] == "1" else ids["total_loss"]
        _check_sentence(_text(parents, source), row["condition_text"], f"별표2 {row['item']}.")
        if "amount_text" in row:
            _check_sentence(_text(parents, source), row["amount_text"], f"별표2 {row['item']}.")
    for edition, _, _, sentence, *_ in FAULT_OFFSET_ROWS:
        _check_sentence(_text(parents, ids[f"offset:{edition}"]), sentence, f"별표3 1.({edition})")
    # 2022-12-31 이전 판 (2)에는 「대인배상Ⅰ 한도 내에서」가 없어야 한다(두 판 뒤바뀜 방지).
    if _compact("「대인배상Ⅰ」 한도 내에서") in _compact(
        _text(parents, ids["offset:until-2022-12-31"])
    ):
        raise ValueError("별표3 1.(2022-12-31 이전)에 2023년 판 문장이 있다")


def _base(doc: dict, record_id: str, **fields) -> dict:
    return {
        "record_id": record_id,
        "parent_id": None,
        "document": doc,
        **fields,
        "check": CHECK,
        "review_status": "reviewed_against_pdf"
        if record_id in REVIEWED_RECORD_IDS
        else "unreviewed",
        "calculation_ready": record_id in REVIEWED_RECORD_IDS,
    }


def build(doc: dict) -> list[dict]:
    """대조 없이 상수로 레코드를 만든다. 코퍼스용으로는 `records()`를 쓴다."""
    document_id = doc["document_id"]
    ids = _parent_ids(document_id)
    out = []
    for i, edition in enumerate(LIABILITY_EDITIONS):
        for grade, amounts in LIABILITY_LIMITS_MANWON.items():
            manwon = amounts[i]
            out.append(
                _base(
                    doc,
                    f"{document_id}:별표1-표1-부상한도:{grade}급:{edition['key']}",
                    schedule="별표1",
                    table="별표1 표1 대인배상Ⅰ 부상 급별 한도",
                    value_type="injury_liability_limit_by_grade",
                    coverage="대인배상Ⅰ",
                    grade=grade,
                    amount_krw=manwon * 10_000,
                    amount_raw=f"{manwon:,}만 원",
                    accident_date_from=edition["accident_date_from"],
                    accident_date_to=edition["accident_date_to"],
                    source_parent_id=ids["limit"],
                    source_pdf_page=LIABILITY_PAGE,
                    cross_check_parent_id=ids["cited"],
                    cross_check_pdf_pages=list(CITED_PAGES),
                )
            )
    for grade, manwon in CONSOLATION_MANWON.items():
        out.append(
            _base(
                doc,
                f"{document_id}:별표1-나-2-부상위자료:{grade}급",
                schedule="별표1",
                table="별표1 나.2 부상 위자료",
                value_type="injury_consolation_by_grade",
                grade=grade,
                amount_krw=manwon * 10_000,
                amount_raw=f"{manwon}만 원",
                accident_date_from=None,
                accident_date_to=None,
                source_parent_id=ids["consolation"],
                source_pdf_page=CONSOLATION_PAGE,
            )
        )
    for i, (low, high, percent, row) in enumerate(DEPRECIATION_ROWS, start=1):
        out.append(
            _base(
                doc,
                f"{document_id}:별표2-6-시세하락:{i}",
                schedule="별표2",
                table="별표2 6. 자동차 시세하락손해",
                value_type="depreciation_condition",
                age_years_min=low,
                age_years_max=high,
                age_years_min_exclusive=low > 0,
                age_years_max_inclusive=True,
                repair_ratio_percent_min=DEPRECIATION_REPAIR_RATIO_MIN,
                repair_ratio_percent_min_exclusive=True,
                percent=percent,
                condition_text=DEPRECIATION_CONDITION,
                amount_text=row,
                boundary_note=(
                    f"출고 후 {low}년 초과 {high}년 이하" if low else f"출고 후 {high}년 이하"
                )
                + f", 수리비용 ÷ 사고 직전 자동차가액 > {DEPRECIATION_REPAIR_RATIO_MIN}%"
                + f"(정확히 {DEPRECIATION_REPAIR_RATIO_MIN}%는 해당 없음)",
                accident_date_from=None,
                accident_date_to=None,
                source_parent_id=ids["depreciation"],
                source_pdf_page=DEPRECIATION_PAGE,
            )
        )
    for row in TOTAL_LOSS_ROWS:
        out.append(
            _base(
                doc,
                f"{document_id}:별표2-{row['key']}",
                schedule="별표2",
                table="별표2 1. 수리비 한도 · 2. 전부손해",
                value_type="total_loss_condition",
                **{k: v for k, v in row.items() if k not in {"key", "item"}},
                accident_date_from=None,
                accident_date_to=None,
                source_parent_id=ids["repair" if row["item"] == "1" else "total_loss"],
                source_pdf_page=TOTAL_LOSS_PAGE,
            )
        )
    editions = {e["key"]: e for e in OFFSET_EDITIONS}
    for key, clause, coverage, sentence, within_i, deduct_i, driver in FAULT_OFFSET_ROWS:
        out.append(
            _base(
                doc,
                f"{document_id}:별표3-1-가-{clause}:{key}",
                schedule="별표3",
                table="별표3 1.가. 과실상계의 방법",
                value_type="injury_fault_offset_rule",
                clause=f"1.가.({clause})",
                coverage=coverage,
                floor_items=["치료관계비", "간병비"],
                treatment_includes_inpatient_meals=True,
                within_coverage_i_limit=within_i,
                deduct_coverage_i_payable=deduct_i,
                driver_grade_12_14_exception=driver,
                condition_text=sentence,
                accident_date_from=editions[key]["accident_date_from"],
                accident_date_to=editions[key]["accident_date_to"],
                source_parent_id=ids[f"offset:{key}"],
                source_pdf_page=FAULT_OFFSET_PAGE,
            )
        )
    return out


def records(parents: Mapping[str, str], doc: dict) -> list[dict]:
    """부모 원문({parent_id: text})과 대조한 뒤 레코드 52개를 낸다. 다르면 ValueError."""
    verify(parents, doc["document_id"])
    return build(doc)
