"""D: 검증 결과를 템플릿으로 조립. 보고서 작성용 LLM은 추가 호출하지 않는다.

항목은 templates/review.md 설계를 따른다.
- 1~7: 선행 MVP (과실비율 근거)
- 8: 손해액·보험금·자기부담금·약관/서류/규칙 버전 (1차 확장). 대물·대인 입력이 있으면
  「대물·대인 1차 판정」 칸을 덧붙인다(C, 기존 칸은 그대로)
- 9: 구상 예정액·실제 청구액·회수액·고객 자기부담금 검토액·필수 서류 (1차 확장)
- 8·9 약관 근거 조항: 검증된 조항만(app/reports/clauses.py). 없으면 "연결 안 됨"
확인/보류 상태에서는 4~6의 수치를 비우고, 8~9는 확정 판단(draft_ready) 과실비율로 바로 채운다(단계별
승인 없음).

B 계약 제안: JudgmentResult에 base_fault_A, modifiers, alternatives가 들어오면 4·5·3번을 채운다.
지금은 .get()으로 읽어서, 없으면 '판단 로그에 없음'으로 비워 둔다.
"""

from datetime import UTC, datetime

from app.calculation.rules import share
from app.claims.package import NOT_RECOVERABLE
from app.reports.clauses import (
    ReportClauses,
    clause_ids,
    clause_notice,
    payout_basis,
    recovery_basis,
)
from app.schemas.facts import FactCard
from app.schemas.judgment import JudgmentResult
from app.schemas.liability import LiabilityResult
from app.schemas.reports import CalculationResult, RecoveryAmounts, ReportSection, ReviewReport

STATUS_LABEL = {
    "draft_ready": "초안 작성 가능",
    "needs_confirmation": "추가 확인 필요",
    "deferred": "판단 보류",
}
CALC_LABEL = {
    # 1차 미사용 — 참고값 기능 제거, 값은 계약 호환용으로 유지
    "estimated": "손해액 미확정(추정·참고)",
    "needs_documents": "손해액 미확정(서류 대기)",
    "needs_contract_review": "계약·규칙 확인 필요",
    "not_payable": "지급 대상 아님",
    "confirmed": "확정",
}
VERIFY_LABEL = {
    "unverified": "미확인",
    "needs_review": "확인 필요",
    "human_confirmed": "담당자 확인",
}


class ReportInputMismatch(ValueError):
    pass


def _won(v: int | None) -> str | None:
    return None if v is None else f"{v:,}원"


def _sec(
    no: int,
    title: str,
    body: dict[str, object] | None = None,
    evidence: list[str] | None = None,
    empty_reason: str | None = None,
) -> ReportSection:
    if empty_reason is not None:
        return {
            "no": no,
            "title": title,
            "state": "empty",
            "body": {},
            "evidence_ids": [],
            "empty_reason": empty_reason,
        }
    return {
        "no": no,
        "title": title,
        "state": "filled",
        "body": body or {},
        "evidence_ids": evidence or [],
        "empty_reason": None,
    }


def _hits(judgment: JudgmentResult, corpus: str) -> list[dict[str, object]]:
    out = []
    for h in judgment["evidence"]:
        c = h["chunk"]
        if c["document"]["corpus"] == corpus:
            out.append(
                {
                    "ID": c["parent_id"],
                    "문서": c["document"]["document_id"],
                    "적용판": c["document"]["edition"],
                    "페이지": f"{c['page_start']}"
                    + (f"–{c['page_end']}" if c["page_end"] != c["page_start"] else ""),
                    "발췌": c["text"][:120],
                }
            )
    return out


def assemble_review(
    judgment: JudgmentResult,
    calculation: CalculationResult | None = None,
    *,
    card: FactCard | None = None,
    recovery: RecoveryAmounts | None = None,
    previous: ReviewReport | None = None,
    reason: str = "판단 결과 반영",
    education_mode: bool = False,
    now: str | None = None,
    clauses: ReportClauses | None = None,
    liability: LiabilityResult | None = None,
) -> ReviewReport:
    """MVP 1~7항목, 확장 8~9항목. 확인/보류 상태에서 수치를 채워 넣지 않는다.

    clauses: 검증된 8·9번 약관 근거(app/reports/clauses.py). None이면 "연결 안 됨"."""
    j = judgment
    ready = j["status"] == "draft_ready" and j["fault_A"] is not None
    if card is not None and card["case_id"] != j["versions"]["case_id"]:
        raise ReportInputMismatch("사실 카드와 판단 결과의 사건이 다릅니다")
    if card is not None and card["version"] != j["versions"]["fact_card_version"]:
        raise ReportInputMismatch("판단에 쓴 사실 카드 버전과 보고서 카드 버전이 다릅니다")
    if (
        calculation is not None
        and calculation["versions"]["judgment_version"] != j["versions"]["judgment_version"]
    ):
        raise ReportInputMismatch("산정 결과의 판단 버전이 현재 판단과 다릅니다")
    if previous is None and card is None:
        raise ReportInputMismatch("첫 보고서에는 사실 카드가 필요합니다")
    if card is None and previous is not None:
        mapping = previous.get("party_mapping")
        fact_section = next(s for s in previous["sections"] if s["no"] == 2)
    else:
        mapping, fact_section = card["party_mapping"], None  # type: ignore[index]

    sections: list[ReportSection] = []

    # 1. 판정 상태
    sections.append(
        _sec(
            1,
            "판정 상태",
            {
                "상태": STATUS_LABEL[j["status"]],
                "사유": j["reasons"],
                "검증": [
                    {"단계": c["layer"], "통과": c["passed"], "사유": "; ".join(c["reasons"])}
                    for c in j["checks"]
                ],
            },
        )
    )

    # 2. 확정 사실
    if fact_section is not None:
        sections.append(fact_section)  # type: ignore[arg-type]
    else:
        assert card is not None
        fields = [
            {
                "칸": k,
                "값": f["value"],
                "확인": VERIFY_LABEL[f["verification_status"]],
                "담당자 수정": f["model_value"] is not None and f["model_value"] != f["value"],
                "출처": f["source"],
            }
            for k, f in card["fields"].items()
        ]
        frames = [f"{fr['frame_id']}@{fr['timestamp_ms']}ms" for fr in card["evidence_frames"]]
        sections.append(
            _sec(
                2,
                "확정 사실",
                {
                    "사고 일자": card["accident_date"],
                    "카드 버전": card["version"],
                    "확정": f"{card['confirmed_by']} · {card['confirmed_at']}"
                    if card["status"] == "confirmed"
                    else "미확정",
                    "사실 카드": fields,
                    "근거 키프레임": frames,
                    "당사자": {
                        "도표 A": card["party_mapping"]["chart_party_A_id"],
                        "도표 B": card["party_mapping"]["chart_party_B_id"],
                        "보험가입자": card["party_mapping"]["insured_party_id"],
                    },
                },
                [fr["frame_id"] for fr in card["evidence_frames"]],
            )
        )

    # 3. 적용 도표
    if j["chart_id"]:
        body3: dict[str, object] = {
            "도표": j["chart_id"],
            "적용판": j["edition"],
            "근거 원문": _hits(j, "fault_standards"),
        }
        alts = j.get("alternatives") or []  # type: ignore[typeddict-item]
        if alts:
            body3["차순위 후보"] = alts
            if education_mode:
                body3["고르지 않은 이유"] = [
                    {"도표": a.get("chart_id"), "이유": a.get("reason")} for a in alts
                ]
        sections.append(
            _sec(
                3,
                "적용 도표",
                body3,
                [j["chart_id"]]
                + ([j["edition"]] if j["edition"] else [])
                + [h["ID"] for h in _hits(j, "fault_standards")],
            )
        )  # type: ignore[misc]
    else:
        sections.append(
            _sec(3, "적용 도표", empty_reason="도표 미선택 — " + STATUS_LABEL[j["status"]])
        )

    # 4. 기본 과실
    base = j.get("base_fault_A")  # type: ignore[typeddict-item]
    if not ready:
        sections.append(_sec(4, "기본 과실", empty_reason="확인/보류 상태 — 수치 비움"))
    elif base is None:
        sections.append(
            _sec(4, "기본 과실", empty_reason="판단 결과에 기본 과실 없음 (B 계약 필드 필요)")
        )
    else:
        sections.append(
            _sec(
                4,
                "기본 과실",
                {"A : B": f"{base} : {100 - int(base)}"},
                [str(j["chart_id"]), "query_db"],
            )
        )

    # 5. 수정요소
    mods = j.get("modifiers") or []  # type: ignore[typeddict-item]
    if mods:
        rows = [
            {
                "수정요소": m.get("name"),
                "대상": m.get("applies_to"),
                "가감": (f"{int(m['delta']):+d}" if ready else "—"),
                "판정": m.get("decision"),
                "근거 칸": ", ".join(m.get("evidence_fields", [])),
            }
            for m in mods
        ]
        body5: dict[str, object] = {"목록": rows}
        if education_mode:
            body5["안내"] = "미적용·불명 요소는 근거 칸과 인정기준 원문을 함께 확인하세요."
        sections.append(_sec(5, "수정요소", body5, [str(m.get("modifier_id")) for m in mods]))
    else:
        sections.append(_sec(5, "수정요소", empty_reason="판단 결과에 수정요소 목록 없음"))

    # 6. 최종 과실비율
    if ready:
        sections.append(
            _sec(
                6,
                "최종 과실비율(초안)",
                {
                    "A : B": f"{j['fault_A']} : {j['fault_B']}",
                    "코드 계산 로그": j["calculation_log"],
                },
                ["calculation_log"] if j["calculation_log"] else [],
            )
        )
    else:
        sections.append(_sec(6, "최종 과실비율(초안)", empty_reason="확인/보류 상태 — 수치 비움"))

    # 7. 추가 확인 사항
    sections.append(
        _sec(
            7,
            "추가 확인 사항",
            {
                "담당자 질문": [{"칸": q["field_key"], "질문": q["text"]} for q in j["questions"]],
                "미해결 반박·검증": [
                    r for c in j["checks"] if not c["passed"] for r in c["reasons"]
                ],
                "유사 결정례": _hits(j, "dispute_cases"),
            },
            [h["ID"] for h in _hits(j, "dispute_cases")],
        )
    )  # type: ignore[misc]

    # 8. 손해액·보험금 (1차 확장)
    if not ready or calculation is None:
        sections.append(_sec(8, "손해액·보험금", empty_reason="과실비율 확정(draft_ready) 후 산정"))
    else:
        c = calculation
        d = c.get("damage")
        sections.append(
            _sec(
                8,
                "손해액·보험금",
                {
                    "산정 상태": CALC_LABEL[c["status"]],
                    "고객 차량 손해액": _won(d["amount"]) if d else None,
                    "손해액 상태": CALC_LABEL.get(d["status"], d["status"]) if d else None,
                    **({"손해액 출처": d["source_label"]} if d and d.get("source_label") else {}),
                    "손해 구분": {"partial": "분손", "total": "전손", None: None}[
                        c.get("loss_type")
                    ],
                    "보상 인정 손해액": _won(c["recognized_damage"]),
                    "자기부담금": _won(c["deductible"]),
                    "자차 보험금": _won(c["insurance_payment"]),
                    "부담 요약": burden_summary(c),
                    "계산식": c.get("formulas", {}),
                    "기본 가정": list(c.get("assumptions") or []),
                    "산정 전 검증": [
                        {
                            "항목": x["label"],
                            "결과": {
                                "pass": "통과",
                                "fail": "해당",
                                "unknown": "확인 필요",
                                "assumed": "기본 가정(미입력)",
                            }[x["result"]],
                            "내용": x["detail"],
                            "판정": x["decided_by"],
                        }
                        for x in c.get("checks", [])
                    ],
                    "사유": c["reasons"],
                    "약관 적용판": c.get("terms_edition"),
                    "규칙 버전": c["rule_version"],
                    "근거 상태": clause_notice(
                        str(c["rule_version"]).startswith("example-"),
                        clauses is not None and clauses.connected,
                    ),
                    "근거 조항": payout_basis(clauses),
                    **({"대물·대인 1차 판정": liability_body(liability)} if liability else {}),
                },
                list(
                    dict.fromkeys(
                        c["source_document_ids"]
                        + c.get("clause_ids", [])
                        + clause_ids(clauses.selection.payout if clauses else (), clauses)
                        + (liability_evidence(liability) if liability else [])
                    )
                ),
            )
        )

    # 9. 구상 (1차 확장)
    if not ready or recovery is None or recovery["planned"] is None:
        sections.append(_sec(9, "구상", empty_reason="견적서·산정 후 작성"))
    else:
        agreed = recovery.get("agreed_counterparty_fault")
        body9: dict[str, object] = {
            "구상 예정액(산정 보험금 × 상대 과실)": _won(recovery["planned"]),
            "청구액(예정액 자동)": _won(recovery["claimed"]),
            "고객 자기부담금 검토액": _won(recovery["customer_deductible_review"]),
            "협의 과실 / 변경 이유": (
                f"상대 과실 {agreed}% / {recovery.get('fault_change_reason')}"
                if agreed is not None
                else "없음(판단 과실비율 기준)"
                + (
                    f" / 협의 해제: {recovery['fault_change_reason']}"
                    if recovery.get("fault_change_reason")
                    else ""
                )
            ),
            "주의": "1차 판정 초안 — 예정액은 단순 산식(원 미만 버림). 실제 청구·회수는 정산 조건 "
            "확인 후",
            **recovery_basis(clauses),
        }
        recovery_refs = (
            clauses.selection.recovery_main + clauses.selection.recovery_reference
            if clauses
            else ()
        )
        sections.append(
            _sec(
                9,
                "구상",
                body9,
                # 구상 예정액 = 산정 보험금 × 상대 과실: 보험금의 근거 서류(견적서·계약서)를 근거로
                # 단다. 예전의 "calculation"은 열어 볼 문서가 아니라 뺐다(#204)
                list(calculation["source_document_ids"] if calculation else [])
                # 예시 규칙 버전 문자열은 근거 목록에 넣지 않는다(8번 '규칙 버전'에는 남음, #204)
                + (
                    [calculation["rule_version"]]
                    if calculation and not str(calculation["rule_version"]).startswith("example-")
                    else []
                )
                + clause_ids(recovery_refs, clauses),
            )
        )

    prev_v = previous["versions"]["report_version"] if previous else None
    versions = {**j["versions"], "report_version": (prev_v or 0) + 1}
    report: ReviewReport = {
        "versions": versions,  # type: ignore[typeddict-item]
        "judgment": j,
        "sections": sections,  # type: ignore[typeddict-item]
        "calculation": calculation if ready else None,
        "recovery": recovery if ready else None,
        "approval_status": "draft",
        "approved_by": None,
        "approved_at": None,
        "created_at": now or datetime.now(UTC).isoformat(timespec="seconds"),
        "reason": reason,
        "supersedes": prev_v,
        "party_mapping": mapping,  # type: ignore[typeddict-item]
        "education_mode": education_mode,
    }
    if ready and calculation is not None and liability is not None:
        report["liability"] = liability  # 입력이 없는 사건은 키를 두지 않는다(기존 보고서와 같음)
    return report


def burden_summary(c: CalculationResult) -> dict[str, object]:
    """8번 부담 요약. 금액은 산정 결과에서 코드로만 계산하고, 모르는 값은 null 그대로 둔다.
    상대방 부담은 지급 전 예상치(보험금 × 상대 과실, 원 미만 버림)이며 9번 구상 계산과는 별개다."""
    payment, cp = c["insurance_payment"], c.get("counterparty_fault")
    cp_share = None if payment is None or cp is None else share(payment, cp)
    return {
        "고객 부담(자기부담금)": _won(c["deductible"]),
        "보험사 지급(자차 보험금)": _won(payment),
        "상대방 부담 예상액(지급 전 예상치)": NOT_RECOVERABLE
        if cp == 0 and payment is not None
        else _won(cp_share),
        "보험사 최종 부담 예상액": _won(None if cp_share is None else payment - cp_share),  # type: ignore[operator]
    }


def missing_numeric_evidence(report: ReviewReport) -> list[int]:
    """수치를 담은 항목 중 근거 식별자가 없는 번호. 비어 있어야 정상."""
    return [
        s["no"]
        for s in report["sections"]  # type: ignore[misc]
        if s["state"] == "filled" and s["no"] in (3, 4, 6, 8, 9) and not s["evidence_ids"]
    ]


LIABILITY_STATUS = {"draft": "초안", "needs_input": "입력 필요", "needs_review": "담당자 확인"}


def _flags(flags: list) -> list[dict[str, object]]:
    return [{"항목": f["label"], "내용": f["detail"]} for f in flags]


def liability_body(r: LiabilityResult) -> dict[str, object]:
    """8번 「대물·대인 1차 판정」. 금액은 app/calculation/liability.py 결과 그대로(모르면 null)."""
    p = r["property"]
    body: dict[str, object] = {
        "주의": r["notice"],
        "우리 과실": None
        if r["insured_fault"] is None
        else f"{r['insured_fault']}%"
        + (" (협의 과실 적용)" if r.get("fault_source") == "agreed" else ""),
    }
    if p is not None:
        body["대물"] = {
            "상태": LIABILITY_STATUS[p["status"]],
            "상대 차 수리비": _won(p["repair_cost"]),
            "대물 초안": _won(p["amount"]),
            "대물 한도 적용": p["limit_applied"],
            "계산식": p["formula"],
            "확인 필요": _flags(p["flags"]),
            "기본 가정": p["assumptions"],
            "사유": p["reasons"],
        }
    if r["injuries"]:
        body["대인"] = [
            {
                "피해자": i.get("label") or i["person_id"],
                "상태": LIABILITY_STATUS[i["status"]],
                "상해 급수": None if i["grade"] is None else f"{i['grade']}급",
                "치료관계비": _won(i["treatment_cost"]),
                "부상 위자료": _won(i["consolation"]),
                "과실상계 후": _won(i["after_offset"]),
                "치료관계비 보장 적용": i["treatment_guarantee"],
                "대인Ⅰ 한도": _won(i["bodily_1_limit"]),
                "대인Ⅰ": _won(i["bodily_1"]),
                "대인Ⅱ": _won(i["bodily_2"]),
                "계산식": i["formula"],
                "확인 필요": _flags(i["flags"]),
                "기본 가정": i["assumptions"],
                "사유": i["reasons"],
            }
            for i in r["injuries"]
        ]
        drafts = [i for i in r["injuries"] if i["status"] == "draft"]
        if drafts and len(drafts) == len(r["injuries"]):
            body["대인 합계"] = {
                "대인Ⅰ": _won(sum(i["bodily_1"] or 0 for i in drafts)),
                "대인Ⅱ": _won(sum(i["bodily_2"] or 0 for i in drafts)),
            }
    return body


def liability_evidence(r: LiabilityResult) -> list[str]:
    """대물·대인 근거 ID: 상대 견적서, 조항 ID, schedule_value record_id."""
    ids: list[str] = []
    p = r["property"]
    if p is not None:
        ids += p["source_document_ids"] + p["clause_ids"]
        ids += [c for f in p["flags"] for c in f["clause_ids"]]
    for i in r["injuries"]:
        ids += i["clause_ids"] + [c for f in i["flags"] for c in f["clause_ids"]]
    return list(dict.fromkeys(ids))
