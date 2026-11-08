"""AI Hub 코드표 행 → 카드 칸 매핑. 행 문구는 설명서 코드표 양식이며 비공개 데이터는 쓰지 않는다."""

import pytest

from evals.vision import aihub
from evals.vision.dataset import build_example


def row(place, feature, a, b, rate_a=50, rate_b=50):
    return {"place": place, "feature": feature, "a": a, "b": b, "rateA": rate_a, "rateB": rate_b}


def label(**overrides):
    base = {
        "video_name": "bb_1_000000_vehicle_1_1", "filming_way": "bb", "point_of_view": 1,
        "accident_object": 0, "accident_place": 1, "accident_place_feature": 10,
        "vehicle_a_progress_info": 16, "vehicle_b_progress_info": 18,
        "accident_type": 21, "rate_a": 40, "rate_b": 60,
    }
    return {**base, **overrides}


@pytest.mark.parametrize("place, expected", [
    ("사거리교차로(신 호등 없음)", "unsignalized_intersection"),
    ("사거리교차로(신호등 있음)", "signalized_intersection"),
    ("사거리 교차로 (신호등 있음)", "signalized_intersection"),
    ("T자형 교차로", "unsignalized_intersection"),
    ("회전교차로", "roundabout"),
    ("직선 도로", "other"),
    ("주차장(또는 차도가 아닌 장소)", "other"),
])
def test_location_type(place, expected):
    assert aihub.location_type(place) == expected


@pytest.mark.parametrize("feature, expected", [
    ("동일폭 도로", "same_width"),
    ("대로와 소로", "major_minor"),
    ("일시정지 표시가 한쪽방향에만 있음", "stop_sign"),
    ("교차로 내 진로변경", None),
    ("상대 차량이 측면 방향에서 진입", None),
])
def test_road_priority(feature, expected):
    assert aihub.road_priority(feature) == expected


@pytest.mark.parametrize("progress, expected", [
    ("오른쪽에서 직진(선진입)", "straight"),
    ("(마주보며) [녹색신호] 좌회전(비보호좌회전)", "left_turn"),
    ("[녹색 좌회전 신호] 직진 (좌회전 노면표시차로)", "straight"),
    ("추월 우회전(직진 노면표시차로)", "right_turn"),
    ("상시유턴구역에서 유턴", "u_turn"),
    ("소로에서 직진《좌측도로》", "straight"),
    ("[녹색신호], 직진, [적색신호] 충돌", "straight"),
    ("회전교차로 진입", None),
    ("중앙선 침범 추월", None),
    ("정차 후 출발", None),
])
def test_movement(progress, expected):
    assert aihub.movement(progress) == expected


@pytest.mark.parametrize("progress, location, expected", [
    ("[녹색신호] 직진", "signalized_intersection", "green"),
    ("[황색신호], 직진, [적색신호] 충돌", "signalized_intersection", "yellow"),
    ("[적색신호] 좌회전", "signalized_intersection", "red"),
    ("[녹색 좌회전 신호] 좌회전", "signalized_intersection", "green_left_arrow"),
    ("[녹색 직진·좌회전 신호] 후행 직진", "signalized_intersection", "green_left_arrow"),
    ("[무신호] 우회전", "signalized_intersection", "no_signal"),
    ("우회전", "signalized_intersection", None),
    ("오른쪽에서 직진", "unsignalized_intersection", "no_signal"),
])
def test_signal(progress, location, expected):
    assert aihub.signal(progress, location) == expected


@pytest.mark.parametrize("a, b, expected", [
    ("오른쪽에서 직진(선진입)", "왼쪽에서 직진(후진입)", "first"),
    ("대로에서 직진(후진입)", "소로에서 직진(선진입)", "second"),
    ("오른쪽에서 직진", "왼쪽에서 직진", None),
])
def test_entry_order(a, b, expected):
    assert aihub.entry_order(a, b) == expected


def test_card_fields_unsignalized_same_width():
    fields = aihub.card_fields(row("사거리교차로(신호등 없음)", "동일폭 도로",
                                   "오른쪽에서 직진(선진입)", "왼쪽에서 직진(후진입)"))
    assert fields == {
        "accident_type": "car_to_car", "location_type": "unsignalized_intersection",
        "road_priority": "same_width", "movement_A": "straight", "movement_B": "straight",
        "signal_A": "no_signal", "signal_B": "no_signal", "entry_order": "first", "night": None,
    }


def test_card_fields_signalized_signal_violation():
    fields = aihub.card_fields(row("사거리교차로(신호등 있음)", "상대 차량이 측면 방향에서 진입",
                                   "[녹색신호] 직진", "[적색신호] 직진"))
    assert fields["location_type"] == "signalized_intersection"
    assert fields["road_priority"] is None
    assert (fields["signal_A"], fields["signal_B"]) == ("green", "red")
    assert fields["entry_order"] is None


def test_build_example_in_scope_and_party_order_unknown():
    ex = build_example(label(), row("사거리교차로(신호등 없음)", "동일폭 도로",
                                    "오른쪽에서 직진", "왼쪽에서 직진", 40, 60), None)
    assert ex["party_mapping"]["video_ego_party_id"] is None
    assert ex["party_order_known"] is False  # 라벨 A/B는 도표 기준이라 촬영 차량과 무관
    assert ex["in_mvp_scope"] is True
    assert ex["aihub"]["rate_a"] == 40


def test_build_example_third_person_and_out_of_scope():
    ex = build_example(label(point_of_view=3, filming_way="cc"),
                       row("직선 도로", "추돌 사고", "후행 추돌", "선행 직진", 100, 0), None)
    assert ex["party_order_known"] is False
    assert ex["in_mvp_scope"] is False


def test_build_example_without_codebook_row_is_unscored():
    ex = build_example(label(accident_type=None), None, None)
    assert ex["fields"] == {}
    assert ex["in_mvp_scope"] is False


def test_load_label_accepts_key_variants(tmp_path):
    import json

    path = tmp_path / "x.json"
    path.write_text(json.dumps({"video": {
        "video_name": "bb_1_1_vehicle_1_1", "filming_way": "bb", "video_point_of_view": 1,
        "accident_object": 0, "accident_place": 1, "accident_place_feature": 10,
        "vehicle_a_progress_info": 16, "vehicle_b_progress_info": 18,
        "accident_type": 21, "accident_negligence_rate": 40,
    }}), encoding="utf-8-sig")
    loaded = aihub.load_label(path)
    assert loaded["accident_type"] == 21
    assert loaded["rate_a"] is None
