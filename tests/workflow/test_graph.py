"""판단 그래프: 판정 세 가지·질문/재개·반박 재실행 상한·미확정 카드 차단. DB·모델 없이 돈다."""

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
from app.validation.evidence import PERIOD_UNKNOWN_WARNING
from app.workflow.graph import (
    CRITERIA_VERSION,
    MAX_REBUTTAL_RETRIES,
    STAGES,
    Dependencies,
    UnconfirmedCardError,
    build_graph,
    check_attempts,
    initial_state,
    question_id,
    run_outcome,
    thread_config,
)
from tests.workflow.synthetic import SIGNALIZED, candidate, candidates, card, field, verdict


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


def test_modifiers_are_never_auto_applied():
    c = card()
    c["fields"]["night"] = field(True)
    _, out, _ = run(c)
    result = out["result"]
    # 야간이 확인돼도 기본 과실만 계산하고, 수정요소는 모두 담당자 판단으로 넘긴다
    assert (result["fault_A"], result["fault_B"]) == (20, 80)
    assert not any("수정요소 적용" in line for line in result["calculation_log"])
    assert {m["decision"] for m in result["modifiers"]} == {"불명"}


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


def test_check_attempts_advance_only_after_wrong_chart():
    def check(layer, passed=True, kind=None):
        return {"layer": layer, "passed": passed, "reasons": [] if passed else ["r"],
                "rebuttal_kind": kind}

    retried = [check("evidence"), check("rebuttal", False, "wrong_chart"),
               check("evidence"), check("rebuttal"), check("numerical")]
    assert check_attempts(retried) == [0, 0, 1, 1, 1]
    stopped = [check("evidence"), check("rebuttal", False, "missing_fact")]
    assert check_attempts(stopped) == [0, 0]
    assert check_attempts([]) == []


def test_run_outcome_distinguishes_questions_from_results():
    _, out, _ = run(card())
    done = run_outcome(out)
    assert (done["status"], done["stage"], done["questions"]) == (
        "completed", "build_judgment_result", [])
    assert done["result"] == out["result"]
    c = card()
    c["fields"]["movement_B"] = field("left_turn", confirmed=False)
    graph = build_graph(Dependencies(retrieve=AsyncMock(return_value=candidates()),
                                     reviewer=AsyncMock(return_value=verdict())))
    waiting = run_outcome(asyncio.run(graph.ainvoke(initial_state(c, "c", "r"),
                                                    thread_config("run-outcome"))))
    assert (waiting["status"], waiting["stage"], waiting["result"]) == (
        "waiting_for_confirmation", "ask_or_resume", None)
    assert [q["field_key"] for q in waiting["questions"]] == ["movement_B"]
    with pytest.raises(RuntimeError):
        run_outcome({"stage": "check_evidence"})


def test_criteria_version_matches_fault_standards_manifest():
    import json
    from pathlib import Path

    manifest = Path(__file__).resolve().parents[2] / "data/corpora/fault_standards/manifest.json"
    editions = {d["edition"] for d in json.loads(manifest.read_text(encoding="utf-8"))["documents"]}
    assert CRITERIA_VERSION == f"fault_standards:{editions.pop()}" and not editions


def test_result_carries_report_fields_for_sections_3_to_5():
    _, out, _ = run(card())
    result = out["result"]
    assert result["base_fault_A"] == 20
    assert [(m["name"], m["applies_to"], m["delta"], m["decision"]) for m in result["modifiers"]] \
        == [("야간", "A", 5, "불명"), ("중대한 과실", "B", 20, "불명")]
    assert all(m["evidence_fields"] == [] and m["reason"] == "자동 적용하지 않음(담당자 판단)"
               for m in result["modifiers"])
    assert result["alternatives"] == [
        {"chart_id": "S-2", "reason": "제외 — 카드와 불일치 ['movement_B']"}]
    # 선택 근거만 reasons에 남고 제외 이유는 alternatives로 옮겼다
    assert result["reasons"] == ["S-1 선택: 확인한 칸 6개 일치 (accident_type, location_type, "
                                 "movement_A, signal_A, signal_B, movement_B)"]


def test_not_applicable_modifiers_are_left_out_of_report():
    cands = candidates()
    parent = cands[0]["parent_id"]
    not_applicable = {"modifier_id": f"{parent}:na", "party": "B", "name": "비적용 요소",
                      "code": None, "delta": None, "conditions": None}
    speeding = {"modifier_id": f"{parent}:speed", "party": "B", "name": "과속", "code": "speeding",
                "delta": 10, "conditions": None}
    cands[0]["modifiers"] += [not_applicable, speeding]
    _, out, _ = run(card(), cands=cands)
    decisions = {m["name"]: (m["decision"], m["reason"]) for m in out["result"]["modifiers"]}
    # 가감치가 없는 요소는 보고서 목록에서 뺀다(보고서가 가감치를 정수로 그린다)
    assert decisions == {
        "야간": ("불명", "자동 적용하지 않음(담당자 판단)"),
        "중대한 과실": ("불명", "자동 적용하지 않음(담당자 판단)"),
        "과속": ("불명", "자동 적용 제외(담당자 입력 전용)"),
    }


def test_rebutted_chart_is_listed_as_alternative_after_retry():
    c = card()
    c["fields"]["movement_B"] = field("straight")
    cands = candidates()
    cands[0]["attributes"]["movement_B"] = "straight"
    verdicts = (verdict(True, "wrong_chart", "S-2", ["B가 직진"]), verdict())
    _, out, _ = run(c, verdicts=verdicts, cands=cands)
    result = out["result"]
    assert result["chart_id"] == "S-2" and result["base_fault_A"] == 50
    assert result["alternatives"] == [{"chart_id": "S-1", "reason": "반박 검토로 제외 — B가 직진"}]


def test_deferred_without_chart_has_empty_report_fields():
    cands = [candidate("U-1", {"location_type": "signalized_intersection"}, 40,
                       calculation_ready=False, reasons=["검수 전"])]
    _, out, _ = run(card(), cands=cands)
    result = out["result"]
    assert (result["base_fault_A"], result["modifiers"], result["alternatives"]) == (None, [], [])


UNSIGNALIZED = {"location_type": "unsignalized_intersection", "road_priority": "stop_sign",
                "movement_A": "straight", "movement_B": "left_turn"}


def unsignalized_card():
    """신호 없는 교차로, 일시정지, A 직진 대 B 좌회전. 진입 순서는 확인하지 않은 상태다."""
    c = card()
    del c["fields"]["signal_A"], c["fields"]["signal_B"]
    c["fields"]["location_type"] = field("unsignalized_intersection")
    c["fields"]["road_priority"] = field("stop_sign")
    return c


def test_required_field_marked_any_is_not_asked():
    """남은 후보가 모두 진입 순서와 무관(`*`)하면 필수 칸이어도 묻지 않고 고른다."""
    cands = [candidate("N-1", {**UNSIGNALIZED, "entry_order": ANY_VALUE}, 30)]
    _, out, _ = run(unsignalized_card(), cands=cands)
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "N-1")
    assert (result["fault_A"], result["fault_B"]) == (30, 70)
    assert result["reasons"] == ["N-1 선택: 확인한 칸 5개 일치 (accident_type, location_type, "
                                 "road_priority, movement_A, movement_B)"]


def test_unknown_required_field_keeps_only_charts_that_ignore_it():
    """진입 순서를 모른다고 답하면 그 칸으로 갈리는 도표는 빠지고 `*`인 도표를 고른다."""
    by_order = candidate("N-1", UNSIGNALIZED, 20, score=0.9, base_variants=[
        {"variant": "가", "base_A": 20, "base_B": 80, "conditions": {"entry_order": "first"}},
        {"variant": "나", "base_A": 60, "base_B": 40, "conditions": {"entry_order": "second"}},
    ])
    ignores = candidate("N-2", UNSIGNALIZED, 30, score=0.5, base_variants=[
        {"variant": None, "base_A": 30, "base_B": 70, "conditions": {"entry_order": ANY_VALUE}},
    ])
    graph = build_graph(Dependencies(retrieve=AsyncMock(return_value=[by_order, ignores]),
                                     reviewer=AsyncMock(return_value=verdict())))
    cfg = thread_config("run-any")
    out = asyncio.run(graph.ainvoke(initial_state(unsignalized_card(), "c", "r"), cfg))
    [question] = out["__interrupt__"][0].value["questions"]
    assert question["field_key"] == "entry_order"
    answer = {"question_id": question["question_id"], "expected_card_version": 3,
              "value": None, "unknown": True}
    result = asyncio.run(graph.ainvoke(Command(resume=[answer]), cfg))["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "N-2")
    assert (result["fault_A"], result["fault_B"]) == (30, 70)
    assert "entry_order: 확인 불가 — N-2 선택에는 쓰지 않음" in result["reasons"]
    assert {"chart_id": "N-1", "reason": "제외 — 확인 불가 칸 ['entry_order']"} \
        in result["alternatives"]


def test_chart_without_fitting_variant_gives_way_to_the_next():
    """1순위라도 카드와 맞는 기본 과실 변형이 없으면 빼고 차순위를 고른다."""
    base = {"location_type": "signalized_intersection", "movement_A": "straight",
            "signal_A": "green"}
    # 값이 여럿인 도표처럼 B 진행 방향을 변형으로 나눴다. 카드의 B 좌회전과 맞는 변형이 없다
    split = candidate("V-1", {**base, "signal_B": "red"}, 50, score=0.9, base_variants=[
        {"variant": "가", "base_A": 50, "base_B": 50, "conditions": {"movement_B": "straight"}},
        {"variant": "나", "base_A": 50, "base_B": 50, "conditions": {"movement_B": "u_turn"}},
    ])
    # 변형 조건으로 둔 칸도 일치 칸 수에 들어간다
    fits = candidate("V-2", {**base, "movement_B": "left_turn"}, 20, score=0.5, base_variants=[
        {"variant": None, "base_A": 20, "base_B": 80, "conditions": {"signal_B": "red"}},
    ])
    _, out, _ = run(card(), cands=[split, fits])
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "V-2")
    assert result["reasons"] == ["V-2 선택: 확인한 칸 6개 일치 (accident_type, location_type, "
                                 "movement_A, signal_A, movement_B, signal_B)"]
    assert {"chart_id": "V-1", "reason": "제외 — 카드와 맞는 기본 과실 변형 없음 ['movement_B']"} \
        in result["alternatives"]


def test_unknown_edition_start_passes_evidence_with_warning():
    cands = candidates()
    cands[0]["effective_from"] = ""
    _, out, _ = run(card(), cands=cands)
    result = out["result"]
    assert result["status"] == "draft_ready"
    evidence = result["checks"][0]
    assert (evidence["layer"], evidence["passed"]) == ("evidence", True)
    assert evidence["reasons"] == [PERIOD_UNKNOWN_WARNING.format(edition="synthetic-1")]


def test_fields_of_contradicting_candidates_are_not_asked():
    """신호 교차로 카드에 신호 없는 교차로 도표가 섞여도 그 도표만 가르는 칸(도로 관계·진입 순서)은
    묻지 않는다. 더 물어도 장소가 다른 도표를 고를 수는 없다."""
    other = candidate("N-1", UNSIGNALIZED, 20, score=0.95, base_variants=[
        {"variant": "가", "base_A": 20, "base_B": 80, "conditions": {"entry_order": "first"}},
        {"variant": "나", "base_A": 60, "base_B": 40, "conditions": {"entry_order": "second"}},
    ])
    _, out, _ = run(card(), cands=[other, *candidates()])
    assert "__interrupt__" not in out
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "S-1")
    assert {"chart_id": "N-1", "reason": "제외 — 카드와 불일치 ['location_type']"} \
        in result["alternatives"]


def mismatching(n: int) -> list:
    return [candidate(f"M-{i}", {**SIGNALIZED, "movement_B": "u_turn"}, 40, score=0.8 - i / 100)
            for i in range(1, n + 1)]


def unreviewed(n: int) -> list:
    return [candidate(f"U-{i}", {"location_type": "signalized_intersection"}, 40,
                      score=0.5 - i / 100, calculation_ready=False, reasons=["검수 전"])
            for i in range(1, n + 1)]


def test_report_lists_only_the_first_excluded_and_unreviewed_candidates():
    """검색을 넓혀도 보고서 3번에는 검색 점수가 높은 제외 도표 5개·검수 전 도표 3개만 남긴다."""
    best = candidates()[0]
    _, out, _ = run(card(), cands=[best, *mismatching(7), *unreviewed(5)])
    alternatives = out["result"]["alternatives"]
    assert [a["chart_id"] for a in alternatives] == \
        ["M-1", "M-2", "M-3", "M-4", "M-5", "U-1", "U-2", "U-3"]


@pytest.mark.parametrize(
    ("cands", "listed"),
    [
        (unreviewed(5),
         ["U-1: 검수 전", "U-2: 검수 전", "U-3: 검수 전", "그 밖에 검수 전 도표 2개"]),
        (mismatching(7), [*(f"M-{i}: 카드와 불일치 ['movement_B']" for i in range(1, 6)),
                          "그 밖에 제외한 도표 2개"]),
    ],
)
def test_deferral_reasons_list_only_the_first_candidates(cands, listed):
    _, out, _ = run(card(), cands=cands)
    result = out["result"]
    assert result["status"] == "deferred"
    assert result["reasons"] == [REASON_NO_REVIEWED_CHART, *listed]


def test_accident_before_published_month_defers_without_llm():
    cands = candidates()
    cands[0]["effective_from"], cands[0]["published_month"] = "", "2026-11"
    _, out, reviewer = run(card(), cands=cands)
    result = out["result"]
    assert result["status"] == "deferred"
    assert result["reasons"] == [
        REASON_NO_REVIEWED_CHART,
        "사고 일자(2026-10-01)가 적용판 synthetic-1 발행월(2026-11)보다 앞선다",
    ]
    assert reviewer.await_count == 0
