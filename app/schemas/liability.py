"""C: 대물·대인 1차 판정 초안 계약. 금액은 원 단위 정수, 모르는 값은 null(0원으로 채우지 않음).

1차 판정은 담당자를 돕는 초안이다. 확실히 계산할 수 있는 것만 계산하고, 나머지는 "확인 필요"
표시(flags)로 남긴다. 숫자마다 근거(record_id·조항 ID)를 붙이고, 입력이 없어 기본값을 쓴 곳은
assumptions에 적는다. 계산 코드는 app/calculation/liability.py.
"""

from typing import Literal, NotRequired, TypedDict

LiabilityStatus = Literal[
    "draft",  # 초안 금액 있음(가정·확인 필요 표시는 따로)
    "needs_input",  # 견적서·상해 급수·치료비·과실 등 입력이 없어 금액 없음
    "needs_review",  # 입력은 있으나 규칙상 담당자 판단이 먼저(예: 우리 과실 0%)
]


class LiabilityFlag(TypedDict):
    """계산하지 않고 담당자에게 알리는 "확인 필요" 항목."""

    key: str
    label: str
    detail: str
    clause_ids: list[str]


class PropertyDamageInput(TypedDict):
    """대물(상대 차). 견적서는 documents의 repair_estimate 중 party_id가 상대 차인 것."""

    counterparty_id: str
    vehicle_value: NotRequired[int | None]  # 사고 직전 차량가액(있으면)
    release_date: NotRequired[str | None]  # 출고일 ISO 날짜(있으면)
    property_limit: NotRequired[int | None]  # 대물배상 가입금액(있으면)


class InjuredPersonInput(TypedDict):
    """대인(상대 차 탑승자) 1명."""

    person_id: str
    label: NotRequired[str | None]  # 보고서 표시 이름(예: "상대 운전자")
    grade: int | None  # 자배법 시행령 별표1 상해 급수(1~14). 담당자 입력
    treatment_cost: int | None  # 치료관계비(원). 담당자 입력
    is_driver: NotRequired[bool | None]  # 차량운전자 여부(12~14급 특례 확인용)


class PropertyDamageResult(TypedDict):
    status: LiabilityStatus
    counterparty_id: str
    repair_cost: int | None  # 상대 차 견적 수리비
    amount: int | None  # 대물 초안 = 수리비 × 우리 과실(한도 안)
    limit_applied: bool  # 대물 한도로 깎였는지
    formula: str
    flags: list[LiabilityFlag]
    assumptions: list[str]
    reasons: list[str]  # 금액이 없거나 낮아진 사유(견적서 상태 등)
    clause_ids: list[str]
    source_document_ids: list[str]


class InjuryResult(TypedDict):
    status: LiabilityStatus
    person_id: str
    label: NotRequired[str | None]
    grade: int | None
    treatment_cost: int | None
    consolation: int | None  # 급수별 부상 위자료(별표1 나 2)
    damage: int | None  # 치료관계비 + 위자료
    after_offset: int | None  # 과실상계 후 = 손해 × 우리 과실
    treatment_guarantee: bool  # 과실상계 후가 치료관계비에 못 미쳐 치료관계비를 보상했는지(별표3)
    bodily_1_limit: int | None  # 급수별 대인Ⅰ 한도
    bodily_1: int | None  # 대인Ⅰ 지급 초안
    bodily_2: int | None  # 대인Ⅱ 지급 초안(대인Ⅰ 초과분)
    formula: str
    flags: list[LiabilityFlag]
    assumptions: list[str]
    reasons: list[str]
    clause_ids: list[str]  # 조항 ID + 쓴 schedule_value record_id


class LiabilityResult(TypedDict):
    insured_fault: int | None  # 우리(보험가입자) 과실 %
    # judgment = 판단 과실, agreed = 담당자가 넣은 협의 과실(9번 구상과 같은 값, 우리 = 100 - 상대)
    fault_source: NotRequired[Literal["judgment", "agreed"]]
    accident_date: str | None
    property: PropertyDamageResult | None
    injuries: list[InjuryResult]
    notice: str  # "1차 판정 초안 — …"
