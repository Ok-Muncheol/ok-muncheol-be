"""검증 ②: LLM 응답 검증과 반박 종류 분류. 모양이 틀리면 추측하지 않는다."""

import asyncio
import json

import pytest

from app.validation.rebuttal import (
    ReviewerNotConfigured,
    openai_reviewer,
    parse_verdict,
    review_payload,
    review_rebuttal,
)
from tests.workflow.synthetic import candidates, card, verdict


def test_passing_verdict_has_no_kind():
    assert parse_verdict(verdict(), ["S-2"]) == {
        "holds": False, "kind": None, "preferred_chart_id": None, "reasons": []}


def test_wrong_chart_requires_alternative_and_other_kinds_drop_preferred():
    out = parse_verdict(verdict(True, "wrong_chart", "S-2"), ["S-2"])
    assert out["preferred_chart_id"] == "S-2"
    out = parse_verdict(verdict(True, "missing_fact", "S-2"), ["S-2"])
    assert out["preferred_chart_id"] is None and out["kind"] == "missing_fact"


@pytest.mark.parametrize("raw", [
    "not json", json.dumps({"holds": "yes", "reasons": []}),
    json.dumps({"holds": True, "kind": "typo", "preferred_chart_id": None, "reasons": ["r"]}),
    json.dumps({"holds": True, "kind": "wrong_chart", "preferred_chart_id": "S-9",
                "reasons": ["r"]}),
    json.dumps({"holds": True, "kind": "contradiction", "preferred_chart_id": None, "reasons": []}),
    json.dumps({"holds": False, "reasons": "x"}),
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
