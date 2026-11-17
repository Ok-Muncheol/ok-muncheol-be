"""전원: MVP 도표 범위 상수. 근거는 docs/contracts/mvp-chart-scope.md (2026-11-06 결정, 11-16 확대).

A 카드 옵션, B 질문 분기·도표 선택, C 도표 레코드 속성, D 검색 필터가 같은 값을 쓴다.
칸 이름과 값은 사실 카드 v1과 같다(`docs/contracts/fact-card-v1.md`, A `facts.FACT_FIELD_OPTIONS`).
C 도표 레코드는 이 값을 도표 A 기준으로 적는다. 사고 유형은 범위 판정용이라 범위 안 값만 둔다.
2026-11-16: 1번 자료 제2장(자동차와 자동차, 이륜차 포함) 도표 108개 중 교차로(차1~21, 59개) 밖
49개(차31~61)도 다룬다. 일반 도로·고속도로·주차장·회전교차로를 범위에 넣고 그 도표를 가르는 칸을
더했다(김지은 안). 차48-1(적재물 낙하)·53-1(보행자 신호)은 칸이 없어 검수하지 않는다.
"""

from typing import Literal

# 범위 축 1·2: 차대차(제2장, 이륜차 포함). 장소는 `other`(차도와 차도 아닌 곳 등)만 범위 밖이다
AccidentType = Literal["car_to_car"]
LocationType = Literal[
    "signalized_intersection", "unsignalized_intersection", "roundabout",
    "straight_road", "highway", "parking_lot", "other",
]
IN_SCOPE_ACCIDENT_TYPES: tuple[AccidentType, ...] = ("car_to_car",)
IN_SCOPE_LOCATION_TYPES: tuple[LocationType, ...] = (
    "signalized_intersection", "unsignalized_intersection", "roundabout",
    "straight_road", "highway", "parking_lot",
)

# 도표 선택 칸과 허용값. 일방통행·속도 칸은 없으므로 범위에 넣지 않는다.
# 도로 관계는 우선 도로가 어느 당사자 쪽인지까지 적는다: `major_A` = A가 대로, `stop_A` = A 쪽에
# 일시정지 표지. `other`는 한쪽만 신호등, `none`은 해당 없음이다(v1 4절).
RoadPriority = Literal["same_width", "major_A", "major_B", "stop_A", "stop_B", "other", "none"]
# 11/16: 정차·정차 후 출발·후진·추월, 도로 밖에서 진입·도로 밖으로 진출, 주차구획 출차·진입.
# 합류(차43-1)는 `lane_change` + `lane_reduction`으로 가른다
Movement = Literal[
    "straight", "left_turn", "right_turn", "u_turn", "lane_change",
    "stopped", "starting", "reversing", "overtaking",
    "entering_road", "leaving_road", "parking_out", "parking_in", "other",
]
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
# 11/16 교차로 밖 도표 칸(아래 FIELD_OPTIONS 주석). 쪽이 없는 칸이라 A/B를 맞바꿔도 값이 같다
OvertakeLine = Literal["yellow_solid", "yellow_dashed", "white_solid", "no_passing_zone", "none"]
UTurnZone = Literal["always", "signal", "none"]
UTurnTiming = Literal["sudden", "simultaneous"]
LaneChangeTiming = Literal["simultaneous", "follower_first", "leader_first"]
SafetyZone = Literal["before_leaving", "after_leaving", "none"]
RoundaboutLanes = Literal["one", "two_plus"]
RoundaboutMove = Literal["circulating", "entering", "exiting"]
RoundaboutLane = Literal["inner", "outer"]
_BOOL = ("true", "false")

ChartFieldKey = Literal[
    "accident_type", "location_type", "road_priority",
    "movement_A", "movement_B", "signal_A", "signal_B",
    "collision_signal_A", "collision_signal_B", "lane_marking_A", "lane_marking_B",
    "relative_position", "entry_order", "unprotected_left_sign", "night",
    "two_wheeler_A", "two_wheeler_B", "emergency_vehicle_A", "emergency_vehicle_B",
    "center_line_crossing_A", "center_line_crossing_B", "door_open_A", "door_open_B",
    "overtake_line", "u_turn_zone", "u_turn_timing", "lane_change_timing", "lane_reduction",
    "shoulder", "traffic_jam", "secondary_collision", "bus_stop", "safety_zone",
    "roundabout_lanes", "roundabout_move_A", "roundabout_move_B", "roundabout_lane_A",
    "roundabout_lane_B",
]
# 11/13 카드 v1에 담당자 입력 칸 5개가 더해졌다(정지원 #160, 11/16 칸은 아래).
# 영상 카드는 null + unverified로 내보내므로 판단은 그 칸으로 갈리는 도표가 후보에 남을 때만
# 구분 질문으로 묻는다.
# - collision_signal_A/B: 충돌 순간 신호. signal_A/B는 진입 시점 신호다(차1-2·2-3·2-5·3-5)
# - lane_marking_A/B: 그 당사자가 있던 차로의 노면표시(차11-x)
# - unprotected_left_sign: 비보호 좌회전 표지가 있는가(차2-2·2-6·4-2)
# 11/16 교차로 밖 도표(차31~61) 칸. 이륜차·긴급자동차는 영상 AI가 채워 늘 확인(2단계 전까지 빈 칸),
# 나머지는 담당자 입력 칸이라 그 칸으로 갈리는 도표가 후보에 남을 때만 묻는다.
# - two_wheeler_*·emergency_vehicle_*: 이륜차(차45-2·61-x)·긴급 운행 중인 긴급자동차(차55-x)
# - center_line_crossing_*·door_open_*: 중앙선 침범(차31-x)·문 열림(차52-1·61-3)
# - overtake_line: 추월한 곳의 차선. `no_passing_zone` = 앞지르기 금지 장소(차45-x)
# - u_turn_zone(상시·신호 유턴, 차33-x)·u_turn_timing(급·동시 유턴, 차46-1)
# - lane_change_timing: 두 차 모두 진로변경일 때 순서. simultaneous 양옆 차로에서 동시에(차43-3),
#   follower_first 같은 차로 뒤차가 먼저(43-5), leader_first 앞차가 먼저(도표 없음, 보류)
# - lane_reduction(합류 차43-1)·shoulder(갓길·가장자리 차42-2·42-3·43-6)·traffic_jam(정체
#   차43-4·45-4·61-1)·secondary_collision(앞차가 사고로 서 있음 차42-1)·bus_stop(차47-3)
# - safety_zone: 안전지대 통과 직진(차43-7 벗어나기 전·후)
# - roundabout_lanes(회전 차로 수)·roundabout_move_*(회전 중·진입·진출하려고 진로변경)·
#   roundabout_lane_*(충돌 때 있던 회전 차로): 차54-1~5
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
    "unprotected_left_sign": _BOOL,
    "night": _BOOL,
    "two_wheeler_A": _BOOL,
    "two_wheeler_B": _BOOL,
    "emergency_vehicle_A": _BOOL,
    "emergency_vehicle_B": _BOOL,
    "center_line_crossing_A": _BOOL,
    "center_line_crossing_B": _BOOL,
    "door_open_A": _BOOL,
    "door_open_B": _BOOL,
    "overtake_line": OvertakeLine.__args__,
    "u_turn_zone": UTurnZone.__args__,
    "u_turn_timing": UTurnTiming.__args__,
    "lane_change_timing": LaneChangeTiming.__args__,
    "lane_reduction": _BOOL,
    "shoulder": _BOOL,
    "traffic_jam": _BOOL,
    "secondary_collision": _BOOL,
    "bus_stop": _BOOL,
    "safety_zone": SafetyZone.__args__,
    "roundabout_lanes": RoundaboutLanes.__args__,
    "roundabout_move_A": RoundaboutMove.__args__,
    "roundabout_move_B": RoundaboutMove.__args__,
    "roundabout_lane_A": RoundaboutLane.__args__,
    "roundabout_lane_B": RoundaboutLane.__args__,
}

# 도표 선택 전에 human_confirmed 여야 하는 칸. 진행 방향(정확도 약 40%)은 항상 질문한다.
# 장소별 필수 칸은 그 장소 도표가 모두 정의해야 한다(`*`도 정의). 11/16:
# - 일반 도로·고속도로: 원문이 마주 보는 방향(나. 차31~33)과 같은 방향(다. 차41~48)으로 먼저 나뉘어
#   상대 위치를 더한다
# - 회전교차로: 진행 방향 대신 회전 차로 수·회전 중/진입/진출로 갈린다(차54-1~5)
REQUIRED_CONFIRMED_FIELDS: dict[LocationType, tuple[ChartFieldKey, ...]] = {
    "signalized_intersection": (
        "accident_type", "location_type", "movement_A", "movement_B", "signal_A", "signal_B",
    ),
    "unsignalized_intersection": (
        "accident_type", "location_type", "road_priority",
        "movement_A", "movement_B", "entry_order",
    ),
    "roundabout": (
        "accident_type", "location_type", "roundabout_lanes",
        "roundabout_move_A", "roundabout_move_B",
    ),
    "straight_road": (
        "accident_type", "location_type", "movement_A", "movement_B", "relative_position",
    ),
    "highway": (
        "accident_type", "location_type", "movement_A", "movement_B", "relative_position",
    ),
    "parking_lot": ("accident_type", "location_type", "movement_A", "movement_B"),
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
REASON_NO_REVIEWED_CHART = "검수한 도표 근거 없음"
REASON_FIELD_NOT_CONFIRMED = "도표 선택 칸 미확인: {field_key}"
# 고를 도표가 없고, 후보 도표 가운데 카드의 두 차 움직임 조합(어느 쪽이 A든)을 다루는 도표가 하나도
# 없을 때 "검수한 도표 근거 없음" 바로 다음에 붙이는 줄(graph.no_shape_lines). "카드와 불일치" 줄만
# 보면 칸을 고치면 될 것처럼 읽히는데, 사고 모양의 도표가 없으면 고쳐도 고를 수 없다는 것을
# 먼저 알린다
# (2026-11-17 앞차 후진 대 정차 사건). 칸 이름을 넣지 않는다 — FE가 칸 이름을 카드 칸 링크로 바꾼다
REASON_NO_SHAPE = (
    "후보 도표 중에 이 움직임 조합({pair})을 다루는 도표가 없다. "
    "인정기준에 이 사고 모양의 도표가 없으면 카드 칸을 고쳐도 고를 수 없다"
)
# 고를 도표가 없을 때 도표 A/B를 맞바꿔 대조한 줄의 머리말(graph.swap_hints). FE가 이 머리말로 줄을
# 모아 보인다
REASON_SWAP_HINT = "도표 A/B를 맞바꾸면"

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
