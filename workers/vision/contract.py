"""A: 비전 내부 카드(한국어 22칸) → 팀 계약 사실 카드 v1(영어 17칸, 2026-11-11 확정·11-13 5칸 추가).

내부 카드는 학습·평가에 쓰는 세분 값 그대로 두고, 판단(B)·화면(FE)에 넘길 때만 이 변환을 거친다.
채점(evals/vision/score_v1.py)도 같은 표를 쓴다. 표를 바꾸면 docs/contracts/fact-card-v1.md도 함께
바꾼다.

- 칸 하나의 모양은 app/schemas/facts.py FactField(+ vision_detail). 값과 model_value만 v1으로
  바꾸고,
  vision_detail에는 내부 값(internal_value)을 남긴다.
- '불명'·빈값은 null + needs_review. A/B는 그대로(B = 블랙박스 차).
- 담당자 입력 칸(MANUAL_FIELDS: 충돌 시점 신호·차로 노면표시·비보호 좌회전 표지)은 비전이 채우지
  않는다. null + unverified(출처 manual)로 내고 확정을 막지 않는다.
- 충돌 시각·근거 프레임 시각에는 영상을 잘라 분석했을 때 자른 시작 시각(offset_ms)을 더해 원본 영상
  기준으로 돌려준다.
"""

from __future__ import annotations

import copy

from app.schemas.facts import MANUAL_FIELDS

LOCATION = {
    "사거리_신호있음": "signalized_intersection",
    "사거리_신호없음": "unsignalized_intersection",
    "T자형교차로": "unsignalized_intersection",
    "회전교차로": "roundabout",
    "직선도로": "straight_road",
    "고속도로": "highway",
    "주차장": "other",
    "차도와비차도": "other",
}
ROAD = {
    "동일폭": "same_width",
    "A대로": "major_A",
    "B대로": "major_B",
    "A쪽_일시정지": "stop_A",
    "B쪽_일시정지": "stop_B",
    "A쪽만_신호등": "other",
    "B쪽만_신호등": "other",
    "해당없음": "none",
}
SIGNAL = {
    "녹색": "green",
    "황색": "yellow",
    "적색": "red",
    "녹색좌회전": "green_left_arrow",
    "녹색직진좌회전": "green_left_arrow",
    "신호없음": "no_signal",
}
MOVE = {
    "직진": "straight",
    "좌회전": "left_turn",
    "우회전": "right_turn",
    "유턴": "u_turn",
    "진로변경": "lane_change",
    "추월": "lane_change",
    "합류": "lane_change",
}
POSITION = {
    "같은방향_앞": "same_direction_front",
    "같은방향_뒤": "same_direction_rear",
    "맞은편": "opposite",
    "왼쪽": "left",
    "오른쪽": "right",
}
ENTRY = {"A먼저": "first", "B먼저": "second", "동시": "simultaneous", "해당없음": "none"}
ACCIDENT = {"차대차": "car_to_car"}

# v1 칸 → (내부 칸, 값 표). 값 표가 None이면 값을 그대로 둔다. 내부 칸이 None이면 담당자 입력 칸
V1_FIELDS: dict[str, tuple[str | None, dict | None]] = {
    "accident_type": ("accident_object", ACCIDENT),
    "location_type": ("location_type", LOCATION),
    "road_priority": ("road_priority", ROAD),
    "signal_A": ("signal_A", SIGNAL),
    "signal_B": ("signal_B", SIGNAL),
    "collision_signal_A": (None, None),
    "collision_signal_B": (None, None),
    "movement_A": ("movement_A", MOVE),
    "movement_B": ("movement_B", MOVE),
    "lane_marking_A": (None, None),
    "lane_marking_B": (None, None),
    "relative_position": ("relative_position", POSITION),
    "entry_order": ("entry_order", ENTRY),
    "night": ("time_condition", None),
    "unprotected_left_sign": (None, None),
    "collision_time_ms": ("collision_time_ms", None),
    "narrative": ("narrative", None),
}
# 채점에 쓰는 v1 핵심 칸(내부 칸 이름 기준)
CORE_MAPS: dict[str, dict] = {
    "location_type": LOCATION,
    "road_priority": ROAD,
    "signal_A": SIGNAL,
    "signal_B": SIGNAL,
    "movement_A": MOVE,
    "movement_B": MOVE,
    "relative_position": POSITION,
    "entry_order": ENTRY,
}
UNKNOWN = (None, "", "불명")

#  칸별 평가 정확도(v1 값 기준, 2026-11-11 evals/vision/score_v1.py). 담당자가 무엇을 먼저 볼지
# 정하는 데 쓴다.
# 장소·움직임은 사람 정답 92건, 나머지는 개발 세트 30건(개발에 써서 조금 부풀려질 수 있음).
# 모델을 바꾸면 score_v1.py로 다시 재고 이 표를 고친다.
FIELD_ACCURACY: dict[str, dict] = {
    "accident_type": {"accuracy": 1.00, "basis": "개발 세트 30건 (30/30)"},
    "location_type": {
        "accuracy": 0.80,
        "basis": "사람 정답 92건 (74/92, 사람 28 + AI Hub 라벨 64)",
    },
    "road_priority": {"accuracy": 0.70, "basis": "개발 세트 30건 (21/30)"},
    "signal_A": {"accuracy": 0.00, "basis": "개발 세트 신호 교차로 4건 (0/4), 늘 확인 필요"},
    "signal_B": {"accuracy": 0.00, "basis": "개발 세트 신호 교차로 4건 (0/4), 늘 확인 필요"},
    "movement_A": {"accuracy": 0.65, "basis": "사람 정답 92건 (60/92)"},
    "movement_B": {"accuracy": 0.75, "basis": "사람 정답 92건 (69/92)"},
    # 질문 v0.2로 잰 값. v0.5(같은 방향 옆 차로 규칙, 11/13)는 아직 다시 재지 않았다
    "relative_position": {"accuracy": 0.63, "basis": "개발 세트 30건, 좌·우 구분 (19/30)"},
    "entry_order": {"accuracy": 0.47, "basis": "개발 세트 30건 (14/30)"},
    "collision_time_ms": {"accuracy": 0.57, "basis": "개발 세트 30건, ±200ms 이내 (17/30)"},
}


def to_v1_value(field: str, value):
    """내부 칸 이름과 값 → v1 값. 불명·빈값 → None. 움직임의 목록 밖 값(주정차 등) → other."""
    if value in UNKNOWN:
        return None
    if field.startswith("movement_"):
        return MOVE.get(value, "other")
    if field == "accident_object":
        return ACCIDENT.get(value, "other")
    if field == "time_condition":
        return value == "야간"
    table = CORE_MAPS.get(field)
    return table.get(value, value) if table else value


def _with_eval(v1_name: str, out: dict) -> dict:
    """vision_detail.eval에 이 칸의 평가 정확도와 근거를 붙인다(평가하지 않은 칸은 없음)."""
    if v1_name in FIELD_ACCURACY:
        out["vision_detail"] = {**(out.get("vision_detail") or {}), "eval": FIELD_ACCURACY[v1_name]}
    return out


def _convert_field(v1_name: str, internal: str, field: dict, offset_ms: int) -> dict:
    return _with_eval(v1_name, _convert_value(v1_name, internal, field, offset_ms))


def _convert_value(v1_name: str, internal: str, field: dict, offset_ms: int) -> dict:
    out = copy.deepcopy(field)
    if v1_name == "collision_time_ms":
        for key in ("value", "model_value"):
            if isinstance(out.get(key), int | float):
                out[key] = int(out[key]) + offset_ms
        return out
    if v1_name == "narrative":
        return out
    out["value"] = to_v1_value(internal, field.get("value"))
    out["model_value"] = to_v1_value(internal, field.get("model_value"))
    if field.get("value") not in UNKNOWN and field.get("value") != out["value"]:
        out.setdefault("vision_detail", {})
        out["vision_detail"] = {**(out["vision_detail"] or {}), "internal_value": field["value"]}
    if out["value"] is None and v1_name != "night":
        out["verification_status"] = "needs_review"
    return out


def manual_field() -> dict:
    """비전이 채우지 않는 담당자 입력 칸. 값을 넣으면 PATCH가 human_confirmed로 바꾼다."""
    return {
        "value": None, "model_value": None, "source": "manual", "model_confidence": None,
        "verification_status": "unverified", "evidence_frame_ids": [], "source_document_ids": [],
    }


def to_contract_card(
    card: dict, *, accident_date: str | None = None, version: int = 1, offset_ms: int = 0
) -> dict:
    """내부 카드 → 팀 계약 v1 카드. 원본 카드는 바꾸지 않는다."""
    fields = card.get("fields", {})
    out_fields = {}
    for v1_name, (internal, _) in V1_FIELDS.items():
        if v1_name in MANUAL_FIELDS:
            out_fields[v1_name] = manual_field()
        elif internal in fields:
            out_fields[v1_name] = _convert_field(v1_name, internal, fields[internal], offset_ms)
    # 3대 이상이면 범위 밖(차대차 도표 아님)
    count = (fields.get("party_count") or {}).get("value")
    if isinstance(count, int) and count >= 3 and "accident_type" in out_fields:
        out_fields["accident_type"]["value"] = "other"
    out = {
        "case_id": card.get("case_id"),
        "version": version,
        "accident_date": accident_date or card.get("accident_date"),
        "fields": out_fields,
        "parties": copy.deepcopy(card.get("parties", [])),
        "party_mapping": copy.deepcopy(card.get("party_mapping", {})),
        "evidence_frames": [
            {**f, "timestamp_ms": (f.get("timestamp_ms") or 0) + offset_ms}
            for f in card.get("evidence_frames", [])
        ],
        "status": card.get("status", "draft"),
        "confirmed_by": card.get("confirmed_by"),
        "confirmed_at": card.get("confirmed_at"),
        "model_info": copy.deepcopy(card.get("model_info", {})),
    }
    return out
