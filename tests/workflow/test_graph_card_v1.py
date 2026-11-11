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
from tests.workflow.synthetic import candidate, candidates, field, v1_card, verdict


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
