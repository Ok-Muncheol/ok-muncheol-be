"""B: 검증 ① 근거 체크. 선택한 도표·수정요소·인용 청크가 검수한 레코드와 원문에 실재하는지 본다."""

from datetime import date

from app.schemas.judgment import ValidationCheck
from app.workflow.candidates import ChartCandidate
from app.workflow.state import ChartSelection


def edition_covers(candidate: ChartCandidate, accident_date: str) -> str | None:
    """사고 일자에 유효한 적용판인지. 문제가 있으면 이유를 돌려준다."""
    if not candidate["effective_from"]:
        return f"적용판 {candidate['edition']}의 유효기간이 확인되지 않았다"
    try:
        accident = date.fromisoformat(accident_date)
        start = date.fromisoformat(candidate["effective_from"])
        end = date.fromisoformat(candidate["effective_to"]) if candidate["effective_to"] else None
    except ValueError:
        return "사고 일자 또는 적용판 유효기간 형식이 잘못됐다"
    if accident < start or (end is not None and accident > end):
        return f"적용판 {candidate['edition']}은 사고 일자 {accident_date}에 유효하지 않다"
    return None


def check_evidence(
    selection: ChartSelection, candidates: list[ChartCandidate], accident_date: str
) -> ValidationCheck:
    """도표 ID·적용판·원문 페이지·수정요소 연결 대조. 생성 인용 차단."""
    reasons: list[str] = []
    candidate = next((c for c in candidates if c["chart_id"] == selection["chart_id"]), None)
    if candidate is None:
        return {
            "layer": "evidence", "passed": False,
            "reasons": [f"도표 {selection['chart_id']}가 검색 후보에 없다"],
        }
    if not candidate["calculation_ready"]:
        reasons.append(f"도표 {candidate['chart_id']}는 검수한 구조 레코드가 아니다")
    same_record = (candidate["parent_id"], candidate["edition"])
    if same_record != (selection["parent_id"], selection["edition"]):
        reasons.append("선택한 도표의 레코드 ID·적용판이 후보와 다르다")
    period = edition_covers(candidate, accident_date)
    if period:
        reasons.append(period)
    if not candidate["hits"]:
        reasons.append(f"도표 {candidate['chart_id']}를 뒷받침하는 원문 청크가 없다")
    for hit in candidate["hits"]:
        chunk = hit["chunk"]
        if chunk["parent_id"] != candidate["parent_id"]:
            reasons.append(f"인용 청크 {chunk['chunk_id']}가 도표 레코드와 연결되지 않는다")
        elif chunk["document"]["document_id"] != candidate["document_id"]:
            reasons.append(f"인용 청크 {chunk['chunk_id']}의 원문이 도표 원문과 다르다")
        elif not (candidate["page_start"] <= chunk["page_start"] <= chunk["page_end"]
                  <= candidate["page_end"]):
            reasons.append(f"인용 청크 {chunk['chunk_id']}의 페이지가 도표 범위 밖이다")
    if selection["variant"] not in candidate["base_variants"]:
        reasons.append("선택한 기본 과실 변형이 도표 레코드에 없다")
    known = {m["modifier_id"]: m for m in candidate["modifiers"]}
    for applied in selection["applied_modifiers"]:
        source = known.get(applied["modifier_id"])
        if source is None:
            reasons.append(f"수정요소 {applied['modifier_id']}가 도표 레코드에 없다")
        elif source["delta"] != applied["delta"] or source["party"] != applied["party"]:
            reasons.append(f"수정요소 {applied['modifier_id']}의 가감치·당사자가 레코드와 다르다")
    return {"layer": "evidence", "passed": not reasons, "reasons": reasons}
