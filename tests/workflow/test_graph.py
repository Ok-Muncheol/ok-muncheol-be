"""판단 그래프: 판정 세 가지·질문/재개·반박 재실행 상한·미확정 카드 차단. DB·모델 없이 돈다."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from langgraph.types import Command

from app.schemas.chart_scope import (
    ANY_VALUE,
    FIELD_OPTIONS,
    REASON_NO_REVIEWED_CHART,
    REASON_NO_SHAPE,
    REASON_OUT_OF_SCOPE,
)
from app.validation.evidence import PERIOD_UNKNOWN_WARNING
from app.validation.rebuttal import UNCONFIRMED_CLAIM_NOTE
from app.workflow.graph import (
    CRITERIA_VERSION,
    MAX_REBUTTAL_RETRIES,
    SCENARIO_DIFFERENT,
    STAGES,
    Dependencies,
    UnconfirmedCardError,
    build_graph,
    check_attempts,
    initial_state,
    question_id,
    run_outcome,
    thread_config,
    value_label,
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
    # '모름'도 답이다 — 다시 고르라고 하지 않고 값마다 결과를 보여 준다(추가 확인 필요)
    assert result["status"] == "needs_confirmation"
    assert result["reasons"] == [
        SCENARIO_DIFFERENT.format(labels="B 차량의 진행 방향"),
        "B 차량의 진행 방향 좌회전 → S-1 「synthetic S-1」 A 20 : B 80",
        "B 차량의 진행 방향 직진 → S-2 「synthetic S-2」 A 50 : B 50",
    ]
    assert result["scenarios"] == [
        {"fields": {"movement_B": "left_turn"}, "card_fields": {"movement_B": "left_turn"},
         "chart_id": "S-1", "title": "synthetic S-1", "variant": None, "base_A": 20, "base_B": 80},
        {"fields": {"movement_B": "straight"}, "card_fields": {"movement_B": "straight"},
         "chart_id": "S-2", "title": "synthetic S-2", "variant": None, "base_A": 50, "base_B": 50},
    ]
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


# 회전교차로는 11/16부터 범위 안이다(test_graph_non_intersection.py)
@pytest.mark.parametrize(("location", "reason"), [("other", REASON_OUT_OF_SCOPE)])
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


@pytest.mark.parametrize(("kind", "name"), [("missing_fact", "사실 누락"),
                                            ("contradiction", "모순")])
def test_fact_rebuttals_not_backed_by_the_card_are_noted_not_followed(kind, name):
    """선택 도표의 '*' 아닌 조건이 모두 확인한 사실과 같으면 LLM의 모순·사실 누락은 경고만 한다.

    판단 그래프는 그런 도표만 고르므로 이 주장은 칸 대조로 확인되지 않는다(gpt-4o-mini 오판, 11/12).
    """
    _, out, reviewer = run(card(), verdicts=(verdict(True, kind, reasons=["쟁점"]),))
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "S-1")
    rebuttal = result["checks"][1]
    assert (rebuttal["layer"], rebuttal["passed"], rebuttal["rebuttal_kind"]) == \
        ("rebuttal", True, None)
    assert rebuttal["reasons"] == [UNCONFIRMED_CLAIM_NOTE.format(name=name), "쟁점"]
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
    with pytest.raises(ValueError, match="verdict"):
        run(card(), verdicts=('{"verdict": "nope", "preferred_chart_id": null, '
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
        # 후보 모두 B의 움직임이 달라 이 움직임 조합의 도표가 없다는 안내가 먼저 붙는다
        (mismatching(7), [REASON_NO_SHAPE.format(pair="직진 대 좌회전"),
                          *(f"M-{i}: 카드와 불일치 ['movement_B']" for i in range(1, 6)),
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


def test_wrong_chart_pointing_at_the_selected_chart_needs_confirmation_without_rerun():
    """검토기가 고른 도표 자신을 wrong_chart로 적으면 다시 고르지 않고 담당자 확인으로 넘긴다."""
    _, out, reviewer = run(card(), verdicts=[verdict(True, "wrong_chart", "S-1", ["비율 이상"])])
    result = out["result"]
    assert result["status"] == "needs_confirmation"
    assert result["reasons"][0] == "도표 오선택 반박인데 고를 차순위 후보가 없음: 담당자 확인 필요"
    assert reviewer.await_count == 1


def test_unknown_scenarios_list_each_value_but_never_pick_one():
    """모르는 칸 값마다 기본 과실이 다르면 추가 확인 필요로 경우를 보여 주고 비율은 비운다."""
    by_order = candidate("N-1", UNSIGNALIZED, 20, score=0.9, base_variants=[
        {"variant": "가", "base_A": 20, "base_B": 80, "conditions": {"entry_order": "first"}},
        {"variant": "나", "base_A": 60, "base_B": 40, "conditions": {"entry_order": "second"}},
    ])
    graph = build_graph(Dependencies(retrieve=AsyncMock(return_value=[by_order]),
                                     reviewer=AsyncMock(return_value=verdict())))
    cfg = thread_config("run-scenarios")
    out = asyncio.run(graph.ainvoke(initial_state(unsignalized_card(), "c", "r"), cfg))
    [question] = out["__interrupt__"][0].value["questions"]
    answer = {"question_id": question["question_id"], "expected_card_version": 3,
              "value": None, "unknown": True}
    result = asyncio.run(graph.ainvoke(Command(resume=[answer]), cfg))["result"]
    assert (result["status"], result["chart_id"], result["fault_A"]) == (
        "needs_confirmation", None, None)
    assert [(s["fields"], s["variant"], s["base_A"]) for s in result["scenarios"]] == [
        ({"entry_order": "first"}, "가", 20), ({"entry_order": "second"}, "나", 60),
    ]
    assert result["reasons"] == [
        SCENARIO_DIFFERENT.format(labels="교차로 진입 순서(A 기준)"),
        "교차로 진입 순서(A 기준) 선진입 → N-1 「synthetic N-1」 (가) A 20 : B 80",
        "교차로 진입 순서(A 기준) 후진입 → N-1 「synthetic N-1」 (나) A 60 : B 40",
    ]


def test_no_scenarios_when_nothing_was_answered_unknown():
    """'모른다'고 한 칸이 없으면 경우별 결과를 만들지 않는다(기존 보류 사유 그대로)."""
    _, out, _ = run(card(), cands=[])
    result = out["result"]
    assert "scenarios" not in result
    assert result["status"] == "deferred"


def test_unknown_with_the_same_ratio_in_every_case_continues_to_draft():
    """모르는 칸이 어느 값이어도 기본 과실이 같으면 그 도표로 판단을 이어 가 초안이 된다."""
    same = candidate("N-1", UNSIGNALIZED, 30, score=0.9, base_variants=[
        {"variant": "가", "base_A": 30, "base_B": 70, "conditions": {"entry_order": "first"}},
        {"variant": "나", "base_A": 30, "base_B": 70, "conditions": {"entry_order": "second"}},
    ])
    graph = build_graph(Dependencies(retrieve=AsyncMock(return_value=[same]),
                                     reviewer=AsyncMock(return_value=verdict())))
    cfg = thread_config("run-same")
    out = asyncio.run(graph.ainvoke(initial_state(unsignalized_card(), "c", "r"), cfg))
    [question] = out["__interrupt__"][0].value["questions"]
    answer = {"question_id": question["question_id"], "expected_card_version": 3,
              "value": None, "unknown": True}
    result = asyncio.run(graph.ainvoke(Command(resume=[answer]), cfg))["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "N-1")
    assert (result["fault_A"], result["fault_B"], result["base_fault_A"]) == (30, 70, 30)
    assert result["reasons"][0].startswith(
        "모른다고 답한 칸(교차로 진입 순서(A 기준))은 어느 값이어도")
    assert len(result["scenarios"]) == 2  # 경우별 결과는 결과에 남는다


def test_unknown_with_different_ratios_follows_the_ai_value():
    """값마다 기본 과실이 다르면 모르는 칸의 AI 원값과 맞는 경우로 판단을 이어 간다."""
    from tests.workflow.synthetic import candidates as two  # S-1 좌회전 20:80, S-2 직진 50:50
    c = card()
    c["fields"]["movement_B"] = {**field(None), "source": "vision", "model_value": "straight"}
    _, out, reviewer = run(c, cands=two())
    result = out["result"]
    assert (result["status"], result["chart_id"]) == ("draft_ready", "S-2")
    assert (result["fault_A"], result["fault_B"]) == (50, 50)
    assert result["reasons"][0].startswith(
        "모른다고 답한 칸(B 차량의 진행 방향)은 AI 추정값(B 차량의 진행 방향 직진)으로 가정해 S-2")
    assert "다른 경우:" in result["reasons"]
    assert reviewer.await_count == 1  # 정식 판단이라 반박 검토도 돈다


def test_unknown_without_an_ai_value_still_needs_confirmation():
    """AI 원값이 없는 칸(담당자 입력)을 모른다고 하면 고를 근거가 없어 추가 확인 필요다."""
    from tests.workflow.synthetic import candidates as two
    c = card()
    c["fields"]["movement_B"] = field(None)  # source manual, model_value 없음
    _, out, _ = run(c, cands=two())
    assert out["result"]["status"] == "needs_confirmation"
    assert out["result"]["fault_A"] is None


def unknown(*keys):
    """card()에서 넘긴 칸을 담당자가 '모름'으로 확인한 카드(값 없음 + 확인함)."""
    c = card()
    for key in keys:
        c["fields"][key] = field(None)
    return c


def scenarios_of(card_, cands):
    """그 카드·후보로 끝까지 돌린 결과와 경우별 도표를 (도표, 변형, 칸→값)으로 줄인 것."""
    result = run(card_, cands=cands)[1]["result"]
    return result, [(s["chart_id"], s["variant"], s["fields"]) for s in result.get("scenarios", [])]


def test_scenarios_list_only_the_fields_each_chart_needs():
    """모르는 칸이 여럿이어도 경우에는 그 도표가 요구하는 칸만 적는다.

    2026-11-17 운영: 모르는 칸 15개의 값 조합을 통째로 넣어 보니 앞 40개가 모두 같은 도표였고
    도표가 쓰지 않는 칸까지 한 줄에 다 적혔다. 도표마다 그 도표의 조건만 적어야 서로 다른 도표가
    보인다.
    """
    left = candidate("S-1", {**SIGNALIZED, "movement_B": "left_turn"}, 20, score=0.9)
    straight = candidate("S-2", {**SIGNALIZED, "movement_B": "straight"}, 50, score=0.8,
                         base_variants=[
        {"variant": None, "base_A": 50, "base_B": 50, "conditions": {"night": "true"}},
    ])
    # 카드와 이미 어긋난 도표(A 좌회전)는 모르는 칸을 무엇으로 채워도 고를 수 없다
    other = candidate("S-3", {**SIGNALIZED, "movement_A": "left_turn", "movement_B": "u_turn"},
                      70, score=0.7)
    c = unknown("movement_B", "night")
    c["fields"]["relative_position"] = field(None)  # 어느 도표도 쓰지 않는 모르는 칸
    result, listed = scenarios_of(c, [left, straight, other])
    assert (result["status"], result["fault_A"]) == ("needs_confirmation", None)
    assert listed == [
        ("S-1", None, {"movement_B": "left_turn"}),
        ("S-2", None, {"movement_B": "straight", "night": "true"}),
    ]
    assert result["reasons"] == [
        # 안내 문장에는 '모른다'고 한 칸 중 경우에 걸린 칸만 적는다(상대 위치는 어느 도표도 안 쓴다)
        SCENARIO_DIFFERENT.format(labels="B 차량의 진행 방향, 야간 여부"),
        "B 차량의 진행 방향 좌회전 → S-1 「synthetic S-1」 A 20 : B 80",
        "B 차량의 진행 방향 직진, 야간 여부 예 → S-2 「synthetic S-2」 A 50 : B 50",
    ]


def test_many_unknown_fields_do_not_fake_a_common_ratio():
    """모르는 칸이 많아도 카드와 맞을 수 있는 도표를 모두 견주고, 비율이 다르면 공통 비율을 안 낸다.

    값 조합 앞 40개만 넣어 보던 때는 모르는 칸이 많으면 그 40개가 모두 한 도표라 "어느 값이어도
    같다"가 됐다(2026-11-17 운영 카드: 모르는 칸 15개 → 차44-1 20:80, 실제로는 비율이 다른 도표가
    10개 더 있었다).
    """
    charts = [
        candidate(f"S-{i}", {**SIGNALIZED, "movement_B": move,
                             "collision_signal_A": "red", "collision_signal_B": "green",
                             "lane_marking_A": "left_only", "lane_marking_B": "right_only",
                             "unprotected_left_sign": "true"}, base, score=1 - i / 10)
        for i, (move, base) in enumerate([("left_turn", 20), ("straight", 50), ("u_turn", 80)])
    ]
    c = unknown("movement_B", "collision_signal_A", "collision_signal_B", "lane_marking_A",
                "lane_marking_B", "unprotected_left_sign")
    result, listed = scenarios_of(c, charts)
    assert [(chart, fields["movement_B"]) for chart, _, fields in listed] == [
        ("S-0", "left_turn"), ("S-1", "straight"), ("S-2", "u_turn"),
    ]
    assert (result["status"], result["fault_A"], result["base_fault_A"]) == (
        "needs_confirmation", None, None)
    # 안내 문장은 모르는 칸 이름을 앞 4개까지만 적는다
    assert result["reasons"][0] == SCENARIO_DIFFERENT.format(
        labels="B 차량의 진행 방향, A 차량의 충돌 순간 신호, B 차량의 충돌 순간 신호, "
               "A 차량이 있던 차로의 노면표시(방향 화살표) 외 2칸")


def test_scenario_lines_merge_values_that_lead_to_the_same_variant():
    """사유 줄은 도표·변형·비율이 같고 한 칸의 값만 다른 경우를 한 줄로 묶는다.

    결과의 경우(scenarios)는 칸마다 값 하나씩 그대로 둔다.
    """
    split = candidate("S-1", {**SIGNALIZED, "movement_B": "straight"}, 30, score=0.9,
                      base_variants=[
        {"variant": None, "base_A": 30, "base_B": 70, "conditions": {"relative_position": "right"}},
        {"variant": None, "base_A": 30, "base_B": 70, "conditions": {"relative_position": "left"}},
    ])
    # 두 칸이 2×2로 갈린 변형 넷도 한 줄이 된다
    grid = candidate("S-2", {**SIGNALIZED, "movement_B": "left_turn"}, 50, score=0.8,
                     base_variants=[
        {"variant": None, "base_A": 50, "base_B": 50,
         "conditions": {"collision_signal_A": a, "collision_signal_B": b}}
        for a in ("yellow", "red") for b in ("yellow", "red")
    ])
    c = unknown("movement_B", "collision_signal_A", "collision_signal_B")
    c["fields"]["relative_position"] = field(None)
    result, listed = scenarios_of(c, [split, grid])
    assert listed[:2] == [
        ("S-1", None, {"movement_B": "straight", "relative_position": "right"}),
        ("S-1", None, {"movement_B": "straight", "relative_position": "left"}),
    ]
    assert len(listed) == 6
    assert result["reasons"][1:] == [
        # 값은 칸의 보기 순서다(왼쪽이 오른쪽보다, 황색이 적색보다 앞)
        "B 차량의 진행 방향 직진, 상대 위치(B가 가는 방향에서 볼 때 A가 있던 쪽) 왼쪽 또는 오른쪽"
        " → S-1 「synthetic S-1」 A 30 : B 70",
        "B 차량의 진행 방향 좌회전, A 차량의 충돌 순간 신호 황색 또는 적색, "
        "B 차량의 충돌 순간 신호 황색 또는 적색 → S-2 「synthetic S-2」 A 50 : B 50",
    ]


def test_scenarios_keep_variants_with_different_ratios_apart():
    """값만 다르고 비율이 다른 변형은 묶지 않는다. 같은 도표의 경우는 이어서 나온다."""
    by_side = candidate("S-1", {**SIGNALIZED, "movement_B": "straight"}, 30, score=0.5,
                        base_variants=[
        {"variant": "가", "base_A": 30, "base_B": 70, "conditions": {"relative_position": "left"}},
        {"variant": "나", "base_A": 80, "base_B": 20, "conditions": {"relative_position": "right"}},
    ])
    turn = candidate("S-2", {**SIGNALIZED, "movement_B": "left_turn"}, 20, score=0.9)
    c = unknown("movement_B")
    c["fields"]["relative_position"] = field(None)
    result, listed = scenarios_of(c, [turn, by_side])
    assert listed == [
        ("S-2", None, {"movement_B": "left_turn"}),
        ("S-1", "가", {"movement_B": "straight", "relative_position": "left"}),
        ("S-1", "나", {"movement_B": "straight", "relative_position": "right"}),
    ]
    assert len(result["reasons"]) == 4


def test_scenario_follows_the_chart_the_judgment_would_pick():
    """경우의 도표는 그 값을 카드에 넣었을 때 판단이 고를 도표다(일치 칸 수가 많은 쪽)."""
    loose = candidate("S-1", {**SIGNALIZED, "movement_B": "left_turn"}, 20, score=0.9)
    # 같은 좌회전이지만 비보호 표지까지 맞아야 하는 도표. 그 값이면 일치 칸이 더 많아 이쪽을 고른다
    tight = candidate("S-2", {**SIGNALIZED, "movement_B": "left_turn",
                              "unprotected_left_sign": "true"}, 60, score=0.5)
    c = unknown("movement_B", "unprotected_left_sign")
    _, listed = scenarios_of(c, [loose, tight])
    assert listed == [
        ("S-1", None, {"movement_B": "left_turn"}),
        ("S-2", None, {"movement_B": "left_turn", "unprotected_left_sign": "true"}),
    ]


def test_value_labels_cover_every_chart_field_value():
    """사유에 적는 값 낱말은 영어 값 그대로 나가지 않는다(범위 칸인 사고 유형·장소 제외)."""
    raw = [(key, value) for key, options in FIELD_OPTIONS.items()
           if key not in ("accident_type", "location_type")
           for value in options if value_label(key, value) == value]
    assert raw == []
    assert (value_label("relative_position", "left"), value_label("relative_position", "right")) \
        == ("왼쪽", "오른쪽")
    assert (value_label("road_priority", "major_A"), value_label("road_priority", "major_B")) \
        == ("A 쪽이 대로", "B 쪽이 대로")
    assert (value_label("night", "true"), value_label("door_open_B", "false")) == ("예", "아니오")
    # 같은 값(동시)도 칸마다 뜻이 다르다
    assert [value_label(key, "simultaneous")
            for key in ("u_turn_timing", "lane_change_timing", "entry_order")] \
        == ["동시 유턴", "양옆 차로 동시", "동시 진입"]


def test_deferred_result_lists_the_fewest_fixes_that_reach_a_chart():
    """확인한 값이 후보와 모두 어긋나 보류하면, 가장 적게 고쳐 고를 수 있는 도표와 값을 싣는다."""
    one_off = candidate("M-1", {**SIGNALIZED, "movement_B": "u_turn"}, 40, score=0.7)
    two_off = candidate("M-2", {**SIGNALIZED, "movement_B": "right_turn", "signal_B": "green"},
                        30, score=0.9)
    elsewhere = candidate("M-3", {"location_type": "unsignalized_intersection",
                                  "movement_A": "straight", "movement_B": "left_turn"}, 50)
    _, out, _ = run(card(), cands=[two_off, one_off, elsewhere])
    result = out["result"]
    assert (result["status"], result["reasons"][0]) == ("deferred", REASON_NO_REVIEWED_CHART)
    # 한 칸만 고치면 되는 M-1만. 두 칸(M-2)·장소(M-3)를 고쳐야 하는 도표는 싣지 않는다
    assert result["fixes"] == [{
        "fields": {"movement_B": "u_turn"}, "card_fields": {"movement_B": "u_turn"},
        "chart_id": "M-1", "title": "synthetic M-1", "variant": None, "base_A": 40, "base_B": 60,
    }]


def test_fixes_carry_the_unconfirmed_fields_the_chart_also_needs():
    """고칠 칸과 함께 그 변형이 요구하는 아직 확인하지 않은 칸도 싣는다(변형마다 한 경우)."""
    by_side = candidate("M-4", {**SIGNALIZED, "movement_B": "u_turn"}, 40, base_variants=[
        {"variant": "가", "base_A": 40, "base_B": 60, "conditions": {"relative_position": "left"}},
        {"variant": "나", "base_A": 70, "base_B": 30, "conditions": {"relative_position": "right"}},
    ])
    _, out, _ = run(card(), cands=[by_side])
    assert [(f["fields"], f["variant"], f["base_A"]) for f in out["result"]["fixes"]] == [
        ({"movement_B": "u_turn", "relative_position": "left"}, "가", 40),
        ({"movement_B": "u_turn", "relative_position": "right"}, "나", 70),
    ]


def test_no_fixes_when_only_the_place_differs_or_a_chart_was_chosen():
    elsewhere = candidate("M-3", {"location_type": "unsignalized_intersection",
                                  "movement_A": "straight", "movement_B": "left_turn"}, 50)
    _, out, _ = run(card(), cands=[elsewhere])
    assert out["result"]["status"] == "deferred" and "fixes" not in out["result"]
    _, out, _ = run(card(), thread="run-chosen")
    assert out["result"]["status"] == "draft_ready" and "fixes" not in out["result"]
