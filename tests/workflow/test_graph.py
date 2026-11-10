"""판단 그래프: 판정 세 가지·질문/재개·반박 재실행 상한·미확정 카드 차단. DB·모델 없이 돈다."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from langgraph.types import Command

from app.schemas.chart_scope import (
    REASON_FIELD_NOT_CONFIRMED,
    REASON_NO_REVIEWED_CHART,
    REASON_OUT_OF_SCOPE,
    REASON_PENDING_DECISION,
)
from app.workflow.graph import (
    MAX_REBUTTAL_RETRIES,
    STAGES,
    Dependencies,
    UnconfirmedCardError,
    build_graph,
    initial_state,
    question_id,
    thread_config,
)
from tests.workflow.synthetic import candidate, candidates, card, field, verdict


def run(card_, verdicts=None, cands=None, thread="run-1"):
    reviewer = AsyncMock(side_effect=list(verdicts) if verdicts is not None else [verdict()])
    retrieve = AsyncMock(return_value=candidates() if cands is None else cands)
    graph = build_graph(Dependencies(retrieve=retrieve, reviewer=reviewer))
    state = initial_state(card_, "criteria-1", "rule-1", judgment_version=1)
    out = asyncio.run(graph.ainvoke(state, thread_config(thread)))
    return graph, out, reviewer


def test_stage_names_match_graph_nodes():
    nodes = set(build_graph().get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == set(STAGES)


def test_confirmed_card_reaches_draft_with_code_calculated_fault():
    graph, out, reviewer = run(card())
    result = out["result"]
    assert result["status"] == "draft_ready"
    assert (result["chart_id"], result["edition"]) == ("S-1", "synthetic-1")
    assert (result["fault_A"], result["fault_B"]) == (20, 80)
    assert result["calculation_log"][-1] == "fault_A = clip(20 + 0 - 0, 0, 100) = 20; fault_B = 80"
    assert [c["layer"] for c in result["checks"]] == ["evidence", "rebuttal", "numerical"]
    assert all(c["passed"] for c in result["checks"])
    assert result["versions"] == {"case_id": "synthetic-case", "fact_card_version": 3,
                                  "judgment_version": 1, "report_version": None}
    assert [h["chunk"]["parent_id"] for h in result["evidence"]] == ["synthetic-standards:S-1"]
    assert result["questions"] == []
    assert out["stage"] == "build_judgment_result"
    assert reviewer.await_count == 1
    assert asyncio.run(graph.aget_state(thread_config("run-1"))).next == ()


def test_confirmed_night_applies_only_coded_modifier():
    c = card()
    c["fields"]["night"] = field(True)
    _, out, _ = run(c)
    result = out["result"]
    assert (result["fault_A"], result["fault_B"]) == (25, 75)
    assert "수정요소 적용 A 야간 +5 (synthetic-standards:S-1:night)" in result["calculation_log"]
    # 중대한 과실(코드 없음)은 자동 적용하지 않는다
    assert not any("중대한 과실" in line for line in result["calculation_log"])


def test_unconfirmed_field_asks_then_unknown_defers():
    c = card()
    c["fields"]["movement_B"] = field("left_turn", confirmed=False)
    reviewer = AsyncMock(return_value=verdict())
    retrieve = AsyncMock(return_value=candidates())
    graph = build_graph(Dependencies(retrieve=retrieve, reviewer=reviewer))
    cfg = thread_config("run-q")
    out = asyncio.run(graph.ainvoke(initial_state(c, "criteria-1", "rule-1"), cfg))
    questions = out["__interrupt__"][0].value["questions"]
    assert [q["field_key"] for q in questions] == ["movement_B"]
    assert questions[0]["question_id"] == question_id("run-q", "movement_B")
    assert questions[0]["allow_unknown"] and "left_turn" in questions[0]["options"]
    assert "result" not in out
    snapshot = asyncio.run(graph.aget_state(cfg))
    assert snapshot.next == ("ask_or_resume",)
    answer = {"question_id": questions[0]["question_id"], "expected_card_version": 3,
              "value": None, "unknown": True}
    out = asyncio.run(graph.ainvoke(Command(resume=[answer]), cfg))
    result = out["result"]
    assert result["status"] == "deferred"
    assert result["reasons"] == [REASON_FIELD_NOT_CONFIRMED.format(field_key="movement_B")]
    assert (result["chart_id"], result["fault_A"], result["questions"]) == (None, None, [])
    assert reviewer.await_count == 0


def test_resume_without_unknown_asks_again_instead_of_guessing():
    c = card()
    c["fields"]["signal_B"] = field("red", confirmed=False)
    graph = build_graph(Dependencies(retrieve=AsyncMock(return_value=candidates()),
                                     reviewer=AsyncMock(return_value=verdict())))
    cfg = thread_config("run-again")
    out = asyncio.run(graph.ainvoke(initial_state(c, "criteria-1", "rule-1"), cfg))
    qid = out["__interrupt__"][0].value["questions"][0]["question_id"]
    out = asyncio.run(graph.ainvoke(Command(resume=[{"question_id": qid, "expected_card_version": 3,
                                                     "value": "red", "unknown": False}]), cfg))
    # 값 응답은 A의 카드 경로(새 버전·새 실행)로 가야 하므로 같은 실행은 다시 멈춘다
    assert out["__interrupt__"][0].value["questions"][0]["question_id"] == qid


@pytest.mark.parametrize(("location", "reason"), [
    ("other", REASON_OUT_OF_SCOPE), ("roundabout", REASON_PENDING_DECISION),
])
def test_out_of_scope_defers_before_retrieval(location, reason):
    c = card()
    c["fields"]["location_type"] = field(location)
    retrieve = AsyncMock(return_value=candidates())
    graph = build_graph(Dependencies(retrieve=retrieve, reviewer=AsyncMock()))
    out = asyncio.run(graph.ainvoke(initial_state(c, "c", "r"), thread_config("run-scope")))
    assert out["result"]["status"] == "deferred"
    assert out["result"]["reasons"] == [reason]
    assert retrieve.await_count == 0


def test_unreviewed_candidates_defer_without_llm():
    cands = [candidate("U-1", {"location_type": "signalized_intersection"}, 40,
                       calculation_ready=False, reasons=["검수 전"])]
    _, out, reviewer = run(card(), cands=cands)
    assert out["result"]["status"] == "deferred"
    assert out["result"]["reasons"][0] == REASON_NO_REVIEWED_CHART
    assert out["result"]["evidence"] == []
    assert reviewer.await_count == 0


def test_mismatching_candidates_defer_with_no_reviewed_chart():
    attributes = {"location_type": "signalized_intersection", "movement_A": "u_turn",
                  "movement_B": "left_turn", "signal_A": "green", "signal_B": "red"}
    cands = [candidate("M-1", attributes, 40)]
    _, out, _ = run(card(), cands=cands)
    assert out["result"]["status"] == "deferred"
    assert out["result"]["reasons"][0] == REASON_NO_REVIEWED_CHART
    assert any("불일치" in r for r in out["result"]["reasons"])


def test_wrong_chart_reruns_once_with_preferred_alternative():
    c = card()
    c["fields"]["movement_B"] = field("straight")  # 두 후보 모두 B 직진으로 맞춘다
    cands = candidates()
    cands[0]["attributes"]["movement_B"] = "straight"
    verdicts = (verdict(True, "wrong_chart", "S-2"), verdict())
    _, out, reviewer = run(c, verdicts=verdicts, cands=cands)
    result = out["result"]
    assert result["status"] == "draft_ready"
    assert result["chart_id"] == "S-2"
    assert (result["fault_A"], result["fault_B"]) == (50, 50)
    assert out["rebuttal_retry_count"] == MAX_REBUTTAL_RETRIES == 1
    layers = [(ch["layer"], ch["passed"]) for ch in result["checks"]]
    assert layers == [("evidence", True), ("rebuttal", False), ("evidence", True),
                      ("rebuttal", True), ("numerical", True)]
    assert result["checks"][1]["rebuttal_kind"] == "wrong_chart"
    assert reviewer.await_count == 2


def test_second_wrong_chart_needs_confirmation_without_third_run():
    c = card()
    c["fields"]["movement_B"] = field("straight")
    cands = candidates()
    cands[0]["attributes"]["movement_B"] = "straight"
    cands.append(candidate("S-3", {**cands[1]["attributes"]}, 30, score=0.5))
    verdicts = (verdict(True, "wrong_chart", "S-2"), verdict(True, "wrong_chart", "S-3"))
    _, out, reviewer = run(c, verdicts=verdicts, cands=cands)
    result = out["result"]
    assert result["status"] == "needs_confirmation"
    assert result["chart_id"] == "S-2"
    assert (result["fault_A"], result["fault_B"]) == (None, None)
    assert result["reasons"][0].startswith("두 번째 도표 오선택")
    assert out["rebuttal_retry_count"] == 1
    assert reviewer.await_count == 2


@pytest.mark.parametrize("kind", ["missing_fact", "contradiction"])
def test_fact_rebuttals_need_confirmation_without_rerun(kind):
    _, out, reviewer = run(card(), verdicts=(verdict(True, kind, reasons=["쟁점"]),))
    result = out["result"]
    assert result["status"] == "needs_confirmation"
    assert result["checks"][-1]["rebuttal_kind"] == kind
    assert "쟁점" in result["reasons"]
    assert result["fault_A"] is None and result["calculation_log"] == []
    assert reviewer.await_count == 1


def test_unconfirmed_card_is_rejected():
    with pytest.raises(UnconfirmedCardError):
        run(card(status="draft"))


def test_missing_party_mapping_needs_confirmation():
    c = card()
    c["party_mapping"]["chart_party_B_id"] = None
    _, out, reviewer = run(c)
    assert out["result"]["status"] == "needs_confirmation"
    assert "당사자 매핑" in out["result"]["reasons"][0]
    assert reviewer.await_count == 0


def test_edition_outside_accident_date_fails_evidence_and_defers():
    cands = candidates()
    cands[0]["effective_to"] = "2025-12-31"
    _, out, reviewer = run(card(), cands=cands)
    result = out["result"]
    assert result["status"] == "deferred"
    assert result["reasons"][0] == REASON_NO_REVIEWED_CHART
    assert result["checks"][0]["layer"] == "evidence" and not result["checks"][0]["passed"]
    assert reviewer.await_count == 0


def test_invalid_reviewer_output_fails_the_run():
    with pytest.raises(ValueError, match="kind"):
        run(card(), verdicts=('{"holds": true, "kind": "nope", "preferred_chart_id": null, '
                              '"reasons": ["x"]}',))


def test_variant_condition_field_is_asked_before_selection():
    cands = candidates()
    cands[0]["base_variants"] = [
        {"variant": "가", "base_A": 20, "base_B": 80, "conditions": {"entry_order": "first"}},
        {"variant": "나", "base_A": 60, "base_B": 40, "conditions": {"entry_order": "second"}},
    ]
    graph = build_graph(Dependencies(retrieve=AsyncMock(return_value=cands),
                                     reviewer=AsyncMock(return_value=verdict())))
    cfg = thread_config("run-variant")
    out = asyncio.run(graph.ainvoke(initial_state(card(), "c", "r"), cfg))
    assert [q["field_key"] for q in out["__interrupt__"][0].value["questions"]] == ["entry_order"]
    c = card()
    c["fields"]["entry_order"] = field("second")
    out = asyncio.run(graph.ainvoke(initial_state(c, "c", "r"), thread_config("run-variant-2")))
    assert (out["result"]["fault_A"], out["result"]["fault_B"]) == (60, 40)
    assert out["result"]["calculation_log"][0] == "기본 과실 A 60 : B 40"
