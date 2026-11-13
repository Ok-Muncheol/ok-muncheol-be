"""D: 1차 확장 구상 청구 패키지. 계산 코드와 템플릿 조립만, LLM 없음.

확정 흐름(1차 판정 초안): 산정 보험금이 나오면 9번 구상을 자동으로 채우고, 담당자 [확인 완료] 한
번에 패키지를 만든다.
- 예정액 = 산정 자차 보험금 × 상대 과실(원 미만 버림). 청구액은 예정액으로 자동 채움, 회수액은 null.
- 고객 자기부담금 검토액(자기부담금 × 상대 과실)은 보고서 9번(내부 참고)에만, 패키지에는 청구
  금액만(ClaimAmounts).
- 사건 1건 = 구상 1행. 증빙은 사건에 바로 연결한다(지급 내역 없음).
- 청구서 증빙은 우리 자차 구상 증빙만(claim_evidence, #198): 상대 차 견적서(대물 판정 근거)와
  진단서·진료비 영수증(대인 판정 근거)은 사건 서류에는 남지만 상대 보험사로 가는 청구서에는 넣지
  않는다.
- 확인 완료는 막지 않고 청구서만 만들지 않는 경우(사유를 돌려줌): 견적서 없음, 지급
  불가(not_payable),
  보험금 0원, 보험금 산정 불가(매핑 불명·가입금액 없음 등), 상대 과실 0%(협의 0% 포함).
- 예시 규칙·기본 가정이어도 만들되 "예시 규칙 기준 — 약관 검수 전 초안"과 가정 항목을 표시한다.
  보고서에 검증된 약관 조항이 붙었으면 "예시 규칙 기준 — 약관 조항 원문 연결(실무 검수 전) 초안".
- 근거서 '근거 조항'에는 조항 이름만(보통약관 제34조는 9번에 검증돼 붙었을 때만,
  상법 제682조는 항상).
  발췌·페이지·산정 근거(제24조·별표)는 넣지 않는다.
  증빙이 없으면 경고만.
- 같은 청구 내용이면 같은 idempotency_key가 나온다(중복 생성 방지). 키는 청구서 내용에서 버전·확인
  기록 칸(보고서 버전, 청구 번호, 증빙의 '관련 구상 건', 확인자·시각)을 뺀 해시다(claim_content).
  그래서 대물·대인 입력처럼 8번만 바뀐 새 보고서 버전을 다시 확인해도 같은 청구서(C1)를 쓴다(#194).
"""

import hashlib
import json
import posixpath
from datetime import UTC, datetime
from urllib.parse import unquote

from app.calculation.insurance import ZERO_PAYMENT_REASON, insured_and_counterparty_side
from app.calculation.rules import DEFAULT_RULES, RuleSet, share
from app.reports.clauses import (
    EXAMPLE_NOTICE,
    claim_clause_names,
    clause_notice,
    report_clauses_connected,
)
from app.schemas.reports import (
    CalculationResult,
    ClaimAmounts,
    ClaimPackage,
    ClaimRow,
    CounterpartyInfo,
    RecoveryAmounts,
    ReviewReport,
)
from app.storage import STORAGE_BUCKET

EXAMPLE_RULE_NOTICE = EXAMPLE_NOTICE
NEEDS_ESTIMATE = "견적서 필요 — 견적서 추가 후 청구서 생성"
NOT_RECOVERABLE = "상대 과실 0% — 구상 대상 아님"  # 8번 부담 요약(assembler)도 이 상수를 쓴다
NOT_PAYABLE = "지급 불가 — 구상할 보험금 없음"
ZERO_PAYMENT = ZERO_PAYMENT_REASON
NOT_CALCULABLE = "보험금 산정 불가"


class ClaimPackageBlocked(ValueError):
    """청구서를 만들 수 없음. reasons에 해당하는 사유를 전부 담는다(메시지는 '; '로 이은 것)."""

    def __init__(self, *reasons: str):
        self.reasons = list(reasons)
        super().__init__("; ".join(reasons))


def idempotency_key(case_id: str, stage: str, version: int, *parts: str) -> str:
    """사건 ID + 단계 + 버전 (+ 입력 요약)."""
    return hashlib.sha256("|".join([case_id, stage, str(version), *parts]).encode()).hexdigest()[
        :24
    ]


# 청구서에서 버전·확인 기록으로 정해지는 칸. 청구 내용 비교에서 뺀다.
_NOT_CONTENT = (
    "package_version", "created_at", "report_version", "confirmed_by", "confirmed_at",
    "idempotency_key",
)


# 대인 판정 근거로만 쓰는 증빙 종류(EvidenceDoc.doc_type, #185). 상대 차 탑승자 건강 정보다
INJURY_EVIDENCE_DOC_TYPES = ("medical_certificate", "medical_bill")
# 대인 증빙 업로드 폴더(POST /uploads kind="medical_document" → case-files/{case_id}/medical/…)
MEDICAL_FOLDER = "medical"


def _medical_folder_path(storage_path: object) -> bool:
    """저장 경로가 어느 사건이든 `medical/` 폴더(대인 증빙) 아래인가.

    문자열 포함 검사가 아니라 정규화한 경로 조각으로 본다: 퍼센트 인코딩을 풀고(여러 번),
    역슬래시를 '/'로, 대소문자를 무시하고, 앞쪽 '/'·'.'·'..'·빈 조각을 정리한 뒤
    `[case-files/]<사건 ID>/medical/<파일>` 모양인지 본다. 다른 사건의 medical/ 경로도 뺀다
    (그 사건 피해자의 건강 정보이고, 우리 구상 청구서에 들어갈 이유가 없다).
    """
    if not isinstance(storage_path, str) or not storage_path:
        return False
    text = storage_path
    for _ in range(3):  # %25 2중 인코딩까지 푼다
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    text = text.replace("\\", "/").lower()
    parts = [p for p in posixpath.normpath("/" + text).split("/") if p]
    if parts and parts[0] == STORAGE_BUCKET:
        parts = parts[1:]
    return len(parts) >= 3 and parts[1] == MEDICAL_FOLDER


# upload_estimate가 견적서 PDF를 증빙으로 자동 등록할 때의 종류·ID 접두어
# (document_id="ev-<견적 ID>")
ESTIMATE_EVIDENCE_DOC_TYPE = "repair_estimate"
ESTIMATE_EVIDENCE_PREFIX = "ev-"


def claim_evidence(
    documents: list[dict[str, object]], insured_party_id: object
) -> list[dict[str, object]]:
    """구상 청구서에 넣을 증빙: 사건 증빙 중 우리 자차 구상 증빙만.

    - 진단서·진료비 영수증(doc_type)은 뺀다. 대인 판정 근거로만 쓴다.
    - 종류와 상관없이 저장 경로가 `medical/` 폴더(대인 증빙 업로드) 아래인 증빙도 뺀다. 진단서
      파일을 영수증 같은 일반 증빙으로 붙여도 청구서에 들어가지 않게 한다(_medical_folder_path).
    - 견적서 PDF에서 자동 등록된 증빙(doc_type="repair_estimate", "ev-<견적 ID>")에는 당사자가
      없다. 그래서 원래 견적서(kind="repair_estimate")의 party_id로 판단해, 당사자가 우리 차가
      아닌 견적서(상대 차, 대물 판정 근거)의 증빙은 뺀다. 당사자가 비어 있는 견적서는 예전처럼
      우리 것으로 본다.
    - 사진·영수증·수리 명세 등 그 밖의 증빙은 그대로 둔다.
    """
    others = {
        str(d["document_id"])
        for d in documents
        if d.get("kind") == "repair_estimate"
        and d.get("party_id") is not None
        and d.get("party_id") != insured_party_id
    }

    def counterparty_estimate(e: dict[str, object]) -> bool:
        doc_id = str(e.get("document_id"))
        return (
            e.get("doc_type") == ESTIMATE_EVIDENCE_DOC_TYPE
            and doc_id.startswith(ESTIMATE_EVIDENCE_PREFIX)
            and doc_id.removeprefix(ESTIMATE_EVIDENCE_PREFIX) in others
        )

    return [
        d
        for d in documents
        if d.get("kind") == "evidence"
        and d.get("doc_type") not in INJURY_EVIDENCE_DOC_TYPES
        and not _medical_folder_path(d.get("storage_path"))
        and not counterparty_estimate(d)
    ]


def claim_content(pkg: ClaimPackage) -> dict[str, object]:
    """청구 내용: 금액·과실·상대 정보·근거서·증빙·기본 가정. 버전에서 나오는 칸은 뺀다.

    예전 키(보고서 버전 포함)로 만든 청구서도 이 값으로 비교할 수 있다.
    """
    out: dict[str, object] = {k: v for k, v in pkg.items() if k not in _NOT_CONTENT}
    out["basis"] = {k: v for k, v in pkg["basis"].items() if k != "보고서 버전"}
    out["rows"] = [{k: v for k, v in r.items() if k != "claim_no"} for r in pkg["rows"]]
    out["evidence"] = [
        {k: v for k, v in e.items() if k != "관련 구상 건"} for e in pkg["evidence"]
    ]
    out["missing_evidence"] = len(pkg["missing_evidence"])  # 원소는 청구 번호뿐
    return out


def claim_content_key(pkg: ClaimPackage) -> str:
    body = json.dumps(claim_content(pkg), ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(body.encode()).hexdigest()
    return idempotency_key(str(pkg["case_id"]), "claim_package", 0, digest)


def counterparty_fault_of(report: ReviewReport) -> int | None:
    sides = insured_and_counterparty_side(report)
    j = report["judgment"]
    if sides is None or j["fault_A"] is None:
        return None
    return int(j["fault_B"] if sides[1] == "B" else j["fault_A"])


def compute_recovery(
    report: ReviewReport,
    calculation: CalculationResult,
    *,
    agreed_counterparty_fault: int | None = None,
    fault_change_reason: str | None = None,
) -> RecoveryAmounts:
    """9번 자동 작성. 산정 보험금 × 상대 과실(협의 과실이 있으면 그 값). 모르면 null."""
    if agreed_counterparty_fault is not None and not fault_change_reason:
        raise ValueError("협의로 과실비율을 바꾸면 이유를 함께 입력해야 합니다")
    cp = (
        agreed_counterparty_fault
        if agreed_counterparty_fault is not None
        else counterparty_fault_of(report)
    )
    payment, ded = calculation.get("insurance_payment"), calculation.get("deductible")
    planned = share(int(payment), cp) if cp is not None and payment is not None else None
    review = share(int(ded), cp) if cp is not None and ded is not None else None
    out: RecoveryAmounts = {
        "planned": planned,
        "claimed": planned,
        "recovered": None,
        "customer_deductible_review": review,
    }
    if agreed_counterparty_fault is not None:
        out.update(
            {
                "agreed_counterparty_fault": agreed_counterparty_fault,
                "fault_change_reason": fault_change_reason,
            }
        )
    return out


def claim_blocked_reasons(report: ReviewReport) -> list[str]:
    """청구서를 만들 수 없는 사유를 전부 모은다(없으면 []). 순서: 지급 불가 → 견적서 → 산정 불가 →
    0원 → 협의 이유 → 상대 과실 0%."""
    if report["judgment"]["status"] != "draft_ready":
        return ["추가 확인·보류 상태의 보고서는 청구서를 만들 수 없습니다"]
    calc, recovery = report.get("calculation"), report.get("recovery")
    if not calc or recovery is None:
        return [f"{NOT_CALCULABLE} — 산정 결과 없음"]
    out: list[str] = []
    not_payable = calc["status"] == "not_payable"
    if not_payable:
        out.append(NOT_PAYABLE)
    damage = calc.get("damage") or {}
    no_estimate = damage.get("amount") is None
    if no_estimate:
        out.append(NEEDS_ESTIMATE)
    payment = calc.get("insurance_payment")
    if payment is None and not not_payable and not no_estimate:
        out.append(f"{NOT_CALCULABLE} — " + "; ".join(calc["reasons"] or [calc["status"]]))
    elif payment == 0:
        out.append(ZERO_PAYMENT)
    agreed = recovery.get("agreed_counterparty_fault")
    if agreed is not None and not recovery.get("fault_change_reason"):
        out.append("협의로 과실비율을 바꾸면 이유를 함께 입력해야 합니다")
    cp = agreed if agreed is not None else counterparty_fault_of(report)
    if cp is None:
        if not any(r.startswith(NOT_CALCULABLE) for r in out):
            out.append(f"{NOT_CALCULABLE} — 보험가입자와 도표 A/B 매핑을 확인할 수 없습니다")
    elif cp == 0:
        out.append(NOT_RECOVERABLE)
    return out


def check_claimable(report: ReviewReport) -> int:
    """청구서 생성 조건 검사. 통과하면 구상에 쓸 상대 과실(%)을 돌려주고, 아니면 사유 전부를 담아
    ClaimPackageBlocked."""
    reasons = claim_blocked_reasons(report)
    if reasons:
        raise ClaimPackageBlocked(*reasons)
    recovery: RecoveryAmounts = report["recovery"]  # type: ignore[assignment]
    agreed = recovery.get("agreed_counterparty_fault")
    return int(agreed if agreed is not None else counterparty_fault_of(report))  # type: ignore[arg-type]


def build_claim_package(
    report: ReviewReport,
    *,
    documents: list[dict[str, object]],
    counterparty: CounterpartyInfo | None,
    confirmed_by: str,
    package_version: int = 1,
    rules: RuleSet = DEFAULT_RULES,
    now: str | None = None,
) -> ClaimPackage:
    """확인 완료한 보고서(8·9번)로 근거서·구상 1행·증빙 목록을 조립한다."""
    cp = check_claimable(report)
    r = report
    calc: CalculationResult = r["calculation"]  # type: ignore[assignment]
    recovery: RecoveryAmounts = r["recovery"]  # type: ignore[assignment]
    case_id = r["versions"]["case_id"]
    payment = int(calc["insurance_payment"])  # type: ignore[arg-type]
    insured = (r.get("party_mapping") or {}).get("insured_party_id")
    evidence = claim_evidence(documents, insured)
    claim_no = f"{case_id}-C{package_version}"
    row: ClaimRow = {
        "claim_no": claim_no,
        "insured_party_id": insured,
        "coverage": "자기차량손해",
        "insurance_payment": payment,
        "counterparty_fault": cp,
        "planned_share": share(payment, cp),
        "evidence_count": len(evidence),
    }
    sections = {s["no"]: s for s in r["sections"]}  # type: ignore[index]
    # 상대 보험사로 나가는 문서라 내부 판단은 넣지 않는다: 산정 상태·사유(LLM 경고·판정 원문 포함),
    # 담당자 질문, 미해결 반박·검증. 이 내용은 보고서 7·8번에만 둔다.
    basis = {
        "보고서 버전": r["versions"]["report_version"],
        "사고 사실": sections[2]["body"],
        "적용 도표": sections[3]["body"],
        "기본 과실": sections[4]["body"],
        "가감 요소": sections[5]["body"],
        "최종 과실비율": sections[6]["body"],
        "유사 결정례": sections[7]["body"].get("유사 결정례", []),
        "기본 가정": list(calc.get("assumptions") or []),
        "근거 조항": claim_clause_names(r),
    }
    evid_list = [
        {
            "파일": str(e["document_id"]),
            "문서 유형": str(e["doc_type"]),
            "관련 구상 건": claim_no,
            "첨부": "첨부" if e.get("storage_path") else "누락",
        }
        for e in evidence
    ]
    is_example = str(calc["rule_version"]).startswith("example-")
    amounts: ClaimAmounts = {
        "planned": row["planned_share"],
        "claimed": row["planned_share"],
        "recovered": None,
    }
    for k in ("agreed_counterparty_fault", "fault_change_reason"):
        if k in recovery:
            amounts[k] = recovery[k]  # type: ignore[literal-required]
    pkg: ClaimPackage = {
        "case_id": case_id,
        "package_version": package_version,
        "created_at": now or datetime.now(UTC).isoformat(timespec="seconds"),
        "report_version": int(r["versions"]["report_version"] or 0),
        "counterparty": counterparty,
        "counterparty_fault": cp,
        "fault_change_reason": recovery.get("fault_change_reason")
        if recovery.get("agreed_counterparty_fault") is not None
        else None,
        "rows": [row],
        "amounts": amounts,
        "basis": basis,
        "evidence": evid_list,
        "missing_evidence": [] if evidence else [claim_no],
        "confirmed_by": confirmed_by,
        "confirmed_at": now or datetime.now(UTC).isoformat(timespec="seconds"),
        "idempotency_key": "",  # 아래에서 청구 내용으로 채운다
        "rule_version": calc["rule_version"],
        "rule_notice": clause_notice(True, report_clauses_connected(r)) if is_example else None,
        "assumptions": list(calc.get("assumptions") or []),
    }
    pkg["idempotency_key"] = claim_content_key(pkg)
    return pkg
