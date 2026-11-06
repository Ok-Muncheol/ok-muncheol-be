"""전원: MVP 도표 범위 상수. 근거는 docs/contracts/mvp-chart-scope.md (2026-11-06 결정).

A 카드 옵션, B 질문 분기·도표 선택, C 도표 레코드 속성, D 검색 필터가 같은 값을 쓴다.
값 이름은 제안이며 A가 facts 칸을 확정하면 여기와 문서를 함께 바꾼다.
"""

from typing import Literal

# 범위 축 1·2: 차대차 교차로만. 회전교차로는 팀 결정 전까지 범위 밖으로 취급한다.
AccidentType = Literal["car_to_car"]
LocationType = Literal[
    "signalized_intersection", "unsignalized_intersection", "roundabout", "other"
]
IN_SCOPE_ACCIDENT_TYPES: tuple[AccidentType, ...] = ("car_to_car",)
IN_SCOPE_LOCATION_TYPES: tuple[LocationType, ...] = (
    "signalized_intersection", "unsignalized_intersection",
)
PENDING_DECISION_LOCATION_TYPES: tuple[LocationType, ...] = ("roundabout",)

# 도표 선택 칸과 허용값. 일방통행·속도 칸은 없으므로 범위에 넣지 않는다.
RoadPriority = Literal["same_width", "major_minor", "stop_sign"]
Movement = Literal["straight", "left_turn", "right_turn", "u_turn"]
Signal = Literal["green", "yellow", "red", "green_left_arrow", "no_signal"]
EntryOrder = Literal["first", "second", "simultaneous"]

ChartFieldKey = Literal[
    "accident_type", "location_type", "road_priority",
    "movement_A", "movement_B", "signal_A", "signal_B", "entry_order", "night",
]
FIELD_OPTIONS: dict[str, tuple[str, ...]] = {
    "accident_type": ("car_to_car",),
    "location_type": LocationType.__args__,
    "road_priority": RoadPriority.__args__,
    "movement_A": Movement.__args__,
    "movement_B": Movement.__args__,
    "signal_A": Signal.__args__,
    "signal_B": Signal.__args__,
    "entry_order": EntryOrder.__args__,
    "night": ("true", "false"),
}

# 도표 선택 전에 human_confirmed 여야 하는 칸. 진행 방향(정확도 약 40%)은 항상 질문한다.
REQUIRED_CONFIRMED_FIELDS: dict[LocationType, tuple[ChartFieldKey, ...]] = {
    "signalized_intersection": (
        "accident_type", "location_type", "movement_A", "movement_B", "signal_A", "signal_B",
    ),
    "unsignalized_intersection": (
        "accident_type", "location_type", "road_priority",
        "movement_A", "movement_B", "entry_order",
    ),
    "roundabout": ("accident_type", "location_type", "movement_A", "movement_B"),
    "other": ("accident_type", "location_type"),
}

# 수정요소. 과속·서행 불이행은 자동 후보에 올리지 않는다(README: 영상으로 속도를 판정하지 않음).
Modifier = Literal["night", "signal_violation"]
MODIFIER_REQUIRED_CONFIRMED_FIELDS: dict[Modifier, tuple[ChartFieldKey, ...]] = {
    "night": ("night",),
    "signal_violation": ("signal_A", "signal_B"),
}
MANUAL_ONLY_MODIFIERS: tuple[str, ...] = ("slow_down_failure",)
EXCLUDED_MODIFIERS: tuple[str, ...] = ("speeding",)
MANUAL_MODIFIER_SOURCE = "담당자 입력"

# 보류·질문 사유 문구. 기존 "검수한 도표 근거 없음"과 구분한다.
REASON_OUT_OF_SCOPE = "MVP 범위 밖 사고 유형(도표 미적재)"
REASON_PENDING_DECISION = "MVP 포함 여부 미결정(회전교차로)"
REASON_NO_REVIEWED_CHART = "검수한 도표 근거 없음"
REASON_FIELD_NOT_CONFIRMED = "도표 선택 칸 미확인: {field_key}"

# C 도표 레코드·D 필터 검색이 공유하는 fault_standards 속성 키.
CHART_FILTER_KEYS: tuple[str, ...] = (
    "accident_type", "location_type", "road_priority", "movement_A", "movement_B",
)


def unconfirmed_fields(fields: dict[str, dict[str, object]], location_type: str) -> list[str]:
    """카드 칸 중 도표 선택 전 질문이 필요한 칸. 값만 있고 사람 확인이 없으면 포함한다."""
    required = REQUIRED_CONFIRMED_FIELDS.get(location_type, REQUIRED_CONFIRMED_FIELDS["other"])  # type: ignore[arg-type]
    return [
        key for key in required
        if fields.get(key, {}).get("verification_status") != "human_confirmed"
    ]
