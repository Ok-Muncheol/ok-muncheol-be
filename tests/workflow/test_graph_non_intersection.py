"""판단 그래프: 교차로 밖 장소(일반 도로·고속도로·주차장·회전교차로, 11/16)와 그 도표 칸.

합성 도표만 쓴다(실제 도표 번호·비율 없음). DB·모델 없이 돈다.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.schemas.chart_scope import (
    ANY_VALUE,
    FIELD_OPTIONS,
    IN_SCOPE_LOCATION_TYPES,
    REASON_NO_REVIEWED_CHART,
    REASON_SWAP_HINT,
    REQUIRED_CONFIRMED_FIELDS,
    LocationType,
)
from app.workflow.graph import (
    FIELD_LABELS,
    Dependencies,
    build_graph,
    choose_orientation,
    initial_state,
    thread_config,
)
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


# ── 도표 A/B 맞바꿈 대조(swap_hints, 11/16.6) ──────────────────────────────
# 상대 차(카드 A)가 앞에서 진로변경, 블랙박스 차(카드 B)가 뒤에서 직진. 도표(ROAD)는 A가 후행
# 직진이라 기본 매핑(도표 A = 상대 차)으로는 두 차의 진행 방향이 거꾸로 대조된다
CUT_IN = {"movement_A": field("lane_change"), "movement_B": field("straight")}


def test_no_chart_suggests_swapping_chart_parties():
    """고를 도표가 없으면 도표 A/B를 맞바꿔 대조한 결과를 보류 사유 끝에 붙인다. 판정은 그대로다."""
    c = road_card(**CUT_IN, relative_position=field("same_direction_front"))
    out, _ = start(c, [candidate("R-1", ROAD, 30)], thread="run-swap-hint")
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("deferred", None)
    assert result["reasons"] == [
        REASON_NO_REVIEWED_CHART,
        "R-1: 카드와 불일치 ['movement_A', 'movement_B', 'relative_position']",
        f"{REASON_SWAP_HINT} 남는 도표: R-1 「synthetic R-1」",
    ]
    # 담당자가 맞바꾸면(도표 A = 블랙박스 차) 그 도표를 고른다
    swapped = en_v1_card(swapped=True, location_type=field("straight_road"), **CUT_IN,
                         relative_position=field("same_direction_front"), **NO_SIGNAL)
    out, _ = start(swapped, [candidate("R-1", ROAD, 30)], thread="run-swap-hint-done")
    assert (out["result"]["status"], out["result"]["chart_id"]) == ("draft_ready", "R-1")


def test_swap_hint_names_the_nearest_chart_and_what_still_differs():
    """상대 위치를 옆 도로(right)로 적고 도표 A도 거꾸로 둔 카드(운영 보류 사례 모양).

    맞바꾸면 상대 위치를 따지지 않는 합류 도표(M-1)가 남고, 진로변경 도표(R-1)는 상대 위치만 다르다.
    맞바꿔도 장소가 다른 도표(H-1)는 적지 않는다.
    """
    merge = candidate("M-1", {**ROAD, "relative_position": ANY_VALUE, "lane_reduction": "true"},
                      40, score=0.5)
    highway = candidate("H-1", {**ROAD, "location_type": "highway"}, 30, score=0.7)
    c = road_card(**CUT_IN, relative_position=field("right"))
    out, _ = start(c, [candidate("R-1", ROAD, 30, score=0.9), merge, highway],
                   thread="run-swap-near")
    assert out["result"]["status"] == "deferred"
    assert [r for r in out["result"]["reasons"] if r.startswith(REASON_SWAP_HINT)] == [
        f"{REASON_SWAP_HINT} 남는 도표: M-1 「synthetic M-1」",
        f"{REASON_SWAP_HINT} R-1 「synthetic R-1」: 카드와 불일치 ['relative_position']",
    ]


def test_swap_hint_writes_side_fields_in_the_current_chart_terms():
    """맞바꿔 대조한 칸도 지금 방향의 도표 칸 이름으로 적는다(FE가 매핑으로 카드 칸 이름을 붙인다).

    도표 A가 이륜차인 도표는 맞바꾸면 도표 A가 블랙박스 차(카드 B)라 표시 없는 '이륜차 아님'과
    어긋난다. 지금 방향에서 블랙박스 차는 도표 B라 two_wheeler_B로 적는다.
    """
    bike = candidate("B-1", {**ROAD, "two_wheeler_A": "true"}, 30)
    c = road_card(**CUT_IN, relative_position=field("same_direction_front"))
    out, _ = start(c, [bike], thread="run-swap-side")
    assert out["result"]["reasons"][-1] == (
        f"{REASON_SWAP_HINT} B-1 「synthetic B-1」: 카드와 불일치 ['two_wheeler_B']")


def test_swap_hint_is_left_out_when_swapping_does_not_help():
    # 장소가 다른 도표만 있으면 맞바꿔도 그대로라 붙이지 않는다
    highway = candidate("H-1", {**ROAD, "location_type": "highway"}, 30)
    out, _ = start(road_card(**CUT_IN, relative_position=field("same_direction_front")),
                   [highway], thread="run-swap-none")
    assert out["result"]["reasons"] == [
        REASON_NO_REVIEWED_CHART,
        "H-1: 카드와 불일치 ['location_type', 'movement_A', 'movement_B', 'relative_position']",
    ]


# ── 도표 A/B를 판단이 정함(choose_orientation, 11/16.7) ─────────────────────


def orient(card_, cands):
    retrieve = AsyncMock(return_value=cands)
    return asyncio.run(choose_orientation(card_, retrieve)), retrieve


def test_orientation_flips_when_only_the_other_side_has_a_chart():
    c = road_card(**CUT_IN, relative_position=field("same_direction_front"))
    chosen, retrieve = orient(c, [candidate("R-1", ROAD, 30)])
    # 도표 A = 블랙박스 차(P1)로 맞바꾼 사본. 원본 카드와 칸 값은 그대로다
    assert chosen["party_mapping"]["chart_party_A_id"] == "P1"
    assert chosen["party_mapping"]["chart_party_B_id"] == "P2"
    assert c["party_mapping"]["chart_party_A_id"] == "P2" and chosen["fields"] == c["fields"]
    assert retrieve.await_count == 2  # 두 방향 모두 검색한다
    out, _ = start({**chosen, "status": "confirmed"}, [candidate("R-1", ROAD, 30)],
                   thread="run-oriented")
    assert (out["result"]["status"], out["result"]["chart_id"]) == ("draft_ready", "R-1")


def test_orientation_stays_when_the_current_side_fits_or_ties():
    cands = [candidate("R-1", ROAD, 30)]
    fits = road_card()
    assert orient(fits, cands)[0] is fits
    # 어느 쪽도 못 고르고 가까운 정도가 같으면 그대로 둔다
    tie = road_card(relative_position=field("opposite"))
    assert orient(tie, [candidate("X-1", {**ROAD, "movement_A": "u_turn",
                                           "movement_B": "u_turn"}, 50)])[0] is tie


def test_orientation_prefers_the_nearer_side_when_nothing_is_selectable():
    # 운영 보류 사례 모양(상대 위치 right): 맞바꾸면 R-1이 한 칸만 다르다 → 맞바꾼다
    c = road_card(**CUT_IN, relative_position=field("right"))
    chosen, _ = orient(c, [candidate("R-1", ROAD, 30)])
    assert chosen["party_mapping"]["chart_party_A_id"] == "P1"


def test_orientation_skips_search_for_cards_it_cannot_flip_or_out_of_scope():
    from tests.workflow.synthetic import card as legacy_card

    legacy = legacy_card()
    chosen, retrieve = orient(legacy, [])
    assert chosen is legacy and retrieve.await_count == 0
    out_of_scope = en_v1_card(location_type=field("other"))
    chosen, retrieve = orient(out_of_scope, [])
    assert chosen is out_of_scope and retrieve.await_count == 0
