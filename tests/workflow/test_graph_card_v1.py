"""판단 그래프: 카드 v1(한국어 값)도 이전 형식과 같은 판정·질문·보류를 낸다. DB·모델 없이 돈다."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from langgraph.types import Command

from app.schemas.chart_scope import (
    ANY_VALUE,
    REASON_FIELD_NOT_CONFIRMED,
    REASON_NO_REVIEWED_CHART,
    REASON_OUT_OF_SCOPE,
    REASON_PENDING_DECISION,
)
from app.workflow.graph import (
    Dependencies,
    build_graph,
    initial_state,
    question_id,
    thread_config,
)
from tests.workflow.synthetic import (
    candidate,
    candidates,
    card,
    en_v1_card,
    field,
    v1_card,
    verdict,
)


def start(card_, cands=None, thread="run-v1"):
    reviewer = AsyncMock(return_value=verdict())
    retrieve = AsyncMock(return_value=candidates() if cands is None else cands)
    graph = build_graph(Dependencies(retrieve=retrieve, reviewer=reviewer))
    out = asyncio.run(graph.ainvoke(initial_state(card_, "criteria-1", "rule-1", 1),
                                    thread_config(thread)))
    return graph, out, retrieve, reviewer


def test_v1_card_reaches_the_same_draft_as_the_old_card():
    _, out, _, _ = start(v1_card())
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "S-1")
    assert (result["fault_A"], result["fault_B"]) == (20, 80)


def test_v1_questions_use_card_field_names_and_values():
    _, out, _, _ = start(v1_card(movement_B=field("좌회전", confirmed=False),
                                 accident_object=field("차대차", confirmed=False)))
    questions = {q["field_key"]: q for q in out["__interrupt__"][0].value["questions"]}
    assert set(questions) == {"accident_object", "movement_B"}
    # 답은 질문의 칸 이름·값 그대로 A의 카드 수정으로 가므로 카드 형식과 같아야 한다
    assert questions["accident_object"]["options"] == ["차대차", "차대이륜차", "기타"]
    assert "좌회전" in questions["movement_B"]["options"]
    assert "left_turn" not in questions["movement_B"]["options"]
    assert "불명" not in questions["movement_B"]["options"]
    # question_id는 도표 선택 칸으로 만든다
    assert questions["accident_object"]["question_id"] == question_id("run-v1", "accident_type")


def test_unknown_answer_to_a_v1_question_counts_the_chart_field():
    c = v1_card(accident_object=field("차대차", confirmed=False))
    graph, out, _, reviewer = start(c, thread="run-v1-unknown")
    question = out["__interrupt__"][0].value["questions"][0]
    assert question["field_key"] == "accident_object"
    answer = {"question_id": question["question_id"], "expected_card_version": 3,
              "value": None, "unknown": True}
    out = asyncio.run(graph.ainvoke(Command(resume=[answer]), thread_config("run-v1-unknown")))
    result = out["result"]
    assert result["status"] == "deferred"
    assert result["reasons"] == [REASON_FIELD_NOT_CONFIRMED.format(field_key="accident_type")]
    assert reviewer.await_count == 0


def test_confirmed_unknown_is_not_asked_again():
    _, out, retrieve, reviewer = start(v1_card(movement_B=field("불명")))
    assert "__interrupt__" not in out
    result = out["result"]
    assert result["status"] == "deferred"
    assert result["reasons"] == [REASON_FIELD_NOT_CONFIRMED.format(field_key="movement_B")]
    assert (retrieve.await_count, reviewer.await_count) == (1, 0)


def test_confirmed_unknown_still_allows_charts_that_ignore_the_field():
    cands = [candidate("S-9", {"location_type": "signalized_intersection",
                               "movement_A": "straight", "movement_B": ANY_VALUE,
                               "signal_A": "green", "signal_B": "red"}, 10)]
    _, out, _, _ = start(v1_card(movement_B=field("불명")), cands=cands)
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "S-9")


@pytest.mark.parametrize(("fields", "reason"), [
    ({"location_type": field("불명")},
     REASON_FIELD_NOT_CONFIRMED.format(field_key="location_type")),
    ({"accident_object": field("불명"), "location_type": field("불명")},
     REASON_FIELD_NOT_CONFIRMED.format(field_key="accident_type, location_type")),
    ({"accident_object": field("차대이륜차")}, REASON_OUT_OF_SCOPE),
    ({"accident_object": field("차대이륜차"), "location_type": field("불명")}, REASON_OUT_OF_SCOPE),
    ({"party_count": field(3)}, REASON_OUT_OF_SCOPE),
    ({"location_type": field("직선도로")}, REASON_OUT_OF_SCOPE),
    ({"location_type": field("회전교차로")}, REASON_PENDING_DECISION),
])
def test_scope_is_decided_before_retrieval(fields, reason):
    _, out, retrieve, _ = start(v1_card(**fields))
    assert out["result"]["status"] == "deferred"
    assert out["result"]["reasons"] == [reason]
    assert retrieve.await_count == 0


def test_unconfirmed_party_count_does_not_defer():
    _, out, _, _ = start(v1_card(party_count=field(3, confirmed=False)))
    assert out["result"]["status"] == "draft_ready"


def test_side_specific_road_priority_never_matches_a_sideless_chart():
    unsignalized = {"location_type": "unsignalized_intersection", "road_priority": "major_minor",
                    "movement_A": "straight", "movement_B": "straight", "entry_order": ANY_VALUE}
    c = v1_card(location_type=field("사거리_신호없음"), road_priority=field("A대로"),
                signal_A=field("신호없음"), signal_B=field("신호없음"),
                movement_B=field("직진"), entry_order=field("해당없음"))
    _, out, _, reviewer = start(c, cands=[candidate("U-1", unsignalized, 30)])
    result = out["result"]
    assert result["status"] == "deferred"
    assert result["reasons"] == [REASON_NO_REVIEWED_CHART,
                                 "U-1: 카드와 불일치 ['road_priority']"]
    assert reviewer.await_count == 0


def test_not_applicable_entry_order_matches_only_charts_that_ignore_it():
    base = {"location_type": "unsignalized_intersection", "road_priority": "same_width",
            "movement_A": "straight", "movement_B": "straight"}
    c = v1_card(location_type=field("사거리_신호없음"), road_priority=field("동일폭"),
                signal_A=field("신호없음"), signal_B=field("신호없음"),
                movement_B=field("직진"), entry_order=field("해당없음"))
    cands = [candidate("U-2", {**base, "entry_order": "first"}, 40, score=0.9),
             candidate("U-3", {**base, "entry_order": ANY_VALUE}, 50, score=0.5)]
    _, out, _, _ = start(c, cands=cands)
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "U-3")


# ── 카드 v1 확정본(영어 12칸, #120) ─────────────────────────────────────


@pytest.mark.parametrize("swapped", [False, True])
def test_en_v1_card_reaches_the_same_draft_as_the_old_card(swapped):
    _, out, _, _ = start(en_v1_card(swapped=swapped), thread=f"run-en-{swapped}")
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "S-1")
    assert (result["fault_A"], result["fault_B"]) == (20, 80)


def test_en_v1_questions_use_chart_field_names_and_v1_values():
    c = en_v1_card(accident_type=field("car_to_car", confirmed=False),
                   movement_B=field("left_turn", confirmed=False))
    _, out, _, _ = start(c, thread="run-en-q")
    questions = {q["field_key"]: q for q in out["__interrupt__"][0].value["questions"]}
    # #119는 확정본을 한국어 안으로 봐서 accident_object·한국어 선택지로 물었다
    assert set(questions) == {"accident_type", "movement_B"}
    assert questions["accident_type"]["options"] == ["car_to_car", "other"]
    assert "lane_change" in questions["movement_B"]["options"]
    assert "좌회전" not in questions["movement_B"]["options"]
    assert questions["accident_type"]["question_id"] == question_id("run-en-q", "accident_type")


def test_swapped_en_v1_question_names_the_card_field():
    # 도표 A = 카드 B. 도표 B의 진행 방향(카드 A 칸)이 미확인이면 카드 A 칸을 묻는다
    c = en_v1_card(swapped=True, movement_A=field("left_turn", confirmed=False))
    graph, out, _, reviewer = start(c, thread="run-en-swap")
    (question,) = out["__interrupt__"][0].value["questions"]
    assert question["field_key"] == "movement_A"
    assert question["question_id"] == question_id("run-en-swap", "movement_B")
    # 문구도 담당자가 고칠 카드 칸(카드 A)으로 쓴다
    assert question["text"] == "A 차량의 진행 방향을(를) 확인해 주세요."
    answer = {"question_id": question["question_id"], "expected_card_version": 3,
              "value": None, "unknown": True}
    out = asyncio.run(graph.ainvoke(Command(resume=[answer]), thread_config("run-en-swap")))
    # '모름'은 도표 B 진행 방향으로 센다
    assert out["result"]["status"] == "deferred"
    assert out["result"]["reasons"] == [REASON_FIELD_NOT_CONFIRMED.format(field_key="movement_B")]
    assert reviewer.await_count == 0


@pytest.mark.parametrize(("fields", "reason"), [
    ({"accident_type": field(None)}, REASON_FIELD_NOT_CONFIRMED.format(field_key="accident_type")),
    ({"location_type": field(None)}, REASON_FIELD_NOT_CONFIRMED.format(field_key="location_type")),
    ({"accident_type": field("other")}, REASON_OUT_OF_SCOPE),
    ({"location_type": field("straight_road")}, REASON_OUT_OF_SCOPE),
    ({"location_type": field("highway")}, REASON_OUT_OF_SCOPE),
    ({"location_type": field("roundabout")}, REASON_PENDING_DECISION),
])
def test_en_v1_scope_is_decided_before_retrieval(fields, reason):
    # 확인한 null(모름)은 미확인처럼 지나가지 않고 보류된다
    _, out, retrieve, _ = start(en_v1_card(**fields))
    assert out["result"]["status"] == "deferred"
    assert out["result"]["reasons"] == [reason]
    assert retrieve.await_count == 0


def test_what_if_on_a_swapped_v1_card_assumes_the_chart_party():
    from app.api import judgments

    judgments.configure(deps=Dependencies(retrieve=AsyncMock(return_value=candidates()),
                                          reviewer=AsyncMock(return_value=verdict())))
    try:
        results = [asyncio.run(judgments._simulate(c, {"movement_B": "straight"}))[0]
                   for c in (card(), en_v1_card(), en_v1_card(swapped=True))]
    finally:
        judgments.configure(deps=Dependencies())
    # 가정 값은 도표 B의 진행 방향이다. 맞바꾼 카드에서는 카드 A 칸(상대 차량)에 들어가야 한다
    outcomes = {(r["status"], r["chart_id"], r["fault_A"]) for r in results}
    assert outcomes == {("completed", "S-2", 50)}


SIGNALIZED_V1 = {"location_type": "signalized_intersection", "movement_A": "straight",
                 "movement_B": "left_turn", "signal_A": "green", "signal_B": "red"}


def test_relative_position_separates_opposite_and_side_charts():
    """맞은편·측면만 다른 도표를 상대 위치로 가른다. 확인하지 않았으면 그 칸을 묻는다."""
    opposite = candidate("P-1", {**SIGNALIZED_V1, "relative_position": "opposite"}, 20,
                         score=0.9)
    # 측면 도표는 왼쪽·오른쪽 변형으로 나눠 적는다(값이 여럿이면 변형 분리)
    side = candidate("P-2", SIGNALIZED_V1, 30, score=0.8, base_variants=[
        {"variant": "가", "base_A": 30, "base_B": 70, "conditions": {"relative_position": "left"}},
        {"variant": "나", "base_A": 30, "base_B": 70, "conditions": {"relative_position": "right"}},
    ])
    _, out, _, _ = start(en_v1_card(relative_position=field("right")), cands=[opposite, side])
    assert (out["result"]["chart_id"], out["result"]["fault_A"]) == ("P-2", 30)
    _, out, _, _ = start(en_v1_card(), cands=[opposite, side], thread="run-opposite")
    assert (out["result"]["chart_id"], out["result"]["fault_A"]) == ("P-1", 20)
    unconfirmed = en_v1_card(relative_position=field("left", confirmed=False))
    _, out, _, _ = start(unconfirmed, cands=[opposite, side], thread="run-ask-position")
    (question,) = out["__interrupt__"][0].value["questions"]
    assert question["field_key"] == "relative_position"
    assert question["options"] == [
        "same_direction_front", "same_direction_rear", "opposite", "left", "right"]


def test_swapped_card_flips_relative_position_before_choosing():
    """같은 사고를 도표 A = 촬영 차량으로 적으면 카드의 좌우가 반대로 적힌다. 같은 도표를 고른다."""
    left = candidate("P-3", {**SIGNALIZED_V1, "relative_position": "left"}, 30, score=0.9)
    right = candidate("P-4", {**SIGNALIZED_V1, "relative_position": "right"}, 60, score=0.8)
    _, out, _, _ = start(en_v1_card(relative_position=field("left")), cands=[left, right])
    assert out["result"]["chart_id"] == "P-3"
    swapped = en_v1_card(swapped=True, relative_position=field("right"))
    _, out, _, _ = start(swapped, cands=[left, right], thread="run-swap-position")
    assert (out["result"]["chart_id"], out["result"]["fault_A"]) == ("P-3", 30)


def unsignalized_v1(swapped: bool = False, **fields):
    base = {"location_type": field("unsignalized_intersection"),
            "movement_A": field("straight"), "movement_B": field("straight"),
            "signal_A": field("no_signal"), "signal_B": field("no_signal")}
    return en_v1_card(swapped=swapped, **{**base, **fields})


def test_priority_side_separates_charts():
    """누가 일시정지 쪽인지만 다른 도표(차9-1/9-2 같은 쌍)를 우선 도로 쪽으로 가른다."""
    base = {"location_type": "unsignalized_intersection", "movement_A": "straight",
            "movement_B": "straight", "entry_order": ANY_VALUE}
    a_stop = candidate("Q-1", {**base, "road_priority": "stop_A"}, 70, score=0.9)
    b_stop = candidate("Q-2", {**base, "road_priority": "stop_B"}, 30, score=0.8)
    c = unsignalized_v1(road_priority=field("stop_B"), entry_order=field("first"))
    _, out, _, _ = start(c, cands=[a_stop, b_stop])
    assert (out["result"]["chart_id"], out["result"]["fault_A"]) == ("Q-2", 30)
    # 도표 A = 촬영 차량이면 카드의 stop_A가 도표의 stop_B다
    swapped = unsignalized_v1(swapped=True, road_priority=field("stop_A"),
                              entry_order=field("second"))
    _, out, _, _ = start(swapped, cands=[a_stop, b_stop], thread="run-swap-stop")
    assert (out["result"]["chart_id"], out["result"]["fault_A"]) == ("Q-2", 30)


def test_lane_marking_separates_charts_and_is_asked_when_unfilled():
    """차로 노면표시로 갈리는 도표(차11-x)는 그 칸을 묻고, 답이 있으면 맞는 도표를 고른다."""
    base = {"location_type": "signalized_intersection", "movement_A": "straight",
            "movement_B": "left_turn", "signal_A": "green", "signal_B": "red",
            "relative_position": "opposite"}
    in_left_lane = candidate("L-1", {**base, "lane_marking_A": "left_only",
                                     "lane_marking_B": "straight_left"}, 100, score=0.9)
    plain = candidate("L-2", {**base, "lane_marking_A": "straight_only",
                              "lane_marking_B": "straight_left"}, 20, score=0.8)
    asked = en_v1_card()
    _, out, _, _ = start(asked, cands=[in_left_lane, plain], thread="run-lane-ask")
    keys = [q["field_key"] for q in out["__interrupt__"][0].value["questions"]]
    assert keys == ["lane_marking_A", "lane_marking_B"]
    filled = en_v1_card(lane_marking_A=field("left_only"), lane_marking_B=field("straight_left"))
    _, out, _, _ = start(filled, cands=[in_left_lane, plain], thread="run-lane")
    assert (out["result"]["chart_id"], out["result"]["fault_A"]) == ("L-1", 100)
    # 도표 A가 촬영 차량이면 카드의 노면표시 칸도 맞바꿔 읽는다
    swapped = en_v1_card(swapped=True, lane_marking_A=field("straight_left"),
                         lane_marking_B=field("left_only"))
    _, out, _, _ = start(swapped, cands=[in_left_lane, plain], thread="run-lane-swap")
    assert (out["result"]["chart_id"], out["result"]["fault_A"]) == ("L-1", 100)


def test_what_if_on_the_unprotected_left_sign_assumes_a_bool_card_value():
    """비보호 좌회전 표지 가정 값은 카드 값처럼 참거짓으로 넣고 그 표지로 갈리는 도표를 고른다."""
    from app.api import judgments

    assert judgments._assumed(None, "unprotected_left_sign", "true")["value"] is True
    assert judgments._assumed(None, "night", "false")["value"] is False
    base = {**SIGNALIZED_V1, "relative_position": "opposite"}
    sign = candidate("U-1", {**base, "unprotected_left_sign": "true"}, 30, score=0.9)
    no_sign = candidate("U-2", {**base, "unprotected_left_sign": "false"}, 20, score=0.8)
    judgments.configure(deps=Dependencies(retrieve=AsyncMock(return_value=[sign, no_sign]),
                                          reviewer=AsyncMock(return_value=verdict())))
    try:
        result, _ = asyncio.run(judgments._simulate(en_v1_card(),
                                                    {"unprotected_left_sign": "true"}))
    finally:
        judgments.configure(deps=Dependencies())
    assert (result["status"], result["chart_id"], result["fault_A"]) == ("completed", "U-1", 30)


def test_fields_of_variants_that_no_longer_fit_the_card_are_not_asked():
    """카드와 이미 어긋난 변형의 칸은 묻지 않는다. 차2-2처럼 녹색 좌회전 변형만 비보호 표지로
    갈리고 적색 좌회전 변형은 표지와 무관(`*`)하면, 적색 좌회전 카드는 표지를 묻지 않는다."""
    base = {"location_type": "signalized_intersection", "movement_A": "straight",
            "movement_B": "left_turn", "signal_A": "green", "relative_position": "opposite"}
    no_sign = candidate("U-2", base, 0, score=0.9, base_variants=[
        {"variant": "녹색 좌회전", "base_A": 30, "base_B": 70,
         "conditions": {"signal_B": "green", "unprotected_left_sign": "false"}},
        {"variant": "적색 좌회전", "base_A": 10, "base_B": 90,
         "conditions": {"signal_B": "red", "unprotected_left_sign": ANY_VALUE}},
    ])
    sign = candidate("U-6", {**base, "signal_B": "green", "unprotected_left_sign": "true"}, 40,
                     score=0.8)
    cands = [no_sign, sign]
    _, out, _, _ = start(en_v1_card(), cands=cands, thread="run-red-left")  # 기본 카드 B 적색
    assert "__interrupt__" not in out
    assert (out["result"]["chart_id"], out["result"]["fault_A"]) == ("U-2", 10)
    green = en_v1_card(signal_B=field("green"))
    _, out, _, _ = start(green, cands=cands, thread="run-green-left")
    keys = [q["field_key"] for q in out["__interrupt__"][0].value["questions"]]
    assert keys == ["unprotected_left_sign"]
    for value, expected in ((False, ("U-2", 30)), (True, ("U-6", 40))):
        filled = en_v1_card(signal_B=field("green"), unprotected_left_sign=field(value))
        _, out, _, _ = start(filled, cands=cands, thread=f"run-green-left-{value}")
        assert (out["result"]["chart_id"], out["result"]["fault_A"]) == expected
