"""판단 그래프: 교차로 밖 장소(일반 도로·고속도로·주차장·회전교차로, 11/16)와 그 도표 칸.

합성 도표만 쓴다(실제 도표 번호·비율 없음). DB·모델 없이 돈다.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.schemas.chart_scope import (
    FIELD_OPTIONS,
    IN_SCOPE_LOCATION_TYPES,
    REASON_NO_REVIEWED_CHART,
    REQUIRED_CONFIRMED_FIELDS,
    LocationType,
)
from app.workflow.graph import FIELD_LABELS, Dependencies, build_graph, initial_state, thread_config
from tests.workflow.synthetic import candidate, en_v1_card, field, verdict

# 일반 도로, A 후행 직진 대 B 선행 진로변경(도표 A 기준)
ROAD = {"location_type": "straight_road", "movement_A": "straight", "movement_B": "lane_change",
        "relative_position": "same_direction_rear"}
NO_SIGNAL = {"signal_A": field("no_signal"), "signal_B": field("no_signal")}


def start(card_, cands, thread="run-road"):
    reviewer = AsyncMock(return_value=verdict())
    retrieve = AsyncMock(return_value=cands)
    graph = build_graph(Dependencies(retrieve=retrieve, reviewer=reviewer))
    out = asyncio.run(graph.ainvoke(initial_state(card_, "criteria-1", "rule-1", 1),
                                    thread_config(thread)))
    return out, retrieve


def road_card(**fields):
    base = {"location_type": field("straight_road"), "movement_A": field("straight"),
            "movement_B": field("lane_change"), "relative_position": field("same_direction_rear"),
            **NO_SIGNAL}
    return en_v1_card(**{**base, **fields})


def questions(out):
    return [q["field_key"] for q in out["__interrupt__"][0].value["questions"]]


def test_every_place_but_other_is_in_scope_with_required_fields():
    assert set(IN_SCOPE_LOCATION_TYPES) == set(LocationType.__args__) - {"other"}
    for place in IN_SCOPE_LOCATION_TYPES:
        assert {"accident_type", "location_type"} <= set(REQUIRED_CONFIRMED_FIELDS[place])
    # 회전교차로는 진행 방향 대신 회전 차로 수·회전 진행으로 가른다
    assert "movement_A" not in REQUIRED_CONFIRMED_FIELDS["roundabout"]
    # 질문 문구가 없는 도표 선택 칸이 없다
    assert set(FIELD_OPTIONS) <= set(FIELD_LABELS)


@pytest.mark.parametrize("place", ["straight_road", "highway", "parking_lot", "roundabout"])
def test_new_places_pass_the_scope_gate(place):
    out, retrieve = start(en_v1_card(location_type=field(place)), cands=[])
    # 범위에서 보류하지 않고 검색까지 간다(검수 도표가 없어 근거 없음으로 보류)
    assert retrieve.await_count == 1
    assert out["result"]["reasons"][0] == REASON_NO_REVIEWED_CHART


def test_road_chart_is_selected():
    out, _ = start(road_card(), [candidate("R-1", ROAD, 30),
                                 candidate("R-2", {**ROAD, "relative_position": "opposite"}, 50)])
    result = out["result"]
    assert (result["status"], result["chart_id"], result["fault_A"]) == ("draft_ready", "R-1", 30)


def test_road_card_asks_relative_position_when_unconfirmed():
    c = road_card(relative_position=field(None, confirmed=False))
    out, _ = start(c, [candidate("R-1", ROAD, 30),
                       candidate("R-2", {**ROAD, "relative_position": "opposite"}, 50)])
    assert questions(out) == ["relative_position"]


def test_chart_for_two_places_matches_both_and_rules_out_intersections():
    places = [{"variant": None, "base_A": 30, "base_B": 70, "conditions": {"location_type": p}}
              for p in ("straight_road", "highway")]
    shared = {k: v for k, v in ROAD.items() if k != "location_type"}
    chart = candidate("R-3", shared, 30, base_variants=places)
    for place in ("straight_road", "highway"):
        out, _ = start(road_card(location_type=field(place)), [chart], thread=f"run-{place}")
        result = out["result"]
        assert (result["status"], result["chart_id"]) == ("draft_ready", "R-3"), place
    # 교차로 카드에서는 "미정의"가 아니라 장소 불일치로 빠진다
    c = road_card(location_type=field("signalized_intersection"))
    out, _ = start(c, [chart], thread="run-intersection")
    assert out["result"]["reasons"] == [REASON_NO_REVIEWED_CHART,
                                        "R-3: 카드와 불일치 ['location_type']"]


def test_swapped_card_reads_new_side_fields_from_the_other_car():
    # 도표 A = 후행 이륜차 직진, 도표 B = 선행 정차 중 문 열림. 촬영 차량(카드 B)이 도표 A다
    two_wheeler = {"location_type": "straight_road", "movement_A": "straight",
                   "movement_B": "stopped", "relative_position": "same_direction_rear",
                   "two_wheeler_A": "true", "door_open_B": "true"}
    car = {**two_wheeler, "two_wheeler_A": "false"}
    c = en_v1_card(swapped=True, location_type=field("straight_road"),
                   movement_A=field("stopped"), movement_B=field("straight"),
                   relative_position=field("same_direction_front"),
                   two_wheeler_B=field(True), door_open_A=field(True), **NO_SIGNAL)
    out, _ = start(c, [candidate("T-1", two_wheeler, 30), candidate("C-1", car, 20)])
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "T-1")
    excluded = [a["reason"] for a in result["alternatives"] if a["chart_id"] == "C-1"]
    assert excluded == ["제외 — 카드와 불일치 ['two_wheeler_A']"]


def test_unfilled_manual_field_is_asked_only_while_a_chart_needs_it():
    # 담당자 입력 칸(null + unverified)은 그 칸으로 갈리는 도표가 남을 때만 묻는다
    unfilled = {"value": None, "model_value": None, "source": "manual", "model_confidence": None,
                "verification_status": "unverified", "evidence_frame_ids": [],
                "source_document_ids": []}
    jam = candidate("J-1", {**ROAD, "traffic_jam": "true"}, 100)
    plain = candidate("J-2", {**ROAD, "traffic_jam": "false"}, 30)
    out, _ = start(road_card(traffic_jam=unfilled), [jam, plain])
    question = out["__interrupt__"][0].value["questions"][0]
    assert (question["field_key"], question["options"]) == ("traffic_jam", ["true", "false"])
    assert question["text"] == "정체 여부을(를) 확인해 주세요."
    # 짝 도표가 false를 적었으면 그 도표 하나만 남아도 묻는다(카드 값이 없어 고를 수 없다)
    out, _ = start(road_card(traffic_jam=unfilled), [plain], thread="run-one-chart")
    assert questions(out) == ["traffic_jam"]
    # 그 칸을 적지 않은 도표만 남으면 묻지 않는다
    out, _ = start(road_card(traffic_jam=unfilled), [candidate("R-1", ROAD, 30)],
                   thread="run-no-jam-chart")
    assert (out["result"]["status"], out["result"]["chart_id"]) == ("draft_ready", "R-1")


def test_roundabout_asks_lanes_and_moves_not_movement():
    c = en_v1_card(location_type=field("roundabout"), **NO_SIGNAL)
    one = candidate("RB-1", {"location_type": "roundabout", "roundabout_lanes": "one",
                             "roundabout_move_A": "circulating",
                             "roundabout_move_B": "entering"}, 20)
    two = candidate("RB-2", {"location_type": "roundabout", "roundabout_lanes": "two_plus",
                             "roundabout_move_A": "circulating", "roundabout_move_B": "entering",
                             "roundabout_lane_A": "inner", "roundabout_lane_B": "inner"}, 10)
    out, _ = start(c, [one, two])
    assert questions(out) == ["roundabout_lanes", "roundabout_move_A", "roundabout_move_B",
                              "roundabout_lane_A", "roundabout_lane_B"]
    answered = en_v1_card(location_type=field("roundabout"), roundabout_lanes=field("one"),
                          roundabout_move_A=field("circulating"),
                          roundabout_move_B=field("entering"), **NO_SIGNAL)
    out, _ = start(answered, [one, two], thread="run-roundabout-answered")
    result = out["result"]
    assert (result["status"], result["chart_id"], result["fault_A"]) == ("draft_ready", "RB-1", 20)


def test_special_ego_chart_drops_out_unless_marked_and_the_reason_says_so():
    """촬영 차 긴급자동차를 표시하지 않으면 '아니오'로 봐서 특수 도표(차55-x 모양)가 빠진다."""
    # 표시하면(true) 맞는 칸이 하나 더 많은 특수 도표가 1순위다(11/16 C 검수 기준)
    signal = {"location_type": "signalized_intersection", "movement_A": "straight",
              "movement_B": "straight", "signal_A": "green", "signal_B": "red"}
    general = candidate("G-1", signal, 0)
    special = candidate("E-1", {**signal, "emergency_vehicle_B": "true"}, 60)
    c = en_v1_card(movement_B=field("straight"))
    out, _ = start(c, [general, special], thread="run-ego-unmarked")
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "G-1")
    assert any("emergency_vehicle_B: 담당자 표시 없음" in r for r in result["reasons"])
    marked = en_v1_card(movement_B=field("straight"), emergency_vehicle_B=field(True))
    out, _ = start(marked, [general, special], thread="run-ego-marked")
    assert (out["result"]["status"], out["result"]["chart_id"]) == ("draft_ready", "E-1")
    assert not any("담당자 표시 없음" in r for r in out["result"]["reasons"])
    # 후보가 그 칸을 가르지 않으면 이유에 적지 않는다
    out, _ = start(c, [general], thread="run-ego-irrelevant")
    assert not any("담당자 표시 없음" in r for r in out["result"]["reasons"])


def test_lane_change_timing_splits_both_lane_change_charts_and_leader_first_defers():
    """두 차 모두 진로변경이면 순서를 묻는다. 동시는 43-3 모양, 뒤차 먼저는 43-5 모양을 고른다.

    앞차 먼저(leader_first)는 43-5 원문이 적용하지 않는 경우라 둘 다 빠져 보류된다(C 요청).
    """
    both = {"location_type": "straight_road", "movement_A": "lane_change",
            "movement_B": "lane_change"}
    sides = [{"variant": None, "base_A": 50, "base_B": 50,
              "conditions": {"relative_position": p, "lane_change_timing": "simultaneous"}}
             for p in ("same_direction_front", "same_direction_rear")]
    simultaneous = candidate("L-3", both, 50, base_variants=sides)
    follower = candidate("L-5", {**both, "relative_position": "same_direction_rear",
                                 "lane_change_timing": "follower_first"}, 40)
    moves = {"movement_A": field("lane_change"), "movement_B": field("lane_change")}
    out, _ = start(road_card(**moves), [simultaneous, follower], thread="run-lane-ask")
    [question] = out["__interrupt__"][0].value["questions"]
    assert question["field_key"] == "lane_change_timing"
    assert question["options"] == ["simultaneous", "follower_first", "leader_first"]
    for answer, chart_id in (("simultaneous", "L-3"), ("follower_first", "L-5")):
        c = road_card(**moves, lane_change_timing=field(answer))
        out, _ = start(c, [simultaneous, follower], thread=f"run-lane-{answer}")
        assert (out["result"]["status"], out["result"]["chart_id"]) == ("draft_ready", chart_id)
    c = road_card(**moves, lane_change_timing=field("leader_first"))
    out, _ = start(c, [simultaneous, follower], thread="run-lane-leader")
    assert out["result"]["status"] == "deferred"
