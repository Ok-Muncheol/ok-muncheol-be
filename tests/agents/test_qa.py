"""B: 근거 질의응답 에이전트(app/agents/qa.py). 모델·검색·가정 실행은 가짜로 바꾼다.

DB 없이 도구 루프·근거 표시(출처 포함)·가정 결과 분리·도구 횟수 상한·LLM 시간 상한을 검사한다.
API 계층(최신 판정·카드 읽기·메모리 가정 실행)은 tests/api/test_judgment_service.py가 본다.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app import llm as llm_policy
from app.agents import qa
from app.retrieval.embedding import RetrievalNotConfigured
from tests.fake_openai import FakeClients, completion, timeout_error
from tests.workflow.synthetic import hit

OFFICIAL_HIT = hit("synthetic-standards:S-1", 10, 0.9)
RESULT = {
    "versions": {"case_id": "case-1", "fact_card_version": 3, "judgment_version": 2,
                 "report_version": None},
    "status": "draft_ready", "chart_id": "S-1", "edition": "synthetic-1",
    "fault_A": 20, "fault_B": 80, "calculation_log": ["fault_A = 20; fault_B = 80"],
    "evidence": [OFFICIAL_HIT], "checks": [{"layer": "evidence", "passed": True, "reasons": []}],
    "questions": [], "reasons": [], "base_fault_A": 20, "modifiers": [], "alternatives": [],
}
HYPOTHESIS = {
    "changes": {"signal_B": "green"}, "status": "completed", "judgment_status": "draft_ready",
    "chart_id": "S-3", "fault_A": 50, "fault_B": 50, "reasons": [], "open_fields": [],
    "error": None,
}


def call(name: str, args: dict | None = None, id_: str = "c1") -> dict:
    return {"id": id_, "name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}


def reply(content: str | None = None, *calls: dict) -> dict:
    return {"content": content, "tool_calls": list(calls)}


def scripted(*replies: dict):
    """차례로 돌려줄 어시스턴트 메시지. 받은 메시지·tool_choice를 seen에 남긴다."""
    queue = list(replies)
    seen: list[dict] = []

    async def llm(messages, tools, tool_choice):
        seen.append({"messages": [dict(m) for m in messages], "tool_choice": tool_choice,
                     "tools": [t["function"]["name"] for t in tools]})
        return queue.pop(0)

    llm.seen = seen  # type: ignore[attr-defined]
    return llm


def context(simulate=None) -> qa.QaContext:
    return qa.QaContext(case_id="case-1", judgment_version=2, accident_date="2026-10-01",
                        result=RESULT, confirmed_facts={"signal_B": "red"}, simulate=simulate)


def ask(llm, ctx: qa.QaContext | None = None, **deps) -> dict:
    return asyncio.run(qa.answer_question(
        "왜 B가 80인가요?", ctx or context(), qa.QaDependencies(llm=llm, **deps)))


def tool_outputs(llm) -> list[dict]:
    return [json.loads(m["content"]) for m in llm.seen[-1]["messages"] if m["role"] == "tool"]


def test_answers_from_the_judgment_log_and_drops_unknown_refs():
    llm = scripted(
        reply(None, call("get_judgment_log")),
        reply("공식 판정은 A 20 : B 80입니다 [E1]. 결정례도 같습니다 [E7]."),
    )
    out = ask(llm)
    assert out["answer"] == "공식 판정은 A 20 : B 80입니다 [E1]. 결정례도 같습니다."
    [cite] = out["citations"]
    assert (cite["ref"], cite["chunk_id"], cite["corpus"], cite["page_start"]) \
        == ("E1", OFFICIAL_HIT["chunk"]["chunk_id"], "fault_standards", 10)
    assert (out["case_id"], out["judgment_version"]) == ("case-1", 2)
    assert out["tool_calls"] == [{"name": "get_judgment_log", "ok": True}]
    assert (out["hypotheticals"], out["fact_change_hint"]) == ([], None)
    [log] = tool_outputs(llm)
    assert (log["fault_A"], log["fault_B"], log["chart_id"]) == (20, 80, "S-1")
    assert log["confirmed_facts"] == {"signal_B": "red"}
    assert log["evidence"][0]["ref"] == "E1"
    first = llm.seen[0]
    assert first["tool_choice"] == "auto"
    assert first["tools"] == ["get_judgment_log", "search_dispute_cases",
                              "search_policy_terms", "simulate"]
    assert "판정 draft_ready" in first["messages"][1]["content"]


def test_simulate_result_stays_separate_from_the_official_one():
    simulate = AsyncMock(return_value=(HYPOTHESIS, [hit("synthetic-standards:S-3", 12, 0.8)]))
    llm = scripted(
        reply(None, call("simulate", {"changes": {"signal_B": "green"}})),
        reply("가정 결과는 A 50 : B 50입니다 [E1]. 공식 결과는 그대로입니다."),
    )
    out = ask(llm, context(simulate=simulate))
    simulate.assert_awaited_once_with({"signal_B": "green"})
    assert out["hypotheticals"] == [HYPOTHESIS]
    assert out["fact_change_hint"] == qa.FACT_CHANGE_HINT
    assert [c["parent_id"] for c in out["citations"]] == ["synthetic-standards:S-3"]
    assert out["tool_calls"] == [{"name": "simulate", "ok": True}]


def test_invalid_assumptions_go_back_to_the_model_without_running():
    simulate = AsyncMock()
    llm = scripted(
        reply(None, call("simulate", {"changes": {"speed": "fast"}}, "c1"),
              call("simulate", {"changes": {"signal_B": "purple"}}, "c2"),
              call("simulate", {"changes": {}}, "c3")),
        reply("가정할 수 없는 칸이라 답할 수 없습니다."),
    )
    out = ask(llm, context(simulate=simulate))
    simulate.assert_not_awaited()
    errors = [o["error"] for o in tool_outputs(llm)]
    assert "바꿀 수 없는 칸" in errors[0] and "signal_B의 값은" in errors[1]
    assert "비어 있으면 안 된다" in errors[2]
    assert [c["ok"] for c in out["tool_calls"]] == [False, False, False]
    assert (out["hypotheticals"], out["fact_change_hint"]) == ([], None)


def test_simulate_needs_the_card_of_the_official_judgment():
    llm = scripted(reply(None, call("simulate", {"changes": {"signal_B": "green"}})),
                   reply("카드를 읽지 못해 가정 결과를 낼 수 없습니다."))
    out = ask(llm, context(simulate=None))
    assert "사실 카드를 읽지 못해" in tool_outputs(llm)[0]["error"]
    assert out["tool_calls"] == [{"name": "simulate", "ok": False}]


def test_tool_calls_stop_at_the_budget_and_every_call_gets_a_reply():
    many = [call("get_judgment_log", id_=f"c{i}") for i in range(3)]
    more = [call("get_judgment_log", id_=f"d{i}") for i in range(3)]
    llm = scripted(reply(None, *many), reply(None, *more), reply("답입니다."))
    out = ask(llm)
    assert len(out["tool_calls"]) == qa.MAX_TOOL_CALLS == 5
    assert [s["tool_choice"] for s in llm.seen] == ["auto", "auto", "none"]
    outputs = tool_outputs(llm)
    assert len(outputs) == 6  # 상한을 넘은 호출에도 응답 메시지를 단다
    assert "질문당 5회까지" in outputs[-1]["error"]
    assert out["answer"] == "답입니다."


def test_search_passes_the_case_date_and_filters_and_reports_missing_index():
    found = AsyncMock(return_value=[hit("synthetic-dispute:case-9", 3, 0.7)])
    missing = AsyncMock(side_effect=RetrievalNotConfigured("색인 없음"))
    llm = scripted(
        reply(None, call("search_dispute_cases", {"text": "좌회전 사고", "chart_id": "S-1"}, "c1"),
              call("search_policy_terms", {"text": "자기부담금", "coverage": " "}, "c2")),
        reply("결정례가 있습니다 [E1]. 약관은 아직 찾을 수 없습니다."),
    )
    out = ask(llm, search_dispute_cases=found, search_policy_terms=missing)
    query = found.await_args.args[0]
    assert query == {"text": "좌회전 사고", "accident_date": "2026-10-01",
                     "filters": {"chart_id": "S-1"}, "top_k": qa.SEARCH_TOP_K}
    assert missing.await_args.args[0]["filters"] == {}  # 빈 필터 값은 보내지 않는다
    assert out["tool_calls"] == [{"name": "search_dispute_cases", "ok": True},
                                 {"name": "search_policy_terms", "ok": False}]
    assert "검색 색인" in tool_outputs(llm)[1]["error"]
    assert out["citations"][0]["parent_id"] == "synthetic-dispute:case-9"


def test_unknown_tools_and_bad_arguments_are_errors_for_the_model():
    llm = scripted(
        reply(None, {"id": "c1", "name": "delete_report", "arguments": "{}"},
              {"id": "c2", "name": "get_judgment_log", "arguments": "[1]"}),
        reply("그 일은 할 수 없습니다."),
    )
    out = ask(llm)
    assert [o["error"] for o in tool_outputs(llm)] == [
        "없는 도구다: delete_report", "도구 인자는 JSON 객체여야 한다"]
    assert [c["ok"] for c in out["tool_calls"]] == [False, False]


def test_empty_answer_is_a_failure():
    with pytest.raises(qa.QaFailed):
        ask(scripted(reply("  [E3] ")))


def test_openai_model_needs_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(qa.QaNotConfigured):
        asyncio.run(qa.openai_qa_llm([], qa.TOOLS, "auto"))


def test_citations_carry_their_source_and_official_evidence_stays_official():
    # 가정 실행이 공식과 같은 근거(S-1)도 돌려주면, 판단 로그가 다시 돌려줄 때 공식으로 올린다
    simulate = AsyncMock(return_value=(HYPOTHESIS, [hit("synthetic-standards:S-3", 12, 0.8),
                                                    OFFICIAL_HIT]))
    found = AsyncMock(return_value=[hit("synthetic-dispute:case-9", 3, 0.7)])
    llm = scripted(
        reply(None, call("simulate", {"changes": {"signal_B": "green"}}, "c1"),
              call("search_dispute_cases", {"text": "좌회전 사고"}, "c2"),
              call("get_judgment_log", {}, "c3")),
        reply("가정하면 S-3입니다 [E1]. 공식 근거입니다 [E2]. 결정례입니다 [E3]."),
    )
    out = ask(llm, context(simulate=simulate), search_dispute_cases=found)
    assert [(c["ref"], c["parent_id"], c["source"]) for c in out["citations"]] == [
        ("E1", "synthetic-standards:S-3", "hypothesis"),
        ("E2", "synthetic-standards:S-1", "official"),
        ("E3", "synthetic-dispute:case-9", "search"),
    ]
    # 모델도 근거마다 출처를 본다
    simulated, searched, log = tool_outputs(llm)
    assert simulated["evidence"][0]["source"] == "hypothesis"
    assert searched["hits"][0]["source"] == "search"
    assert log["evidence"][0] == {**log["evidence"][0], "ref": "E2", "source": "official"}


def test_simulate_runs_at_most_twice_per_question():
    simulate = AsyncMock(return_value=(HYPOTHESIS, []))
    changes = {"changes": {"signal_B": "green"}}
    llm = scripted(
        reply(None, call("simulate", changes, "c1"), call("simulate", changes, "c2"),
              call("simulate", changes, "c3")),
        reply("가정 결과 두 가지입니다."),
    )
    out = ask(llm, context(simulate=simulate))
    assert simulate.await_count == qa.MAX_SIMULATIONS == 2
    assert [c["ok"] for c in out["tool_calls"]] == [True, True, False]
    assert "질문당 2번까지" in tool_outputs(llm)[2]["error"]
    assert len(out["hypotheticals"]) == 2


def budget_ask(llm, budget: float, ctx: qa.QaContext | None = None) -> dict:
    return asyncio.run(qa.answer_question(
        "만약 B가 녹색이었다면?", ctx or context(),
        qa.QaDependencies(llm=llm, llm_budget_seconds=budget)))


def test_the_llm_time_budget_does_not_count_tool_time():
    async def slow_simulate(changes):
        await asyncio.sleep(0.3)  # 도구(가정 실행)는 상한을 넘겨도 된다
        return HYPOTHESIS, []

    llm = scripted(reply(None, call("simulate", {"changes": {"signal_B": "green"}})),
                   reply("가정 결과입니다."))
    out = budget_ask(llm, 0.2, context(simulate=slow_simulate))
    assert out["answer"] == "가정 결과입니다."


def test_a_slow_model_call_is_cut_at_the_remaining_budget():
    async def slow(messages, tools, tool_choice):
        await asyncio.sleep(1)
        return reply("늦은 답")

    with pytest.raises(qa.QaTimedOut):
        budget_ask(slow, 0.05)


def test_the_budget_is_the_sum_of_model_calls():
    async def steady(messages, tools, tool_choice):
        await asyncio.sleep(0.06)
        return reply(None, call("get_judgment_log", id_=f"c{len(messages)}"))

    with pytest.raises(qa.QaTimedOut):
        budget_ask(steady, 0.15)  # 두 번째까지 0.12초, 세 번째에서 넘는다


def test_the_openai_model_call_is_retried_once_then_times_out(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm_policy, "BACKOFF_SECONDS", 0)
    clients = FakeClients(timeout_error(), timeout_error())
    monkeypatch.setattr(llm_policy, "openai_client", clients)
    with pytest.raises(qa.QaTimedOut):
        asyncio.run(qa.openai_qa_llm([], qa.TOOLS, "auto"))
    assert len(clients.requests) == llm_policy.MAX_ATTEMPTS == 2


def test_the_openai_model_answer_after_a_transient_failure(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm_policy, "BACKOFF_SECONDS", 0)
    clients = FakeClients(timeout_error(), completion("답입니다."))
    monkeypatch.setattr(llm_policy, "openai_client", clients)
    out = asyncio.run(qa.openai_qa_llm([], qa.TOOLS, "none"))
    assert out == {"content": "답입니다.", "tool_calls": []}
    assert clients.requests[-1]["tool_choice"] == "none"
