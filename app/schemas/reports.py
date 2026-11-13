"""D: 금액은 원 단위 정수. 미확정을 0원으로 채우지 않는다.

1차 확장(자차·구상) 계약. 기존 필드는 유지하고, 추가 필드는 NotRequired로 덧붙였다.
FE 타입(`src/lib/api/types.ts`)에도 같은 변경을 반영해야 한다(아래 '계약 변경' 표시).
"""

from typing import Literal, NotRequired, TypedDict

from app.schemas.common import VersionRef
from app.schemas.facts import PartyMapping
from app.schemas.judgment import JudgmentResult
from app.schemas.liability import LiabilityResult

# 계약 변경: needs_contract_review, not_payable 추가 (기존 3개 값 유지)
CalculationStatus = Literal[
    "estimated",  # 1차 미사용 — 참고값 기능 제거, 값은 계약 호환용으로 유지
    "needs_documents",  # 견적서 없음·OCR 미확인 → 금액 미확정(null)
    "needs_contract_review",  # 계약·면책·당사자 매핑 확인 필요
    "not_payable",  # 자차 미가입·면책·보험기간 밖
    "confirmed",  # 확정 견적·계약 확인 완료
]


# ── 입력 서류 (calculate_damages / calculate_insurance의 documents) ──────────
class RepairEstimateDoc(TypedDict):
    kind: Literal["repair_estimate"]
    document_id: str
    party_id: str  # 보통 insured_party_id
    amount: int | None
    # 계약 변경: pdf_extract 추가 — 서식 텍스트 PDF에서 자동 추출, 담당자 확인 없이 계산에
    # 사용(마지막 담당자 확인 완료에서 확인)
    read_by: Literal["manual", "ocr", "pdf_extract"]
    verification_status: Literal["unverified", "human_confirmed"]
    received_at: str  # ISO 8601
    storage_path: str | None
    # 계약 변경: 아래는 PDF 자동 추출(read_by="pdf_extract") 때 채우는 추가 필드
    serial_no: NotRequired[str | None]  # 견적서 일련번호
    issued_on: NotRequired[str | None]  # 발급 년월일 (ISO 날짜)
    estimate_type: NotRequired[Literal["insurance", "general"] | None]  # 견적구분 보험/일반
    parts: NotRequired[int | None]  # 부품
    labor: NotRequired[int | None]  # 공임
    vat: NotRequired[int | None]  # 부가가치세
    extract_reasons: NotRequired[list[str]]  # 추출 확인 사유(총액 인식 실패·합계 불일치)


class OwnDamageContractDoc(TypedDict):
    """자차 계약 정보. 실제 도입 시 계약 시스템 연동, 프로토타입은 담당자 입력."""

    kind: Literal["own_damage_contract"]
    document_id: str
    policy_id: str
    own_damage_joined: bool | None
    period_start: str
    period_end: str
    insured_amount: int | None  # 보험가입금액
    vehicle_value: int | None  # 사고 당시 차량가액
    deductible_fixed: int | None  # 정액 자기부담금 계약
    deductible_rate: float | None  # 정률 계약 (없으면 규칙 기본값)
    deductible_min: int | None
    deductible_max: int | None
    driver_scope: str | None
    driver_in_scope: bool | None  # 사고 운전자가 운전자 범위 안인지. None=미확인
    terms_edition: str | None  # 약관 적용판 (policy_terms 필터, 사고 일자로 대체 금지)


class EvidenceDoc(TypedDict):
    """증빙(영수증·사진 등). 계약 변경: 지급 건이 아니라 사건에 바로 연결(related_payment_id
    삭제)."""

    kind: Literal["evidence"]
    document_id: str
    doc_type: Literal[
        "repair_invoice",
        "receipt",
        "photo",
        "estimate",
        "repair_estimate",
        "medical_certificate",  # 계약 변경: 진단서(첨부만, 읽지 않음 — 급수는 담당자 입력)
        "medical_bill",  # 계약 변경: 진료비 영수증(첨부만, 읽지 않음)
        "other",
    ]  # repair_estimate: upload_estimate가 자동 등록
    storage_path: str


class InjuredPersonDoc(TypedDict):
    """계약 변경(C): 대인 1명의 담당자 입력. 진단서·영수증을 보고 담당자가 적는다(자동 판독 없음).

    같은 person_id가 여러 번 오면 가장 늦게 받은 것(received_at, document_id 순)을 쓴다.
    치료관계비는 입력 시점까지의 누적 총액이다. 계산은 app/calculation/liability.py."""

    kind: Literal["injured_person"]
    document_id: str
    person_id: str
    label: NotRequired[str | None]  # 보고서 표시 이름(예: "상대 운전자"). 없으면 person_id
    grade: int | None  # 자배법 시행령 별표1 상해 급수 1~14
    treatment_cost: int | None  # 치료관계비 누적 총액(원)
    is_driver: NotRequired[bool | None]  # 차량운전자 여부. null = 모름(12~14급 특례 보류)
    received_at: str  # ISO 8601


class PropertyDamageInputDoc(TypedDict):
    """계약 변경(C): 대물 담당자 입력(모두 선택). 상대 차 견적서는 upload_estimate(party_id=상대)로.

    여러 번 오면 가장 늦게 받은 것을 쓴다. 자차 계약(own_damage_contract)과 따로 둬서 자차
    산정 입력(inputs_hash)을 바꾸지 않는다."""

    kind: Literal["property_damage_input"]
    document_id: str
    property_damage_limit: NotRequired[int | None]  # 우리 계약 대물배상 가입금액
    vehicle_value: NotRequired[int | None]  # 상대 차 사고 직전 차량가액(전손·시세하락 확인)
    release_date: NotRequired[str | None]  # 상대 차 출고일 ISO 날짜(시세하락 확인)
    received_at: str  # ISO 8601


class CounterpartyInfo(TypedDict):
    vehicle_no: str
    insurer: str
    claim_no: str | None


# ── 산정 결과 ────────────────────────────────────────────
class PreCheck(TypedDict):
    """산정 전 필수 검증 1건."""

    key: str
    label: str
    result: Literal[
        "pass", "fail", "unknown", "assumed"
    ]  # assumed: 미입력 → 기본 가정(산정·확인 완료를 막지 않음)
    decided_by: Literal["code", "llm", "handler"]
    detail: str
    clause_ids: list[str]
    # 통과했지만 담당자가 봐야 하는 경고(detail과 같은 문구). 산정 사유에도 들어가 8번에 보인다
    warning: NotRequired[str]


class DamageResult(TypedDict):
    status: Literal[
        "estimated", "needs_documents", "confirmed"
    ]  # estimated: 1차 미사용 — 참고값 기능 제거, 값은 계약 호환용으로 유지
    party_id: str | None
    amount: int | None
    source_document_ids: list[str]
    reasons: list[str]
    # 계약 변경: 손해액 출처 표시(예: "견적서 PDF 자동 추출 (일련번호 …)"), 산정 사유로 넘길 서류
    # 확인 사유
    source_label: NotRequired[str]
    document_reasons: NotRequired[list[str]]


class CalculationResult(TypedDict):
    versions: VersionRef
    status: CalculationStatus
    currency: Literal["KRW"]
    recognized_damage: int | None
    insurance_payment: int | None
    deductible: int | None
    rule_version: str
    source_document_ids: list[str]
    reasons: list[str]
    # 계약 변경: 아래는 추가 필드
    damage: NotRequired[DamageResult]
    loss_type: NotRequired[Literal["partial", "total"] | None]
    counterparty_fault: NotRequired[int | None]
    checks: NotRequired[list[PreCheck]]
    formulas: NotRequired[dict[str, str]]
    clause_ids: NotRequired[list[str]]
    terms_edition: NotRequired[str | None]
    inputs_hash: NotRequired[str]
    # 기본 가정 문구(계약·자기부담금·면책). 계약·면책 가정이 있으면 confirmed가 아님.
    # 자기부담금 기본 규칙은 규칙 값이라 표시만 하고 상태를 낮추지 않는다
    assumptions: NotRequired[list[str]]


class RecoveryAmounts(TypedDict):
    """보고서 9번. 산정 보험금이 나오면 자동으로 채운다(지급 내역 입력 없음)."""

    planned: int | None  # 구상 예정액 = 산정 보험금 × 상대 과실 (원 미만 버림)
    claimed: int | None  # 청구액 — 예정액으로 자동 채움
    recovered: int | None  # 회수액 — 입력 기능 없음, null 유지(후속 회수 기능 자리)
    customer_deductible_review: (
        int | None
    )  # 고객 자기부담금 검토액 = 자기부담금 × 상대 과실 (내부 참고)
    agreed_counterparty_fault: NotRequired[
        int | None
    ]  # 협의로 바꾼 상대 과실(%) — 담당자 입력. None이면 협의 해제 기록
    fault_change_reason: NotRequired[str | None]  # 협의 과실 변경(해제) 이유 — 담당자 입력


class ClaimAmounts(TypedDict):
    """구상 청구 패키지(외부 전달용)의 금액 — 청구 금액만. 고객 자기부담금 검토액은 보고서 9번(내부
    참고)에만 둔다."""

    planned: int | None  # 구상 예정액 = 산정 보험금 × 상대 과실
    claimed: int | None  # 청구액 — 예정액과 같음(자동)
    recovered: int | None  # 회수액 — null(입력 기능 없음)
    agreed_counterparty_fault: NotRequired[int | None]
    fault_change_reason: NotRequired[str | None]


class ReportSection(TypedDict):
    no: int
    title: str
    state: Literal["filled", "empty"]
    body: dict[str, object]
    evidence_ids: list[str]
    empty_reason: str | None


class ReviewReport(TypedDict):
    versions: VersionRef
    judgment: JudgmentResult
    sections: list[dict[str, object]]  # 실제 원소는 ReportSection
    calculation: CalculationResult | None
    recovery: RecoveryAmounts | None
    # 확인 완료 기록. 필드 이름은 유지, 의미는 '담당자 확인 완료'(confirm_report). approved = 확인
    # 완료
    approval_status: Literal["draft", "approved"]
    approved_by: str | None
    approved_at: str | None
    # 계약 변경: 아래는 추가 필드
    created_at: NotRequired[str]
    reason: NotRequired[str]
    supersedes: NotRequired[int | None]
    party_mapping: NotRequired[PartyMapping]
    education_mode: NotRequired[bool]
    # 계약 변경(C): 대물·대인 1차 판정. 입력(상대 견적·대인·대물 입력)이 없는 사건은 키가 없다
    liability: NotRequired[LiabilityResult | None]


class ClaimRow(TypedDict):
    """계약 변경: 지급 건별 행 → 사건 1건 = 1행 (payment_id·victim·paid_amount·paid_at 삭제)."""

    claim_no: str
    insured_party_id: str | None  # 보험가입자(우리 고객) 당사자 ID
    coverage: str
    insurance_payment: int  # 산정 자차 보험금
    counterparty_fault: int
    planned_share: int  # 산정 보험금 × 상대 과실 (예정액)
    evidence_count: int  # 사건에 연결된 증빙 수


class ClaimPackage(TypedDict):
    case_id: str
    package_version: int
    created_at: str
    report_version: int
    counterparty: CounterpartyInfo | None  # 계약 변경: 미입력이면 null
    counterparty_fault: int
    fault_change_reason: str | None
    rows: list[ClaimRow]
    amounts: ClaimAmounts  # 청구 금액만 (고객 자기부담금 검토액 제외)
    basis: dict[str, object]  # 과실비율 근거서: 보고서 2~6 + 결정례 + 키프레임
    evidence: list[dict[str, object]]  # 증빙 목록
    missing_evidence: list[str]  # 증빙 없는 구상 건(경고만, 생성은 막지 않음)
    # 계약 변경: released 삭제 → 담당자 확인 완료(confirm_report) 기록으로 대체
    confirmed_by: str
    confirmed_at: str
    idempotency_key: str
    rule_version: str
    rule_notice: str | None  # 예시 규칙이면 "예시 규칙 기준 — 약관 검수 전 초안"
    assumptions: list[str]  # 산정에 쓴 기본 가정(계약·자기부담금·면책 미입력). 없으면 []


class ConfirmResult(TypedDict):
    """confirm_report 응답. 청구서를 못 만들면 package·html은 null, 사유는 claim_blocked_reasons."""

    report: ReviewReport
    package: ClaimPackage | None
    html: str | None
    claim_blocked_reason: str | None  # claim_blocked_reasons의 첫 번째 값(없으면 null)
    claim_blocked_reasons: list[str]  # 청구서 조건이 안 맞는 사유 전부. 만들었으면 []


class ClaimResult(TypedDict):
    """get_claim 응답: 그 보고서 버전으로 만든 구상 청구서."""

    package: ClaimPackage
    html: str
