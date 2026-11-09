"""AI Hub 교통사고 영상 데이터(597) 라벨을 사실 카드 칸(chart_scope enum)으로 바꾼다.

라벨 JSON은 숫자 코드만 담고, 코드의 뜻은 설명서의 사고 유형 코드표에 있다
(scripts/aihub_codebook.py).
사고 유형 코드 하나가 장소·장소 특징·A/B 진행·과실비율을 모두 결정하므로 그 행에서 칸 값을 유도한다.
A/B는 코드표(=과실비율 인정기준 도표)의 A/B다.
설명서의 "1인칭은 본인=B" 규칙은 실제 라벨과 맞지 않았다.
"""

import json
import re
from pathlib import Path
from typing import TypedDict

from app.schemas.facts import PartyMapping


class CodebookRow(TypedDict):
    place: str
    feature: str
    a: str
    b: str
    rateA: int
    rateB: int


class AihubLabel(TypedDict):
    video_name: str
    filming_way: str
    point_of_view: int
    accident_object: int
    accident_place: int
    accident_place_feature: int
    vehicle_a_progress_info: int
    vehicle_b_progress_info: int
    accident_type: int | None
    rate_a: int | None
    rate_b: int | None


_BRACKET = re.compile(r"\[[^\]]*\]|\([^)]*\)|《[^》]*》")
_SIGNAL_TOKEN = re.compile(r"\[([^\]]*)\]")


def load_codebook(path: Path) -> dict[int, CodebookRow]:
    data = json.loads(path.read_text())
    return {int(code): row for code, row in data["accident_type"].items()}


def load_label(path: Path) -> AihubLabel:
    """라벨 파일의 키 변형(traffic_accident_type/accident_type, rateA/rate)을 흡수한다."""
    video = json.loads(path.read_text(encoding="utf-8-sig"))["video"]
    accident_type = video.get("traffic_accident_type", video.get("accident_type"))
    rate_a = video.get("accident_negligence_rateA")
    rate_b = video.get("accident_negligence_rateB")
    return {
        "video_name": video["video_name"],
        "filming_way": video["filming_way"],
        "point_of_view": int(video["video_point_of_view"]),
        "accident_object": int(video["accident_object"]),
        "accident_place": int(video["accident_place"]),
        "accident_place_feature": int(video["accident_place_feature"]),
        "vehicle_a_progress_info": int(video["vehicle_a_progress_info"]),
        "vehicle_b_progress_info": int(video["vehicle_b_progress_info"]),
        "accident_type": None if accident_type is None else int(accident_type),
        "rate_a": None if rate_a is None else int(rate_a),
        "rate_b": None if rate_b is None else int(rate_b),
    }


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def location_type(place: str) -> str:
    """코드표 장소 → location_type. T자형은 신호 없는 교차로 도표군이므로 unsignalized로 둔다."""
    p = _compact(place)
    if p.startswith("회전교차로"):
        return "roundabout"
    if "교차로" in p and "신호등있음" in p:
        return "signalized_intersection"
    if "교차로" in p and ("신호등없음" in p or p.startswith("T자형")):
        return "unsignalized_intersection"
    return "other"


def road_priority(feature: str) -> str | None:
    f = _compact(feature)
    if f.startswith("동일폭"):
        return "same_width"
    if f.startswith("대로와소로"):
        return "major_minor"
    if f.startswith("일시정지"):
        return "stop_sign"
    return None


def movement(progress: str) -> str | None:
    """진행 설명에서 괄호·신호 표기를 걷어낸 뒤 동작어로 판정한다. 추월·회전·추돌 등은 None."""
    core = _BRACKET.sub(" ", progress)
    for token, value in (
        ("유턴", "u_turn"), ("좌회전", "left_turn"), ("우회전", "right_turn"), ("직진", "straight"),
    ):
        if token in core:
            return value
    return None


def signal(progress: str, location: str) -> str | None:
    """진입 시점 신호 = 첫 [..] 표기.

    신호 없는 교차로는 no_signal, 신호 교차로인데 표기가 없으면 None.
    """
    tokens = _SIGNAL_TOKEN.findall(progress)
    if tokens:
        t = _compact(tokens[0])
        if "무신호" in t:
            return "no_signal"
        if "좌회전" in t and "녹색" in t:
            return "green_left_arrow"
        if "녹색" in t:
            return "green"
        if "황색" in t:
            return "yellow"
        if "적색" in t:
            return "red"
        return None
    if location in ("unsignalized_intersection", "roundabout"):
        return "no_signal"
    return None


def entry_order(progress_a: str, progress_b: str) -> str | None:
    """A 기준 진입 순서. 코드표에 선/후진입 표기가 없으면 구분하지 않은 유형이므로 None."""
    if "선진입" in progress_a or "후진입" in progress_b:
        return "first"
    if "후진입" in progress_a or "선진입" in progress_b:
        return "second"
    return None


def card_fields(row: CodebookRow) -> dict[str, str | None]:
    location = location_type(row["place"])
    return {
        "accident_type": "car_to_car",
        "location_type": location,
        "road_priority": road_priority(row["feature"]),
        "movement_A": movement(row["a"]),
        "movement_B": movement(row["b"]),
        "signal_A": signal(row["a"], location),
        "signal_B": signal(row["b"], location),
        "entry_order": entry_order(row["a"], row["b"]),
        "night": None,  # AI Hub 라벨에 주야 칸이 없다. 별도 라벨링 전까지 채점 제외.
    }


def party_mapping(label: AihubLabel) -> PartyMapping:
    """라벨의 A/B는 도표 A/B다. 촬영 차량이 A인지 B인지 라벨로는 알 수 없어 ego는 None."""
    return {
        "video_ego_party_id": None,
        "insured_party_id": None,
        "chart_party_A_id": "A",
        "chart_party_B_id": "B",
    }
