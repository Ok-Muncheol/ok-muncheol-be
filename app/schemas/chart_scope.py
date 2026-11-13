"""전원: MVP 도표 범위 상수. 근거는 docs/contracts/mvp-chart-scope.md (2026-11-06 결정).

A 카드 옵션, B 질문 분기·도표 선택, C 도표 레코드 속성, D 검색 필터가 같은 값을 쓴다.
도로 관계·신호·진행 방향·상대 위치·진입 순서 값은 사실 카드 v1 확정본 8절대로다(2026-11-11,
`docs/contracts/fact-card-v1.md`, A `facts.FACT_FIELD_OPTIONS`). C 도표 레코드는 이 값을
도표 A 기준으로 적는다. 사고 유형·장소는 범위 판정용이라 v1 값 중 범위 안 값만 둔다
(직선도로·고속도로는 판단이 `other`로 읽는다).
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
# 도로 관계는 우선 도로가 어느 당사자 쪽인지까지 적는다: `major_A` = A가 대로, `stop_A` = A 쪽에
# 일시정지 표지. `other`는 한쪽만 신호등, `none`은 해당 없음이다(v1 4절).
RoadPriority = Literal["same_width", "major_A", "major_B", "stop_A", "stop_B", "other", "none"]
Movement = Literal["straight", "left_turn", "right_turn", "u_turn", "lane_change", "other"]
# 점멸 신호(flashing_*)는 11/13 카드 v1에 추가(차1-5·3-7·3-8)
Signal = Literal[
    "green", "yellow", "red", "green_left_arrow", "flashing_yellow", "flashing_red", "no_signal",
]
# 상대 위치: B가 가는 방향에서 볼 때 A가 충돌 직전 있던 쪽(어디서 왔나). `left` = A가 B의
# 왼쪽 도로에서 진입, `opposite` = 마주 오는 차(v1 4절). 맞은편·측면·좌우로 갈리는 도표에 쓴다
RelativePosition = Literal[
    "same_direction_front", "same_direction_rear", "opposite", "left", "right",
]
EntryOrder = Literal["first", "second", "simultaneous", "none"]
# 그 당사자가 있던 차로의 방향 노면표시(화살표). 차11-1~11-6(교차로 노면 표시 위반)이
# 이것으로 갈린다. 실선·점선(차로 변경)이 아니다. `none` = 표시 없음
LaneMarking = Literal[
    "left_only", "straight_left", "straight_only", "straight_right", "right_only", "none",
]

ChartFieldKey = Literal[
    "accident_type", "location_type", "road_priority",
    "movement_A", "movement_B", "signal_A", "signal_B",
    "collision_signal_A", "collision_signal_B", "lane_marking_A", "lane_marking_B",
    "relative_position", "entry_order", "unprotected_left_sign", "night",
]
# 11/13 카드 v1에 담당자 입력 칸 5개가 더해졌다(정지원 #160). 영상 카드는 null + unverified로
# 내보내므로 판단은 그 칸으로 갈리는 도표가 후보에 남을 때만 구분 질문으로 묻는다.
# - collision_signal_A/B: 충돌 순간 신호. signal_A/B는 진입 시점 신호다(차1-2·2-3·2-5·3-5)
# - lane_marking_A/B: 그 당사자가 있던 차로의 노면표시(차11-x)
# - unprotected_left_sign: 비보호 좌회전 표지가 있는가(차2-2·2-6·4-2)
FIELD_OPTIONS: dict[str, tuple[str, ...]] = {
    "accident_type": ("car_to_car",),
    "location_type": LocationType.__args__,
    "road_priority": RoadPriority.__args__,
    "movement_A": Movement.__args__,
    "movement_B": Movement.__args__,
    "signal_A": Signal.__args__,
    "signal_B": Signal.__args__,
    "collision_signal_A": Signal.__args__,
    "collision_signal_B": Signal.__args__,
    "lane_marking_A": LaneMarking.__args__,
    "lane_marking_B": LaneMarking.__args__,
    "relative_position": RelativePosition.__args__,
    "entry_order": EntryOrder.__args__,
    "unprotected_left_sign": ("true", "false"),
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

# C 도표 레코드의 `scope_attributes`·`scope_variants[].conditions` 값으로만 쓰는 "무관" 표기.
# 그 도표가 이 칸으로 갈리지 않는다는 뜻이다. 카드 값이 무엇이든, 담당자가 모른다고 해도
# 맞는 것으로 본다. 칸을 아예 빼면 "미정의"(검수 누락)라 필수 칸 검사에서 후보가 빠진다.
ANY_VALUE = "*"

# 수정요소는 MVP에서 자동 적용하지 않는다(2026-11-10 결정). 2023.6 도표 표에는 카드 칸만으로
# 판정할 수 있는 행이 없다. 야간 가산은 공통 수정요소이고 조건이 "등화 미점등·감속 불이행"이다.
# 도표 표의 "신호불이행·지연"은 방향지시기 신호이고, 신호 위반은 기본 비율(도표 선택)에 들어 있다.
# 모든 수정요소는 담당자 판단(불명)으로 넘기고, 아래 코드는 그중 담당자 입력 전용임을 표시한다.
# 과속·서행 불이행은 영상으로 속도를 판정하지 않는다(README).
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
