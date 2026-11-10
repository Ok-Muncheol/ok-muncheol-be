"""B(통합): 보고서 경로 → D 서비스(메모리 저장소) 왕복.

경로·본문 모양·응답 직렬화(TypedDict 응답 모델이 D 결과의 필드를 버리거나 바꾸지 않는지)·오류 코드를
실제 앱으로 확인한다. D 서비스 로직 자체는 tests/test_d_*.py가 검사한다.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.api import reports
from app.calculation.rules import RuleSet
from app.config import Settings, get_settings
from app.main import app
from tests import fixtures_d as fx

ACTOR, CASE = "handler-1", "synthetic-case"
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}


@pytest.fixture
def client():
    reports.configure(repo=reports.InMemoryReportRepository(),
                      rules=RuleSet(version="reviewed-test"), fetch_clauses=None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="local", on_vercel=False, supabase_url=None, supabase_jwt_secret=None,
        jwt_audience="authenticated", cors_allow_origins=(), dev_actor_id=ACTOR)
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()
    reports.configure(repo=reports.InMemoryReportRepository(), rules=RuleSet(),
                      fetch_clauses=None)


def as_json(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False))


def contract_view(report: object) -> dict:
    """응답 모델(TypedDict)을 거쳐도 서비스 결과와 필드·값이 같아야 한다."""
    return as_json(report)  # type: ignore[return-value]


def test_report_flow_through_routes(client):
    asyncio.run(reports.submit_judgment(ACTOR, fx.card(), fx.judgment()))
    res = client.get(f"/cases/{CASE}/reports/1")
    assert res.status_code == 200, res.text
    assert res.json() == contract_view(asyncio.run(reports.get_report(ACTOR, CASE, 1)))
    # 보고서 3·4·5번이 쓰는 판단 필드(JudgmentResult)가 응답에 남는다
    judgment = res.json()["judgment"]
    assert judgment["base_fault_A"] == 30
    assert judgment["modifiers"][0]["decision"] == "미적용"
    assert judgment["alternatives"][0]["chart_id"] == "synthetic-chart-24"

    body = {"documents": [fx.contract(), fx.estimate(), *fx.evidence()], "case_facts": NO_FLAGS}
    res = client.post(f"/cases/{CASE}/documents", json=body)
    assert res.status_code == 200, res.text
    version = res.json()["versions"]["report_version"]
    assert res.json() == contract_view(asyncio.run(reports.get_report(ACTOR, CASE, version)))

    res = client.post(f"/cases/{CASE}/reports/{version}/confirm",
                      json={"counterparty": fx.counterparty()})
    assert res.status_code == 200, res.text
    confirmed = res.json()
    assert confirmed["package"] is not None and confirmed["html"]
    assert (confirmed["claim_blocked_reasons"], confirmed["claim_blocked_reason"]) == ([], None)
    stored = asyncio.run(reports.get_report(ACTOR, CASE, version))
    assert confirmed["report"] == contract_view(stored)
    assert confirmed["report"]["approval_status"] == "approved"

    res = client.get(f"/cases/{CASE}/reports/{version}/claim")
    assert res.status_code == 200, res.text
    assert res.json() == as_json(asyncio.run(reports.get_claim(ACTOR, CASE, version)))

    agreed = {"agreed_counterparty_fault": 30, "fault_change_reason": "상대 보험사와 협의"}
    res = client.patch(f"/cases/{CASE}/agreed-fault", json=agreed)
    assert res.status_code == 200, res.text
    assert res.json()["versions"]["report_version"] == version + 1

    # 협의 과실로 새 버전이 생겨, 본문 없이 옛 버전을 확인하면 409
    res = client.post(f"/cases/{CASE}/reports/{version}/confirm")
    assert (res.status_code, res.json()["error"]["code"]) == (409, "version_conflict")


def test_report_route_errors(client):
    res = client.get(f"/cases/{CASE}/reports/1")
    assert (res.status_code, res.json()["error"]["code"]) == (404, "not_found")
    res = client.post(f"/cases/{CASE}/estimates",
                      json={"storage_path": f"cases/{CASE}/estimate.pdf", "party_id": "P1"})
    assert (res.status_code, res.json()["error"]["code"]) == (501, "not_implemented")
    res = client.post(f"/cases/{CASE}/estimates", json={"party_id": "P1"})
    assert (res.status_code, res.json()["error"]["code"]) == (422, "invalid_request")
    # 확인 완료 흐름으로 바뀌며 없어진 경로
    for removed in ("approve", "calculation", "claim-export"):
        assert client.post(f"/cases/{CASE}/reports/1/{removed}").status_code == 404
