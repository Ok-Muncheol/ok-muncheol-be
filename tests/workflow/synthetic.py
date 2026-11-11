"""판단 그래프 테스트용 합성 카드·도표 후보. 실제 도표 번호·비율·사건을 담지 않는다."""

import json
from copy import deepcopy

from app.schemas.facts import FactCard, FactField
from app.schemas.retrieval import EvidenceHit
from app.workflow.candidates import ChartCandidate

DOCUMENT = {
    "document_id": "synthetic-standards", "corpus": "fault_standards",
    "source_uri": "synthetic://standards", "edition": "synthetic-1",
    "effective_from": "2020-01-01", "effective_to": None,
    "content_hash": "0" * 64, "parser_version": "synthetic",
}


def field(value: object, confirmed: bool = True) -> FactField:
    return {
        "value": value, "model_value": None, "source": "manual", "model_confidence": None,  # type: ignore[typeddict-item]
        "verification_status": "human_confirmed" if confirmed else "needs_review",
        "evidence_frame_ids": [], "source_document_ids": ["synthetic-statement"],
    }


def card(**overrides: object) -> FactCard:
    """신호 교차로, A 녹색 직진 대 B 적색 좌회전. 전부 확인된 상태가 기본이다."""
    base: FactCard = {
        "case_id": "synthetic-case", "version": 3, "accident_date": "2026-10-01",
        "fields": {
            "accident_type": field("car_to_car"), "location_type": field("signalized_intersection"),
            "movement_A": field("straight"), "movement_B": field("left_turn"),
            "signal_A": field("green"), "signal_B": field("red"), "night": field(False),
        },
        "party_mapping": {
            "video_ego_party_id": None, "insured_party_id": "p-1",
            "chart_party_A_id": "p-1", "chart_party_B_id": "p-2",
        },
        "evidence_frames": [], "status": "confirmed", "confirmed_by": "reviewer",
        "confirmed_at": "2026-10-02T09:00:00+09:00",
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return deepcopy(base)


def v1_card(**fields: FactField) -> FactCard:
    """card()와 같은 사고를 카드 v1 칸·값(한국어)으로. 넘긴 칸만 바꾼다."""
    base: dict[str, FactField] = {
        "accident_object": field("차대차"), "party_count": field(2),
        "location_type": field("사거리_신호있음"),
        "movement_A": field("직진"), "movement_B": field("좌회전"),
        "signal_A": field("녹색"), "signal_B": field("적색"),
        "relative_position": field("맞은편"), "time_condition": field("주간"),
    }
    base.update(fields)
    return card(fields=base)


def en_v1_card(swapped: bool = False, **fields: FactField) -> FactCard:
    """card()와 같은 사고를 카드 v1 확정본(영어 12칸, #120)으로. 카드 B = 촬영 차량(P1).

    기본은 도표 A = 상대 차량(P2) = 카드 A다. `swapped`면 도표 A = 촬영 차량(P1) = 카드 B라서
    같은 사고를 적으려면 카드의 A/B 값이 맞바뀐다(v1 2절). 넘긴 칸만 바꾼다.
    """
    chart_a = {"movement": "straight", "signal": "green"}
    chart_b = {"movement": "left_turn", "signal": "red"}
    card_a, card_b = (chart_b, chart_a) if swapped else (chart_a, chart_b)
    base: dict[str, FactField] = {
        "accident_type": field("car_to_car"), "location_type": field("signalized_intersection"),
        "movement_A": field(card_a["movement"]), "movement_B": field(card_b["movement"]),
        "signal_A": field(card_a["signal"]), "signal_B": field(card_b["signal"]),
        "relative_position": field("opposite"), "night": field(False),
        "collision_time_ms": field(4000), "narrative": field("합성 서술"),
    }
    base.update(fields)
    c = card(fields=base)
    c["parties"] = [  # type: ignore[typeddict-unknown-key]
        {"party_id": "P1", "role": "촬영 차량", "track_id": None},
        {"party_id": "P2", "role": "상대 차량", "track_id": None},
    ]
    c["party_mapping"] = {
        "video_ego_party_id": "P1", "insured_party_id": None,
        "chart_party_A_id": "P1" if swapped else "P2",
        "chart_party_B_id": "P2" if swapped else "P1",
    }
    c["model_info"] = {"pipeline": "synthetic"}  # type: ignore[typeddict-unknown-key]
    return c


def hit(parent_id: str, page: int, score: float) -> EvidenceHit:
    return {
        "chunk": {
            "chunk_id": f"chunk-{parent_id}-{page}", "parent_id": parent_id, "document": DOCUMENT,
            "page_start": page, "page_end": page, "text": "synthetic", "metadata": {},
        },
        "score": score, "retrieval_version": "synthetic",
    }


def candidate(chart_id: str, attributes: dict[str, str], base_A: int, score: float = 1.0,
              **overrides: object) -> ChartCandidate:
    parent = f"{DOCUMENT['document_id']}:{chart_id}"
    base: ChartCandidate = {
        "chart_id": chart_id, "parent_id": parent, "document_id": DOCUMENT["document_id"],
        "edition": DOCUMENT["edition"], "effective_from": DOCUMENT["effective_from"],
        "effective_to": None, "title": f"synthetic {chart_id}", "page_start": 10, "page_end": 12,
        "attributes": {"accident_type": "car_to_car", **attributes},
        "base_variants": [{"variant": None, "base_A": base_A, "base_B": 100 - base_A,
                           "conditions": {}}],
        # 수정요소는 자동 적용하지 않으므로 C 레코드처럼 code·conditions를 비운다
        "modifiers": [
            {"modifier_id": f"{parent}:night", "party": "A", "name": "야간", "code": None,
             "delta": 5, "conditions": None},
            {"modifier_id": f"{parent}:gross", "party": "B", "name": "중대한 과실", "code": None,
             "delta": 20, "conditions": None},
        ],
        "calculation_ready": True, "hits": [hit(parent, 10, score)], "score": score,
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return deepcopy(base)


SIGNALIZED = {"location_type": "signalized_intersection", "movement_A": "straight",
              "signal_A": "green", "signal_B": "red"}


def candidates() -> list[ChartCandidate]:
    """같은 신호·A 직진에서 B의 진행 방향만 다른 두 후보."""
    return [
        candidate("S-1", {**SIGNALIZED, "movement_B": "left_turn"}, 20, score=0.9),
        candidate("S-2", {**SIGNALIZED, "movement_B": "straight"}, 50, score=0.8),
    ]


def verdict(holds: bool = False, kind: str | None = None, preferred: str | None = None,
            reasons: list[str] | None = None) -> str:
    if reasons is None:
        reasons = ["반박 사유"] if holds else []
    return json.dumps({"holds": holds, "kind": kind, "preferred_chart_id": preferred,
                       "reasons": reasons})
