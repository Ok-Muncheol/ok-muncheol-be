"""D: 판단 과실(draft_ready)·검수 약관/계약/한도/면책/자기부담금으로 자차 보험금 산정.

설계 문서(2026-11-05) 단순 예시 산식 — 실무 검수 전에는 확정으로 내지 않는다.
- 자차 보험금 = 계약상 보상 인정 손해액 − 적용 자기부담금
- 구상 예정액 = 산정 보험금 × 상대방 책임비율        (claims/package.py에서 RecoveryAmounts로 분리)
- 고객 자기부담금 검토액 = 자기부담금 × 상대방 책임비율
금액은 모두 이 파일의 코드가 계산한다. LLM은 exclusions.py에서 면책 조항 해당 여부만 본다.
"""

import hashlib
import json

from app.calculation.damages import calculate_damages
from app.calculation.rules import DEFAULT_RULES, RuleSet, deductible_for, share
from app.schemas.reports import CalculationResult, OwnDamageContractDoc, PreCheck, ReviewReport

# 산정 사유·청구서 없음 사유 공통
ZERO_PAYMENT_REASON = "자기부담금이 손해액 이상이라 지급액 0원 — 구상할 보험금 없음"
ASSUMED = "기본 가정(미입력)"
CONTRACT_ASSUMPTION = (
    "계약 정보: 기본 가정(미입력) — 자차 가입·보험기간 안·운전자 범위 안, 전손 한도 검사 생략"
)
EXCLUSION_ASSUMPTION = "면책 사실 미입력 — 해당 없음으로 가정"


def deductible_rule_text(rules: RuleSet) -> str:
    """예: "기본 규칙 적용(20%, 최저 20만·최고 50만)" — rules.py 값으로 만든다."""
    return (
        f"기본 규칙 적용({rules.deductible_rate:.0%}, 최저 {rules.deductible_min // 10_000:,}만·"
        f"최고 {rules.deductible_max // 10_000:,}만)"
    )


def _uses_default_deductible(contract: OwnDamageContractDoc | None) -> bool:
    keys = ("deductible_fixed", "deductible_rate", "deductible_min", "deductible_max")
    return contract is None or all(contract.get(k) is None for k in keys)


def assumptions_of(checks: list[PreCheck], rules: RuleSet) -> list[str]:
    """산정 전 검증의 assumed 항목을 사람이 읽는 가정 목록으로."""
    keys = {c["key"] for c in checks if c["result"] == "assumed" or c["key"] == "deductible"}
    out: list[str] = []
    if "own_damage_joined" in keys:
        out.append(CONTRACT_ASSUMPTION)
    if "deductible" in keys:
        out.append(f"자기부담금: {deductible_rule_text(rules)}")
    excl = [k.split(":", 1)[1] for k in sorted(keys) if k.startswith("exclusion:")]
    if excl:
        total = sum(1 for c in checks if c["key"].startswith("exclusion:"))
        out.append(
            f"면책: {EXCLUSION_ASSUMPTION}"
            if len(excl) == total
            else f"면책({'·'.join(excl)}): {EXCLUSION_ASSUMPTION}"
        )
    return out


class CalculationBlocked(ValueError):
    """판단·입력 조건이 맞지 않아 산정을 시작할 수 없음."""


def insured_and_counterparty_side(report: ReviewReport) -> tuple[str, str] | None:
    m = report.get("party_mapping")
    if not m or not m.get("insured_party_id"):
        return None
    if m["insured_party_id"] == m.get("chart_party_A_id"):
        return "A", "B"
    if m["insured_party_id"] == m.get("chart_party_B_id"):
        return "B", "A"
    return None


def _latest(documents: list[dict[str, object]], kind: str) -> dict[str, object] | None:
    docs = [d for d in documents if d.get("kind") == kind]
    return docs[-1] if docs else None


def _hash(*parts: object) -> str:
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _contract_checks(
    contract: OwnDamageContractDoc | None, accident_date: str, mapping_ok: bool, rules: RuleSet
) -> list[PreCheck]:
    def c(key, label, result, detail, decided_by="code", clauses=()):
        return {
            "key": key,
            "label": label,
            "result": result,
            "decided_by": decided_by,
            "detail": detail,
            "clause_ids": cited(*clauses),
        }

    checks: list[PreCheck] = [
        c(
            "party_mapping",
            "보험가입자·도표 A/B 매핑",
            "pass" if mapping_ok else "unknown",
            "가입자가 도표 당사자와 일치" if mapping_ok else "가입자와 도표 A/B 매핑 확인 필요",
            "handler",
        )
    ]
    if contract is None:  # 1차 판정 초안: 계약 서류가 없으면 기본 가정으로 산정한다
        checks += [
            c("own_damage_joined", "자기차량손해 가입", "assumed", f"{ASSUMED} — 자차 가입"),
            c(
                "policy_period",
                "사고일이 보험기간 안",
                "assumed",
                f"{ASSUMED} — 보험기간 안",
                clauses=[rules.clause_period],
            ),
            c(
                "driver_scope",
                "운전자 범위",
                "assumed",
                f"{ASSUMED} — 운전자 범위 안",
                clauses=[rules.clause_driver_scope],
            ),
            c(
                "loss_limit",
                "전손·보험가입금액 한도",
                "assumed",
                f"{ASSUMED} — 전손 한도 검사 생략",
            ),
        ]
        return checks + _deductible_check(contract, rules)
    joined = contract.get("own_damage_joined")
    checks.append(
        c(
            "own_damage_joined",
            "자기차량손해 가입",
            {True: "pass", False: "fail", None: "unknown"}[joined],
            {True: "가입", False: "미가입", None: "가입 여부 미확인"}[joined],
        )
    )
    in_period = contract["period_start"] <= accident_date <= contract["period_end"]
    checks.append(
        c(
            "policy_period",
            "사고일이 보험기간 안",
            "pass" if in_period else "fail",
            f"{contract['period_start']} ~ {contract['period_end']}, 사고일 {accident_date}",
            clauses=[rules.clause_period],
        )
    )
    drv = contract.get("driver_in_scope")
    checks.append(
        c(
            "driver_scope",
            f"운전자 범위({contract.get('driver_scope') or '미입력'})",
            {True: "pass", False: "fail", None: "unknown"}[drv],
            {
                True: "범위 안",
                False: "범위 밖 운전자 — 보상 제외 대상",
                None: "범위 해당 여부 미확인",
            }[drv],
            "handler",
            [rules.clause_driver_scope],
        )
    )
    if contract.get("insured_amount") is None:
        checks.append(c("insured_amount", "보험가입금액", "unknown", "보험가입금액 미입력"))
    return checks + _deductible_check(contract, rules)


# 예시 규칙(rules.py)의 자리 표시 조항 ID. 열어 볼 문서가 아니라 근거 목록에는 넣지 않는다(#204).
# 예시 규칙이라는 표시는 rule_version("example-…")과 8번 '근거 상태'·청구서 워터마크로 남는다
EXAMPLE_CLAUSE_PREFIX = "example:"


def cited(*clause_ids: str) -> list[str]:
    """근거로 내보낼 조항 ID. 예시 규칙 자리 표시 ID(example:)는 뺀다."""
    return [i for i in clause_ids if i and not i.startswith(EXAMPLE_CLAUSE_PREFIX)]


def _deductible_check(contract: OwnDamageContractDoc | None, rules: RuleSet) -> list[PreCheck]:
    if not _uses_default_deductible(contract):
        return []
    return [
        {
            "key": "deductible",
            "label": "자기부담금",
            "result": "pass",
            "decided_by": "code",  # 규칙 값 사용 — 상태를 낮추지 않음
            "detail": deductible_rule_text(rules),
            "clause_ids": cited(rules.clause_deductible),
        }
    ]


def calculate_insurance(
    report: ReviewReport,
    documents: list[dict[str, object]],
    *,
    exclusion_checks: list[PreCheck] | None = None,
    rules: RuleSet = DEFAULT_RULES,
) -> CalculationResult:
    """판단 결과(draft_ready)로 바로 산정한다(단계별 승인 없음). 견적 추가는 이 단계부터 새 버전으로
    재산정."""
    judgment = report["judgment"]
    if (
        judgment["status"] != "draft_ready"
        or judgment["fault_A"] is None
        or judgment["fault_B"] is None
    ):
        raise CalculationBlocked("과실비율이 확정(draft_ready)되지 않았습니다")

    sides = insured_and_counterparty_side(report)
    mapping = report.get("party_mapping") or {}
    insured_id = mapping.get("insured_party_id")
    contract: OwnDamageContractDoc | None = _latest(documents, "own_damage_contract")  # type: ignore[assignment]
    accident_date = _accident_date(report)
    damage = calculate_damages(documents, party_id=insured_id)

    checks = _contract_checks(contract, accident_date, sides is not None, rules)
    if exclusion_checks is None:
        checks.append(
            {
                "key": "exclusions",
                "label": "면책·감액 조항",
                "result": "unknown",
                "decided_by": "code",
                "detail": "약관 면책 확인 미실행",
                "clause_ids": [],
            }
        )
    else:
        checks += exclusion_checks

    result: CalculationResult = {
        "versions": dict(report["versions"]),  # type: ignore[typeddict-item]
        "status": "needs_contract_review",
        "currency": "KRW",
        "recognized_damage": None,
        "insurance_payment": None,
        "deductible": None,
        "rule_version": rules.version,
        "source_document_ids": list(damage["source_document_ids"])
        + ([str(contract["document_id"])] if contract else []),
        "reasons": [],
        "damage": damage,
        "loss_type": None,
        "counterparty_fault": None,
        "checks": checks,
        "formulas": {},
        "clause_ids": [],
        "terms_edition": contract.get("terms_edition") if contract else None,
        "assumptions": assumptions_of(checks, rules),
    }
    result["inputs_hash"] = _hash(
        damage,
        contract,
        judgment["fault_A"],
        [(c["key"], c["result"], c.get("warning")) for c in checks],
        rules.version,
    )
    failed = [c for c in checks if c["result"] == "fail"]
    unknown = [c for c in checks if c["result"] == "unknown"]
    assumed = [c for c in checks if c["result"] == "assumed"]
    warned = [c for c in checks if c.get("warning")]
    noted = unknown + warned  # 막지 않고 사유로 보여 줄 항목
    if failed:
        result["status"] = "not_payable"
        result["reasons"] = [f"{c['label']}: {c['detail']}" for c in failed + warned]
        result["clause_ids"] = sorted({i for c in failed for i in c["clause_ids"]})
        return result
    if damage["amount"] is None:
        result["status"] = "needs_documents"
        result["reasons"] = list(damage["reasons"]) + [
            f"{c['label']}: {c['detail']}" for c in noted
        ]
        return result
    if (contract is not None and contract.get("insured_amount") is None) or sides is None:
        result["reasons"] = list(damage.get("document_reasons") or []) + [
            f"{c['label']}: {c['detail']}" for c in noted
        ]
        return result

    # ── 계산 (모두 코드) ──
    amount = damage["amount"]
    if contract is None:  # 기본 가정: 가입금액·전손 한도 검사 생략
        loss_type, recognized = "partial", amount
        f_rec = f"분손(기본 가정, 한도 검사 생략): 손해액 {amount:,}"
    elif (
        vehicle_value := contract.get("vehicle_value")
    ) and amount >= vehicle_value * rules.total_loss_ratio:
        insured_amount = int(contract["insured_amount"])  # type: ignore[arg-type]
        loss_type, recognized = "total", min(insured_amount, int(vehicle_value))
        f_rec = f"전손: min(보험가입금액 {insured_amount:,}, 차량가액 {int(vehicle_value):,})"
        result["clause_ids"] += cited(rules.clause_total_loss)
    else:
        insured_amount = int(contract["insured_amount"])  # type: ignore[arg-type]
        loss_type, recognized = "partial", min(amount, insured_amount)
        f_rec = f"분손: min(손해액 {amount:,}, 보험가입금액 {insured_amount:,})"
    k = contract or {}
    ded = deductible_for(
        recognized,
        fixed=k.get("deductible_fixed"),
        rate=k.get("deductible_rate"),
        min_=k.get("deductible_min"),
        max_=k.get("deductible_max"),
        rules=rules,
    )
    payment = max(recognized - ded, 0)
    cp_fault = int(judgment["fault_B"] if sides[1] == "B" else judgment["fault_A"])
    rate = (
        k.get("deductible_rate") if k.get("deductible_rate") is not None else rules.deductible_rate
    )

    result.update(
        {
            "recognized_damage": recognized,
            "deductible": ded,
            "insurance_payment": payment,
            "loss_type": loss_type,
            "counterparty_fault": cp_fault,
        }
    )
    result["clause_ids"] += cited(rules.clause_payout, rules.clause_deductible)
    result["formulas"] = {
        "보상 인정 손해액": f"{f_rec} = {recognized:,}",
        "자기부담금": (
            f"계약 정액 {k['deductible_fixed']:,}"
            if k.get("deductible_fixed") is not None
            else f"{deductible_rule_text(rules)}: 손해액 × {rate:.0%}"
            if _uses_default_deductible(contract)
            else f"손해액 × {rate:.0%} (최저·최고 한도 적용)"
        )
        + f" = {ded:,}",
        "자차 보험금": f"{recognized:,} − {ded:,} = {payment:,}",
        "구상 예정액(참고)": f"{payment:,} × 상대 과실 {cp_fault}% = {share(payment, cp_fault):,}",
        "고객 자기부담금 검토액": f"{ded:,} × 상대 과실 {cp_fault}% = {share(ded, cp_fault):,}",
    }

    reasons = list(damage.get("document_reasons") or []) + [
        f"{c['label']}: {c['detail']}" for c in noted
    ]
    if payment == 0:
        reasons.insert(0, ZERO_PAYMENT_REASON)
    if rules.is_example:
        reasons.append("예시 규칙(실무·약관 검수 전) — 확정 산정 아님")
    if unknown or assumed or rules.is_example:
        result["status"] = "needs_contract_review"
    else:
        result["status"] = "confirmed"
    result["reasons"] = reasons
    return result


def _accident_date(report: ReviewReport) -> str:
    for s in report["sections"]:
        if s.get("no") == 2:
            body = s.get("body") or {}
            if isinstance(body, dict) and body.get("사고 일자"):
                return str(body["사고 일자"])
    raise CalculationBlocked("보고서에 사고 일자가 없습니다")
