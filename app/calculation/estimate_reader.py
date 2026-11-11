"""D: 자동차점검ㆍ정비견적서(자동차관리법 시행규칙 별지 제89호의3) 텍스트 PDF → RepairEstimateDoc.

- 서식: data/forms/car_estimate_form.pdf. 서식에 값이 찍힌 텍스트 PDF만 읽는다(스캔 이미지 OCR
  아님).
- 읽는 값: 총액, 일련번호, 발급 년월일, 견적구분(보험/일반), 부품·공임·부가가치세.
- read_by="pdf_extract": 담당자 확인 단계 없이 바로 계산에 쓴다(1차 판정 초안, 마지막 담당자 확인
  완료에서 확인).
- 총액을 못 찾으면 amount=None + "견적서 총액 인식 실패" (0으로 채우지 않음).
- 부품+공임+부가가치세 ≠ 총액이면 총액으로 계산하고 "견적서 합계 불일치 — 확인 필요".
- party_id는 업로드할 때 받은 값을 쓴다(차량번호 자동 매칭 안 함).
- pdfplumber는 기본 의존성(견적서 경로가 운영에서 쓴다). 그래도 없으면 이 기능만
  EstimateReaderUnavailable로 안내한다.
"""

import hashlib
import re
from datetime import date, datetime
from pathlib import Path

from app.calculation.damages import KST
from app.schemas.reports import RepairEstimateDoc

TOTAL_NOT_FOUND = "견적서 총액 인식 실패"
SUM_MISMATCH = "견적서 합계 불일치 — 확인 필요"
INSTALL_HINT = "견적서 PDF 읽기에는 pdfplumber가 필요합니다 (py -3.12 -m pip install pdfplumber)"

# 하단 합계표 머리행 → 필드. '계'(부품+공임)는 읽지 않는다.
_SUMMARY_HEADERS = {"부품": "parts", "공임": "labor", "부가가치세": "vat", "총액": "total"}
_CHECK = r"[VvXx✓✔Oo○●■√]"
_AMOUNT = re.compile(r"\d{1,3}(,\d{3})+원?|\d+원?")


class EstimateReaderUnavailable(RuntimeError):
    """pdfplumber 미설치."""


def _amount(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _summary_values(words: list[dict]) -> dict[str, int | None]:
    """'총액'과 같은 줄의 머리행을 찾아, 바로 아래 행 숫자를 가장 가까운 머리 칸에 넣는다."""
    totals = [w for w in words if w["text"] == "총액"]
    if not totals:
        return {}
    head = max(totals, key=lambda w: w["top"])  # 합계표는 품목표 아래
    row = [w for w in words if abs(w["top"] - head["top"]) < 3 and w["text"] in _SUMMARY_HEADERS]
    centers = {_SUMMARY_HEADERS[w["text"]]: (w["x0"] + w["x1"]) / 2 for w in row}
    # 값 행은 머리행 바로 아래 한 줄(약 16pt)뿐 — 그 아래 법 조문(제58조제5항)의 숫자를 읽지 않도록
    # 금액 모양만.
    below = [
        w
        for w in words
        if head["bottom"] < w["top"] < head["bottom"] + 16 and _AMOUNT.fullmatch(w["text"])
    ]
    values: dict[str, int | None] = {k: None for k in centers}
    for w in below:
        cx = (w["x0"] + w["x1"]) / 2
        nearest = min(centers, key=lambda k: abs(centers[k] - cx))
        if abs(centers[nearest] - cx) < 45:
            values[nearest] = _amount(w["text"])
    return values


def _serial(text: str) -> str | None:
    m = re.search(r"일련번호\s*:[^\S\n]*(\S+)", text)
    return m.group(1) if m else None


def _issued_on(text: str) -> str | None:
    # 서식의 '년 월 일' 줄은 공백 문자로 채워져 있어 찍힌 숫자가 공백과 섞여 나온다("2 0 2 6 년") →
    # 줄 안 공백 제거
    compact = re.sub(r"[^\S\n]+", "", text)
    m = re.search(r"(\d{4})년(\d{1,2})월(\d{1,2})일", compact)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _estimate_type(text: str) -> str | None:
    compact = re.sub(r"[^\S\n]+", "", text)
    ins = re.search(rf"\[{_CHECK}\]보험", compact)
    gen = re.search(rf"\[{_CHECK}\]일반", compact)
    if bool(ins) == bool(gen):
        return None  # 미체크·이중 체크는 판정하지 않음
    return "insurance" if ins else "general"


def read_estimate_pdf(
    path: str | Path, *, party_id: str, received_at: str | None = None
) -> RepairEstimateDoc:
    try:
        import pdfplumber
    except ImportError as exc:
        raise EstimateReaderUnavailable(INSTALL_HINT) from exc
    path = Path(path)
    data = path.read_bytes()
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        words = page.extract_words()

    v = _summary_values(words)
    total = v.get("total")
    parts, labor, vat = v.get("parts"), v.get("labor"), v.get("vat")
    reasons: list[str] = []
    if total is None:
        reasons.append(TOTAL_NOT_FOUND)
    elif (
        parts is not None and labor is not None and vat is not None and parts + labor + vat != total
    ):
        reasons.append(SUM_MISMATCH)
    elif None in (parts, labor, vat):
        reasons.append(SUM_MISMATCH)  # 합계를 검산할 수 없음

    return {
        "kind": "repair_estimate",
        "document_id": "est-pdf-" + hashlib.sha256(data).hexdigest()[:12],
        "party_id": party_id,
        "amount": total,
        "read_by": "pdf_extract",
        "verification_status": "unverified",
        "received_at": received_at or datetime.now(KST).isoformat(timespec="seconds"),
        "storage_path": str(path),
        "serial_no": _serial(text),
        "issued_on": _issued_on(text),
        "estimate_type": _estimate_type(text),  # type: ignore[typeddict-item]
        "parts": parts,
        "labor": labor,
        "vat": vat,
        "extract_reasons": reasons,
    }
