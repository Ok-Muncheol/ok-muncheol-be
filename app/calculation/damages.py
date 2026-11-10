"""D: 1차 확장 자차 인정 손해액. 결측을 0원으로 보지 않는다.

- 담당자가 확인한 정비 견적서가 있으면 가장 최근 것을 쓴다(분해 후 수정 견적 반영).
- 서식 PDF에서 자동 추출한 견적(read_by="pdf_extract")은 담당자 확인 없이 쓴다(1차 판정 초안, 담당자
  확인 완료에서 확인).
  총액을 못 읽었으면 금액 없이 needs_documents, 합계 불일치는 계산하되 사유를 남긴다.
- OCR로 읽고 아직 확인하지 않은 견적은 금액을 넣지 않고 needs_documents로 둔다.
- 확정 견적보다 나중에 접수된 미확인 견적이 있으면 금액을 넣지 않고 needs_documents로 둔다.
- 견적서가 없으면 손해액은 항상 null(미정). 평균 수리비 참고값(추정)은 1차에서 제외했다.
"""

from datetime import datetime, timedelta, timezone

from app.schemas.reports import DamageResult

KST = timezone(timedelta(hours=9))


def received_at(doc: dict[str, object]) -> datetime:
    """접수 시각을 실제 시각으로 비교한다(문자열 비교 금지). 시간대 표기가 없으면 KST로 본다."""
    ts = datetime.fromisoformat(str(doc["received_at"]))
    return ts if ts.tzinfo else ts.replace(tzinfo=KST)


def pdf_source_label(doc: dict[str, object]) -> str:
    return f"견적서 PDF 자동 추출 (일련번호 {doc.get('serial_no') or '인식 실패'})"


def _extract_reasons(docs: list[dict[str, object]]) -> list[str]:
    return list(dict.fromkeys(r for d in docs for r in (d.get("extract_reasons") or [])))  # type: ignore[union-attr]


def calculate_damages(
    documents: list[dict[str, object]], *, party_id: str | None = None
) -> DamageResult:
    """견적 확정/서류 대기를 분리. 결측을 0원으로 보지 않는다(견적서 없음 → null)."""
    estimates = [
        d
        for d in documents
        if d.get("kind") == "repair_estimate"
        and (party_id is None or d.get("party_id") == party_id)
    ]
    confirmed = [
        d
        for d in estimates
        if d.get("amount") is not None
        and (
            d.get("verification_status") == "human_confirmed"
            or d.get("read_by") in ("manual", "pdf_extract")
        )
    ]
    if confirmed:
        latest = max(confirmed, key=lambda d: (received_at(d), str(d["document_id"])))
        newer = [
            d for d in estimates if d not in confirmed and received_at(d) > received_at(latest)
        ]
        if newer:
            ids = [str(d["document_id"]) for d in newer]
            return {
                "status": "needs_documents",
                "party_id": party_id,
                "amount": None,
                "source_document_ids": [str(latest["document_id"]), *ids],
                "reasons": [
                    f"더 최신 견적 확인 필요 — 확정 견적({latest['document_id']})보다 나중에 "
                    "접수된 "
                    f"미확인 견적({', '.join(ids)})",
                    *_extract_reasons(newer),
                ],
            }
        reasons = [f"확정 견적 {len(confirmed)}건 중 최신 사용"] if len(confirmed) > 1 else []
        result: DamageResult = {
            "status": "confirmed",
            "party_id": party_id,
            "amount": int(latest["amount"]),
            "source_document_ids": [str(latest["document_id"])],
            "reasons": reasons,
        }
        if latest.get("read_by") == "pdf_extract":
            result["source_label"] = pdf_source_label(latest)
            doc_reasons = _extract_reasons([latest])
            if doc_reasons:
                result["reasons"] = doc_reasons + reasons
                result["document_reasons"] = doc_reasons
        return result
    if estimates:
        ids = [str(d["document_id"]) for d in estimates]
        pdf = [d for d in estimates if d.get("read_by") == "pdf_extract"]
        reasons = _extract_reasons(pdf) if pdf else []
        if len(pdf) < len(estimates):
            reasons.append("OCR로 읽은 견적 금액 — 담당자 확인 전")
        pending: DamageResult = {
            "status": "needs_documents",
            "party_id": party_id,
            "amount": None,
            "source_document_ids": ids,
            "reasons": reasons,
        }
        if pdf:
            pending["source_label"] = pdf_source_label(
                max(pdf, key=lambda d: (received_at(d), str(d["document_id"])))
            )
        return pending
    return {
        "status": "needs_documents",
        "party_id": party_id,
        "amount": None,
        "source_document_ids": [],
        "reasons": ["견적서 없음 — 손해액 미확정"],
    }
