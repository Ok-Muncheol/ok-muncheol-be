"""검증 ②: LLM 응답 검증과 반박 종류 분류. 모양이 틀리면 추측하지 않는다."""

import asyncio
import json

import pytest

from app.validation.rebuttal import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    UNCONFIRMED_CLAIM_NOTE,
    VERDICTS,
    ReviewerNotConfigured,
    claim_confirmed,
    openai_reviewer,
    parse_verdict,
    review_payload,
    review_rebuttal,
)
from tests.workflow.synthetic import candidates, card, en_v1_card, field, verdict


def test_passing_verdict_has_no_kind():
    assert parse_verdict(verdict(), ["S-2"]) == {
        "holds": False, "kind": None, "preferred_chart_id": None, "reasons": []}


def test_wrong_chart_requires_alternative_and_other_kinds_drop_preferred():
    out = parse_verdict(verdict(True, "wrong_chart", "S-2"), ["S-2"])
    assert out["preferred_chart_id"] == "S-2"
    out = parse_verdict(verdict(True, "missing_fact", "S-2"), ["S-2"])
    assert out["preferred_chart_id"] is None and out["kind"] == "missing_fact"


@pytest.mark.parametrize("raw", [
    "not json", json.dumps(["none"]),
    json.dumps({"verdict": "typo", "preferred_chart_id": None, "reasons": ["r"]}),
    json.dumps({"verdict": "contradiction", "preferred_chart_id": None, "reasons": []}),
    json.dumps({"verdict": "none", "preferred_chart_id": None, "reasons": "x"}),
    # 예전 형식은 받지 않는다. 이 모순(반박 성립인데 종류 없음)이 실제로 나왔다(11/12)
    json.dumps({"holds": True, "kind": None, "preferred_chart_id": None, "reasons": ["일치"]}),
])
def test_malformed_verdicts_raise(raw):
    with pytest.raises(ValueError):
        parse_verdict(raw, ["S-2"])


def test_payload_contains_only_confirmed_facts_and_no_numbers_request():
    c = card()
    c["fields"]["movement_B"]["verification_status"] = "needs_review"
    cands = candidates()
    sel = {"chart_id": "S-1", "parent_id": cands[0]["parent_id"], "edition": "synthetic-1",
           "variant": cands[0]["base_variants"][0], "applied_modifiers": [],
           "unresolved_modifiers": [], "alternatives": ["S-2"], "reasons": ["why"]}
    payload = json.loads(review_payload(c, sel, cands))
    assert "movement_B" not in payload["confirmed_facts"]
    assert payload["selected_chart"]["title"] == "synthetic S-1"
    assert [a["chart_id"] for a in payload["alternative_charts"]] == ["S-2"]


def test_review_returns_check_and_preferred():
    cands = candidates()
    sel = {"chart_id": "S-1", "parent_id": cands[0]["parent_id"], "edition": "synthetic-1",
           "variant": cands[0]["base_variants"][0], "applied_modifiers": [],
           "unresolved_modifiers": [], "alternatives": ["S-2"], "reasons": []}

    async def reviewer(system: str, user: str) -> str:
        assert "JSON" in system and "S-2" in user
        return verdict(True, "wrong_chart", "S-2", ["B가 직진"])

    check, preferred = asyncio.run(review_rebuttal(card(), sel, cands, reviewer))
    assert check == {"layer": "rebuttal", "passed": False, "reasons": ["B가 직진"],
                     "rebuttal_kind": "wrong_chart"}
    assert preferred == "S-2"


def test_default_reviewer_refuses_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # NotImplementedError 계열이라 판단 실행은 dependency_not_ready로 기록된다
    with pytest.raises(ReviewerNotConfigured, match="OPENAI_API_KEY"):
        asyncio.run(openai_reviewer("s", "u"))
    assert issubclass(ReviewerNotConfigured, NotImplementedError)


def test_default_reviewer_uses_the_llm_call_policy(monkeypatch):
    # 호출 1회 상한·재시도 1회(app/llm.py) — 공식 판단과 질의응답의 가정 실행이 같은 검토기를 쓴다
    from app import llm as llm_policy
    from tests.fake_openai import FakeClients, completion, status_error, timeout_error

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm_policy, "BACKOFF_SECONDS", 0)
    clients = FakeClients(timeout_error(), completion(verdict()))
    monkeypatch.setattr(llm_policy, "openai_client", clients)
    assert asyncio.run(openai_reviewer("s", "u")) == verdict()
    assert len(clients.requests) == 2

    refused = FakeClients(status_error(429), completion(verdict()))
    monkeypatch.setattr(llm_policy, "openai_client", refused)
    with pytest.raises(Exception, match="upstream"):
        asyncio.run(openai_reviewer("s", "u"))
    assert len(refused.requests) == 1  # 4xx는 다시 보내지 않는다


def chosen(cands):
    return {"chart_id": "S-1", "parent_id": cands[0]["parent_id"], "edition": "synthetic-1",
            "variant": cands[0]["base_variants"][0], "applied_modifiers": [],
            "unresolved_modifiers": [], "alternatives": ["S-2"], "reasons": []}


def test_payload_facts_follow_the_chart_for_swapped_cards():
    """도표 A = 카드 B(촬영 차량)인 카드도 사실을 도표 A/B 기준으로 넣는다. 같은 사고면 같다.

    카드 칸을 그대로 넣으면 신호·진행 방향이 도표 속성과 반대로 보여 '모순'으로 판정될 수 있었다.
    """
    cands = candidates()
    plain = json.loads(review_payload(en_v1_card(), chosen(cands), cands))
    swapped = json.loads(review_payload(en_v1_card(swapped=True), chosen(cands), cands))
    assert swapped["confirmed_facts"] == plain["confirmed_facts"]
    attributes = cands[0]["attributes"]
    assert {k: swapped["confirmed_facts"][k] for k in ("signal_A", "movement_B")} == \
        {k: attributes[k] for k in ("signal_A", "movement_B")}
    assert "party_note" in swapped and "party_note" not in plain


def test_payload_lists_confirmed_unknown_fields_apart_from_facts():
    cands = candidates()
    c = en_v1_card(movement_B=field(None))  # 담당자가 모름으로 확인했다
    payload = json.loads(review_payload(c, chosen(cands), cands))
    assert "movement_B" not in payload["confirmed_facts"]
    assert payload["unknown_facts"] == ["movement_B"]
    assert payload["confirmed_facts"]["narrative"] == "합성 서술"


def test_verdict_is_one_value_so_rebuttal_and_kind_cannot_disagree():
    """판정은 하나다. 반박 없음(none)이면 차순위 후보 ID가 와도 버린다."""
    out = parse_verdict(json.dumps({"verdict": "none", "preferred_chart_id": "S-2",
                                    "reasons": ["선택 도표가 사실과 맞다"]}), ["S-2"])
    assert out == {"holds": False, "kind": None, "preferred_chart_id": None,
                   "reasons": ["선택 도표가 사실과 맞다"]}
    assert RESPONSE_SCHEMA["required"] == ["reasons", "verdict", "preferred_chart_id"]
    assert RESPONSE_SCHEMA["properties"]["verdict"]["enum"] == list(VERDICTS)
    assert VERDICTS == ("none", "wrong_chart", "missing_fact", "contradiction")


def test_prompt_explains_the_any_value():
    # 진입 순서 `*`인 도표에서 모델이 판정을 헷갈렸다. `*`이 반박 사유가 아님을 알린다
    assert "'*'" in SYSTEM_PROMPT and "verdict" in SYSTEM_PROMPT


def test_wrong_chart_outside_alternatives_goes_to_staff_instead_of_failing():
    """차순위 후보가 아닌 도표(고른 도표 자신 등)를 가리키면 멈추지 않고 담당자에게 넘긴다."""
    out = parse_verdict(verdict(True, "wrong_chart", "S-1", ["B 과실이 더 커야 한다"]), ["S-2"])
    assert (out["holds"], out["kind"], out["preferred_chart_id"]) == (True, "wrong_chart", None)
    assert out["reasons"][-1] == \
        "반박 검토가 차순위 후보가 아닌 도표(S-1)를 골라 다시 고르지 않았다"


def test_reasons_come_before_the_verdict_in_the_response():
    # 구조화 출력은 스키마 순서로 생성된다. 판정이 먼저면 gpt-4o-mini가 대조하기 전에 판정을 정해
    # "일치함"이라 쓰고도 모순을 냈다(11/13 시범, 충돌 순간 신호 도표 변형 10개 모두)
    assert list(RESPONSE_SCHEMA["properties"]) == ["reasons", "verdict", "preferred_chart_id"]


def test_no_rebuttal_check_leaves_out_the_comparison_notes():
    """이유를 먼저 쓰게 하면 반박이 없어도 칸 대조를 길게 쓴다. 통과한 검증 사유에는 싣지 않는다."""
    cands = candidates()

    async def reviewer(system: str, user: str) -> str:
        return json.dumps({"reasons": ["signal_A green 일치", "signal_B red 일치"],
                           "verdict": "none", "preferred_chart_id": None})

    check, preferred = asyncio.run(review_rebuttal(card(), chosen(cands), cands, reviewer))
    assert check == {"layer": "rebuttal", "passed": True, "reasons": [], "rebuttal_kind": None}
    assert preferred is None


def test_prompt_separates_entry_and_collision_signals():
    # gpt-4o-mini가 signal_A(진입 시)와 collision_signal_A(충돌 순간)를 같은 칸으로 읽어 맞게 고른
    # 도표를 '모순'으로 돌렸다(11/13 차1-2·2-5·3-5)
    assert "collision_signal_A" in SYSTEM_PROMPT and "서로 다른 칸" in SYSTEM_PROMPT


def test_prompt_limits_the_reviewer_to_comparing_conditions():
    # gpt-4o-mini가 과실비율을 따져 맞는 도표를 '모순'으로 돌린 일(11/12)을 막는다
    assert "과실비율이 타당한지" in SYSTEM_PROMPT and "칸마다 대조" in SYSTEM_PROMPT


def test_claims_are_checked_field_by_field():
    """모순은 같은 칸 값이 다를 때, 사실 누락은 '*' 아닌 조건 칸이 사실에 없을 때만 확인된다."""
    cands = candidates()
    sel = chosen(cands)  # S-1: 카드와 모든 칸이 같다
    assert claim_confirmed("contradiction", card(), sel, cands) is False
    assert claim_confirmed("missing_fact", card(), sel, cands) is False
    other = {**sel, "chart_id": "S-2"}  # S-2는 movement_B가 straight라 카드(left_turn)와 다르다
    assert claim_confirmed("contradiction", card(), other, cands) is True
    unknown = card()
    del unknown["fields"]["signal_B"]
    assert claim_confirmed("missing_fact", unknown, sel, cands) is True
    assert claim_confirmed("contradiction", card(), {**sel, "chart_id": "ghost"}, cands) is None


def test_confirmed_claim_still_blocks_and_unconfirmed_claim_is_a_warning():
    cands = candidates()

    async def says(kind):
        async def reviewer(system, user):
            return verdict(True, kind, reasons=["쟁점"])
        return reviewer

    other = {**chosen(cands), "chart_id": "S-2"}
    contradiction = asyncio.run(says("contradiction"))
    check, _ = asyncio.run(review_rebuttal(card(), other, cands, contradiction))
    assert (check["passed"], check["rebuttal_kind"]) == (False, "contradiction")
    check, _ = asyncio.run(review_rebuttal(card(), chosen(cands), cands, contradiction))
    assert check["passed"] is True and check["rebuttal_kind"] is None
    assert check["reasons"][0] == UNCONFIRMED_CLAIM_NOTE.format(name="모순")
