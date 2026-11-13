"""B: 도표 후보 계약과 C 검색 결과 → 후보 변환. 검수한 구조 레코드만 선택 대상이 된다.

후보의 `attributes`·`base_variants[].conditions`·`modifiers[].conditions`는
`app/schemas/chart_scope.py`의 칸 이름과 허용값(영문 enum)을 쓴다. C의 도표 레코드(`charts.jsonl`)는
아직 한국어 원문 낱말만 가지므로(`filter_status == "unreviewed_candidates_only"`), 레코드에
`scope_attributes`·`scope_variants`·`scope_modifiers`가 검수 상태로 더해질 때까지
`calculation_ready`는 False이고 선택되지 않는다.
값 `*`(`chart_scope.ANY_VALUE`)은 그 도표가 그 칸과 무관하다는 뜻이다.
카드는 `app/workflow/card_fields.py`로 같은 칸 이름·값으로 바꿔 대조한다(카드 v1 한국어 값 포함).

적용판·유효기간은 레코드 안의 문서 사본(파싱 시점 값)이 아니라 검증을 통과한 검색 결과의
`chunk.document`에서 읽는다. `validate_hits`가 이 값을 DB 색인(`corpus_documents` 조인)과
대조하므로, manifest를 고치면 레코드를 다시 만들지 않아도 반영된다.
발행월(`published_month`)도 같다. 레코드 사본은 같은 원문인지 확인하는 데만 쓴다.
"""

from typing import Literal, NotRequired, TypedDict

from app.schemas.facts import FactCard
from app.schemas.retrieval import EvidenceHit, SearchQuery
from app.workflow.card_fields import UNKNOWN, chart_view

# 판단 후보를 찾는 검색 깊이(청크 수). 검수한 도표도 이 안에 들어야 후보가 된다.
# 2026-11-11 시범 실행(RRF, 검수 도표 9개)에서 도표마다 그 도표에 맞는 카드로 검색하니, 대부분
# 상위 3위 안이었지만 차4-2는 첫 청크가 28위라 10개로는 빠졌다. 검수 도표가 늘어도 여유를 두려고
# 50으로 둔다. 늘어난 검수 전 도표는 보고서·보류 사유에서 앞쪽만 보여 준다(graph.py)
CHART_SEARCH_TOP_K = 50


class ChartVariant(TypedDict):
    variant: str | None
    base_A: int
    base_B: int
    # 이 변형이 적용되는 카드 칸 값(`*`은 무관). 비어 있으면 도표의 유일한 기본 비율이다.
    # 값이 여럿인 도표(예: 직진 또는 좌회전)는 값마다 변형을 나눠 같은 비율을 넣는다
    conditions: dict[str, str]


class ChartModifier(TypedDict):
    modifier_id: str
    party: Literal["A", "B"]
    name: str
    # MVP는 수정요소를 자동 적용하지 않는다. 과속·서행 불이행만 담당자 입력 전용 코드
    # (chart_scope.EXCLUDED_MODIFIERS·MANUAL_ONLY_MODIFIERS)로 표시하고 나머지는 None이다
    code: str | None
    delta: int | None
    # 적용 조건(카드 칸 값). 코드로 판정하지 않으므로 보통 None이다
    conditions: dict[str, str] | None


class ChartCandidate(TypedDict):
    chart_id: str
    parent_id: str
    document_id: str
    edition: str
    effective_from: str
    effective_to: str | None
    # 원문에서 확인한 발행월 "YYYY-MM"(시행일 아님). 모르면 None. 근거 검증 ①이 사고 일자와 대조한다
    published_month: NotRequired[str | None]
    title: str
    page_start: int
    page_end: int
    attributes: dict[str, str]
    base_variants: list[ChartVariant]
    modifiers: list[ChartModifier]
    calculation_ready: bool
    hits: list[EvidenceHit]
    score: float
    reasons: NotRequired[list[str]]


# 검색 질의 낱말(원문 표현). 우선 도로의 쪽(A·B)과 측면의 좌우는 원문 제목이 같은 낱말을 써서
# 낱말로는 가르지 않는다(도표 선택이 칸 값으로 가른다). `major_minor`·`stop_sign`은 11/6 카드용이다
_KOREAN_VALUES = {
    "signalized_intersection": "신호 교차로", "unsignalized_intersection": "신호 없는 교차로",
    "roundabout": "회전교차로", "same_width": "동일폭", "major_minor": "대로 소로",
    "major_A": "대로 소로", "major_B": "대로 소로", "stop_sign": "일시정지",
    "stop_A": "일시정지", "stop_B": "일시정지", "straight": "직진", "left_turn": "좌회전",
    "right_turn": "우회전", "u_turn": "유턴", "lane_change": "진로변경", "green": "녹색",
    "yellow": "황색", "red": "적색", "green_left_arrow": "녹색화살표", "no_signal": "신호 없음",
    "opposite": "맞은편", "left": "측면", "right": "측면", "first": "선진입",
    "second": "후진입", "simultaneous": "동시 진입",
    "flashing_yellow": "황색점멸", "flashing_red": "적색점멸",
}
# 차로 노면표시(차11-x 제목 표현). 값이 다른 칸과 겹치지 않게 따로 둔다
_LANE_WORDS = {
    "left_only": "좌회전 노면표시차로", "straight_left": "직진좌회전 노면표시차로",
    "straight_only": "직진 노면표시차로", "straight_right": "직진우회전 노면표시차로",
    "right_only": "우회전 노면표시차로",
}


def confirmed_values(card: FactCard) -> dict[str, str]:
    """담당자가 확인한 도표 선택 칸만 도표 선택 값(문자열)으로 돌려준다.

    미확인·범위 밖 칸과 `불명`(확인한 모름, `card_fields.confirmed_unknown_fields`)은 뺀다.
    """
    values: dict[str, str] = {}
    for key, field in chart_view(card).items():
        if field["verification_status"] != "human_confirmed":
            continue
        value = field["value"]
        if isinstance(value, bool):
            value = "true" if value else "false"
        if value is None or value == UNKNOWN:
            continue
        values[key] = str(value)
    return values


def build_search_query(card: FactCard, top_k: int = CHART_SEARCH_TOP_K) -> SearchQuery:
    """확인한 카드 값으로 인정기준 검색 문장을 만든다. 속성 필터는 검수 전이라 쓰지 않는다.

    검색 낱말이 정해진 도표 선택 값만 넣는다. `해당없음`과 대응 없는 카드 값은 넣지 않는다.
    """
    values = confirmed_values(card)

    def word(key: str) -> str | None:
        return _KOREAN_VALUES.get(values.get(key, ""))

    words = [w for k in ("location_type", "road_priority") if (w := word(k))]
    for side in ("A", "B"):
        parts = [w for k in (f"signal_{side}", f"movement_{side}") if (w := word(k))]
        if lane := _LANE_WORDS.get(values.get(f"lane_marking_{side}", "")):
            parts.append(lane)
        if parts:
            words.append(f"{side} {' '.join(parts)}")
    if position := word("relative_position"):
        words.append(position)
    if order := word("entry_order"):
        words.append(order)
    if values.get("unprotected_left_sign") == "true":
        words.append("비보호 좌회전")
    return {
        "text": " ".join(words) or "차대차 교차로 사고",
        # 사고 일자를 모르면 빈 값(검색 도구는 YYYY-MM-DD나 빈 값만 받는다)
        "accident_date": card.get("accident_date") or "", "filters": {}, "top_k": top_k,
    }


def candidates_from_hits(hits: list[EvidenceHit], records: dict[str, dict]) -> list[ChartCandidate]:
    """검색 결과를 도표 단위 후보로 묶는다. records는 parent_id → 도표 레코드다.

    수정요소 해설 청크(도표 레코드 없음)는 건너뛴다. 같은 도표의 여러 청크는 한 후보로 합치고
    점수는 최고 점수를 쓴다. 검수 속성이 없는 레코드는 calculation_ready=False로 들어간다.
    """
    grouped: dict[str, list[EvidenceHit]] = {}
    for hit in hits:
        parent_id = hit["chunk"]["parent_id"]
        if parent_id in records:
            grouped.setdefault(parent_id, []).append(hit)
    candidates = [candidate_from_record(records[pid], chart_hits)
                  for pid, chart_hits in grouped.items()]
    return sorted(candidates, key=lambda c: -c["score"])


def candidate_from_record(record: dict, hits: list[EvidenceHit]) -> ChartCandidate:
    """도표 레코드 하나와 그 도표의 검색 결과로 후보를 만든다. 문서 값은 검색 결과에서 읽는다.

    검색 결과끼리 또는 레코드와 원문 문서가 다르면 색인이 어긋난 것이라 예외를 낸다(fail-closed).
    """
    document = hits[0]["chunk"]["document"]
    if any(hit["chunk"]["document"] != document for hit in hits) or (
        document["document_id"] != record["document"]["document_id"]
    ):
        raise ValueError(f"도표 {record['chart_id']}의 검색 결과와 레코드 원문 문서가 다르다")
    attributes = record.get("scope_attributes") or {}
    variants = record.get("scope_variants")
    modifiers = record.get("scope_modifiers")
    reviewed = (
        record.get("review_status") == "reviewed"
        and bool(record.get("calculation_ready"))
        and isinstance(attributes, dict) and bool(attributes)
        and isinstance(variants, list) and bool(variants)
    )
    reasons = [] if reviewed else ["검수 전 도표 레코드: 선택·계산에 쓰지 않음"]
    return {
        "chart_id": record["chart_id"], "parent_id": record["parent_id"],
        "document_id": document["document_id"], "edition": document["edition"],
        "effective_from": document.get("effective_from") or "",
        "effective_to": document.get("effective_to"),
        "published_month": document.get("published_month"),
        "title": record.get("title", ""), "page_start": record["page_start"],
        "page_end": record["page_end"],
        "attributes": dict(attributes) if reviewed else {},
        "base_variants": list(variants) if reviewed else [],
        "modifiers": list(modifiers or []) if reviewed else [],
        "calculation_ready": reviewed,
        "hits": list(hits), "score": max(h["score"] for h in hits), "reasons": reasons,
    }


async def retrieve_from_corpus(card: FactCard) -> list[ChartCandidate]:
    """기본 후보 검색: C의 인정기준 검색툴 → DB 색인의 도표 구조 레코드(`EvidenceStore.get_parent`).

    검색 색인(D `configure_retrieval`)이나 색인 어댑터(C `configure_evidence_store`)가 없으면
    그 모듈의 오류가 그대로 올라간다. "근거 없음" 보류로 숨기지 않는다.
    """
    from app.tools._evidence import evidence_store
    from app.tools.fault_standards import search_fault_standards

    hits = await search_fault_standards(build_search_query(card))
    if not hits:
        return []
    store = evidence_store()
    records: dict[str, dict] = {}
    for parent_id in dict.fromkeys(hit["chunk"]["parent_id"] for hit in hits):
        record = await store.get_parent("fault_standards", parent_id)
        if record is not None:  # 공통 수정요소 해설 청크는 부모 레코드가 없다
            records[parent_id] = record
    return candidates_from_hits(hits, records)
