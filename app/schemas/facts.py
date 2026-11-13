"""A: 영상/수동 입력 공통 사실 카드. 모델 원값과 사람의 확인을 분리한다.

칸과 값은 docs/contracts/fact-card-v1.md(영어 17칸). 비전 내부 한국어 값은
workers/vision/contract.py가 이 값으로 바꾼다.
"""

from typing import Literal, NotRequired, TypedDict

FactValue = str | int | float | bool | None


class EvidenceFrame(TypedDict):
    frame_id: str
    timestamp_ms: int
    storage_path: str | None  # 근거 사진을 Storage에 올리기 전에는 null


class FactField(TypedDict):
    value: FactValue
    model_value: FactValue
    source: Literal["vision", "manual", "statement"]
    model_confidence: float | None
    verification_status: Literal["unverified", "needs_review", "human_confirmed"]
    evidence_frame_ids: list[str]
    source_document_ids: list[str]
    # 영상 분석 칸만: 모델별 의견·내부 값·칸별 평가 정확도(eval). 담당자가 고쳐도 바꾸지 않는다
    vision_detail: NotRequired[dict[str, object]]


class PartyMapping(TypedDict):
    video_ego_party_id: str | None
    insured_party_id: str | None
    chart_party_A_id: str | None
    chart_party_B_id: str | None


class Party(TypedDict):
    party_id: str
    role: str
    track_id: int | None


class FactCard(TypedDict):
    case_id: str
    version: int
    accident_date: str | None
    fields: dict[str, FactField]
    party_mapping: PartyMapping
    evidence_frames: list[EvidenceFrame]
    status: Literal["draft", "confirmed"]
    confirmed_by: str | None
    confirmed_at: str | None
    parties: NotRequired[list[Party]]
    model_info: NotRequired[dict[str, object]]  # 쓴 모델·질문 버전·시간. 판단에는 쓰지 않음


class FactFieldPatch(TypedDict):
    """고칠 값만 보낸다. FactField 전체를 보내도 된다(나머지 키는 무시, 모델 원값은 서버가
    지킨다)."""

    value: FactValue


class FactPatch(TypedDict):
    expected_version: int
    # 고친 칸만. 이 칸들은 human_confirmed가 된다(null = 확인한 모름)
    fields: dict[str, FactFieldPatch]
    party_mapping: NotRequired[PartyMapping]  # 도표 A/B를 바꿀 때만
    reason: str


SIGNAL_VALUES = (
    "green", "yellow", "red", "green_left_arrow", "flashing_yellow", "flashing_red", "no_signal",
)
LANE_MARKING_VALUES = (
    "left_only", "straight_left", "straight_only", "straight_right", "right_only", "none",
)

# v1 칸별 허용값(fact-card-v1.md 3절). 모든 칸은 null(모름)도 된다.
# night·unprotected_left_sign은 참거짓, collision_time_ms는 0 이상 정수, narrative는 글이라 목록이
# 없다. collision_signal_*·lane_marking_*·unprotected_left_sign은 담당자 입력 칸(MANUAL_FIELDS)이다.
FACT_FIELD_OPTIONS: dict[str, tuple[str, ...]] = {
    "accident_type": ("car_to_car", "other"),
    "location_type": (
        "signalized_intersection", "unsignalized_intersection", "roundabout",
        "straight_road", "highway", "other",
    ),
    "road_priority": ("same_width", "major_A", "major_B", "stop_A", "stop_B", "other", "none"),
    "signal_A": SIGNAL_VALUES,
    "signal_B": SIGNAL_VALUES,
    "collision_signal_A": SIGNAL_VALUES,
    "collision_signal_B": SIGNAL_VALUES,
    "movement_A": ("straight", "left_turn", "right_turn", "u_turn", "lane_change", "other"),
    "movement_B": ("straight", "left_turn", "right_turn", "u_turn", "lane_change", "other"),
    "lane_marking_A": LANE_MARKING_VALUES,
    "lane_marking_B": LANE_MARKING_VALUES,
    "relative_position": (
        "same_direction_front", "same_direction_rear", "opposite", "left", "right",
    ),
    "entry_order": ("first", "second", "simultaneous", "none"),
}
BOOL_FIELDS = ("night", "unprotected_left_sign")
FACT_FIELDS: tuple[str, ...] = (
    *FACT_FIELD_OPTIONS, *BOOL_FIELDS, "collision_time_ms", "narrative",
)
# 비전이 채우지 않는 칸(2026-11-13). 영상 카드에서는 null + unverified로 나가고 확정을 막지 않는다.
# 도표를 가르는 데 필요할 때만 판단이 빈 칸 질문으로 묻는다.
MANUAL_FIELDS = (
    "collision_signal_A", "collision_signal_B", "lane_marking_A", "lane_marking_B",
    "unprotected_left_sign",
)
