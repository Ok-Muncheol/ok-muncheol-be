"""B(통합): 인증·오류 응답·경로 배선 검증. 서비스 함수는 바꿔 끼우고 DB·모델 없이 돈다."""

import json
import re
import time
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api import cases, facts, judgments
from app.api.errors import forbidden
from app.config import Settings, get_settings
from app.main import app, create_app

ROOT = Path(__file__).resolve().parents[2]
SECRET = "test-secret-at-least-32-bytes-long!!"
KEY = {"Idempotency-Key": "req-00000001"}


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "production", "on_vercel": False, "supabase_url": None,
        "supabase_jwt_secret": SECRET, "jwt_audience": "authenticated",
        "cors_allow_origins": (), "dev_actor_id": None,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def token(sub: str = "user-1", **claims: object) -> dict[str, str]:
    payload = {"sub": sub, "aud": "authenticated", "exp": int(time.time()) + 600, **claims}
    return {"Authorization": f"Bearer {jwt.encode(payload, SECRET, algorithm='HS256')}"}


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = lambda: settings()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def card(version: int = 2, status: str = "confirmed") -> dict[str, object]:
    return {
        "case_id": "case-1", "version": version, "accident_date": "2026-10-01", "fields": {},
        "party_mapping": {"video_ego_party_id": None, "insured_party_id": None,
                          "chart_party_A_id": None, "chart_party_B_id": None},
        "evidence_frames": [], "status": status, "confirmed_by": "user-1",
        "confirmed_at": "2026-10-02T00:00:00Z",
    }


def test_health_is_open(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_openapi_matches_fe_api_client():
    # FE ApiClient(src/lib/api/client.ts) 메서드 18개(getEvidence 포함) + listCases(11/13)
    # GET /cases는 POST /cases와 경로가 같아 경로 수는 그대로 17이다
    assert len([p for p in app.openapi()["paths"] if p != "/health"]) == 17
    ops = sum(len(v) for k, v in app.openapi()["paths"].items() if k != "/health")
    assert ops == 19


def test_missing_token_is_401_api_error(client):
    res = client.get("/cases/case-1")
    assert res.status_code == 401
    error = res.json()["error"]
    assert (error["code"], error["retryable"]) == ("unauthorized", False)
    assert set(error) == {"code", "message", "retryable"}


@pytest.mark.parametrize("headers", [
    {"Authorization": "Basic abc"},
    {"Authorization": "Bearer not-a-jwt"},
    token(exp=int(time.time()) - 10),
    token(aud="anon"),
    {"Authorization": "Bearer " + jwt.encode(
        {"sub": "u", "aud": "authenticated", "exp": int(time.time()) + 60},
        "wrong-secret-also-32-bytes-long!!!", algorithm="HS256")},
])
def test_bad_tokens_are_rejected(client, headers):
    res = client.get("/cases/case-1", headers=headers)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"


def test_no_auth_config_fails_closed(client):
    app.dependency_overrides[get_settings] = lambda: settings(supabase_jwt_secret=None)
    res = client.get("/cases/case-1", headers=token())
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "auth_not_configured"


def test_dev_actor_only_in_local_off_vercel(client):
    app.dependency_overrides[get_settings] = lambda: settings(
        app_env="local", dev_actor_id="dev-1", supabase_jwt_secret=None)
    # 인증 통과 → A 사건 조회가 잘못된 사건 번호로 404(DB 전)
    assert client.get("/cases/case-1").status_code == 404
    app.dependency_overrides[get_settings] = lambda: settings(
        app_env="local", on_vercel=True, dev_actor_id="dev-1", supabase_jwt_secret=None)
    assert client.get("/cases/case-1").status_code == 503


def test_unimplemented_service_is_501(client, monkeypatch):
    # 경로 서비스가 모두 구현돼, 서비스 하나를 미구현으로 바꿔 끼워 501 변환을 확인한다
    async def not_yet(*args, **kwargs):
        raise NotImplementedError("미구현 예시")

    monkeypatch.setattr(cases, "get_case", not_yet)
    res = client.get("/cases/case-1", headers=token())
    assert res.status_code == 501
    assert res.json()["error"]["code"] == "not_implemented"


def test_validation_error_shape(client):
    res = client.post("/cases/case-1/fact-card/confirm", json={"expected_version": 0},
                      headers=token())
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_request"


def test_judgment_requires_idempotency_key(client):
    res = client.post("/cases/case-1/judgments", json={"card_version": 2}, headers=token())
    assert res.status_code == 422


def test_judgment_rejects_stale_card_version(client, monkeypatch):
    async def get_card(actor_id, case_id):
        return card(version=3)
    monkeypatch.setattr(facts, "get_fact_card", get_card)
    res = client.post("/cases/case-1/judgments", json={"card_version": 2},
                      headers={**token(), **KEY})
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "version_conflict"


def test_judgment_rejects_unconfirmed_card(client, monkeypatch):
    async def get_card(actor_id, case_id):
        return card(status="draft")
    monkeypatch.setattr(facts, "get_fact_card", get_card)
    res = client.post("/cases/case-1/judgments", json={"card_version": 2},
                      headers={**token(), **KEY})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "card_not_confirmed"


def test_judgment_passes_actor_card_and_key(client, monkeypatch):
    seen = {}

    async def get_card(actor_id, case_id):
        return card()

    async def start(actor_id, c, key):
        seen.update(actor=actor_id, version=c["version"], key=key)
        return {"task_id": "t-1"}

    monkeypatch.setattr(facts, "get_fact_card", get_card)
    monkeypatch.setattr(judgments, "start_judgment", start)
    res = client.post("/cases/case-1/judgments", json={"card_version": 2},
                      headers={**token(sub="user-9"), **KEY})
    assert res.status_code == 202
    assert seen == {"actor": "user-9", "version": 2, "key": KEY["Idempotency-Key"]}


def test_service_api_exception_passes_through(client, monkeypatch):
    async def get_card(actor_id, case_id):
        raise forbidden()
    monkeypatch.setattr(facts, "get_fact_card", get_card)
    res = client.get("/cases/case-1/fact-card", headers=token())
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"


def test_unexpected_error_hides_details(client, monkeypatch):
    async def get_card(actor_id, case_id):
        raise RuntimeError("db password=hunter2")
    monkeypatch.setattr(facts, "get_fact_card", get_card)
    res = client.get("/cases/case-1/fact-card", headers=token())
    assert res.status_code == 500
    assert "hunter2" not in res.text
    assert res.json()["error"] == {"code": "internal_error",
                                   "message": res.json()["error"]["message"], "retryable": True}


def test_cors_only_for_configured_origins(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://fe.example")
    get_settings.cache_clear()
    try:
        c = TestClient(create_app())
        ok = c.options("/health", headers={"Origin": "https://fe.example",
                                           "Access-Control-Request-Method": "GET"})
        bad = c.options("/health", headers={"Origin": "https://evil.example",
                                            "Access-Control-Request-Method": "GET"})
    finally:
        get_settings.cache_clear()
    assert ok.headers.get("access-control-allow-origin") == "https://fe.example"
    assert "access-control-allow-origin" not in bad.headers


def test_asymmetric_jwks_token_checks_issuer(client, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.api import deps

    private = ec.generate_private_key(ec.SECP256R1())

    class FakeJwks:
        def get_signing_key_from_jwt(self, _token):
            return jwt.PyJWK.from_dict(
                {**json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private.public_key())),
                 "kid": "k1", "alg": "ES256"})

    url = "https://proj.supabase.co"
    monkeypatch.setattr(deps, "_jwks_client", lambda _url: FakeJwks())
    app.dependency_overrides[get_settings] = lambda: settings(
        supabase_url=url, supabase_jwt_secret=None)

    def es256(iss: str) -> dict[str, str]:
        payload = {"sub": "user-es", "aud": "authenticated", "iss": iss,
                   "exp": int(time.time()) + 600}
        return {"Authorization": "Bearer " + jwt.encode(payload, private, algorithm="ES256")}

    assert client.get("/cases/case-1", headers=es256(f"{url}/auth/v1")).status_code == 404
    assert client.get("/cases/case-1", headers=es256("https://other/auth/v1")).status_code == 401
    # 비밀키 미설정이면 HS256 토큰은 받지 않는다(알고리즘 혼동 방지)
    assert client.get("/cases/case-1", headers=token()).status_code == 401


@pytest.mark.parametrize(("error", "status", "code"), [
    # 목록에 없는 키 ID: 위조·다른 프로젝트 토큰 → 401
    (jwt.PyJWKClientError("Unable to find a signing key that matches: x"), 401, "unauthorized"),
    # 공개키 목록 조회 실패: 서버 쪽 문제 → 503, 재시도 가능
    (jwt.PyJWKClientConnectionError("Fail to fetch data from the url"), 503, "auth_unavailable"),
])
def test_jwks_lookup_failures(client, monkeypatch, error, status, code):
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.api import deps

    class FailingJwks:
        def get_signing_key_from_jwt(self, _token):
            raise error

    monkeypatch.setattr(deps, "_jwks_client", lambda _url: FailingJwks())
    app.dependency_overrides[get_settings] = lambda: settings(
        supabase_url="https://proj.supabase.co", supabase_jwt_secret=None)
    payload = {"sub": "u", "aud": "authenticated", "exp": int(time.time()) + 60}
    forged = jwt.encode(payload, ec.generate_private_key(ec.SECP256R1()), algorithm="ES256",
                        headers={"kid": "unknown"})
    res = client.get("/cases/case-1", headers={"Authorization": f"Bearer {forged}"})
    assert res.status_code == status
    assert res.json()["error"]["code"] == code
    assert res.json()["error"]["retryable"] is (status == 503)


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _served_routes() -> set[tuple[str, str]]:
    return {(method.upper(), _shape(path)) for path, ops in app.openapi()["paths"].items()
            if path != "/health" for method in ops}


def test_routes_follow_readme_api_table():
    # README 「API 계약 제안」 표가 경로의 기준이다. 표의 경로는 모두 서버에 있어야 한다
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*`(GET|POST|PATCH|PUT|DELETE) (/[^`\s]*)`\s*\|", text, re.M)
    designed = {(method, _shape(path)) for method, path in rows}
    assert designed, "README API 표를 찾지 못했다"
    missing = designed - _served_routes()
    assert not missing, f"README 표에 있는데 서버에 없는 경로: {sorted(missing)}"


def test_contract_doc_lists_exactly_the_served_routes():
    text = (ROOT / "docs/contracts/README.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*([A-Z·]+)\s*\|\s*`(/[^`]*)`\s*\|", text, re.M)
    documented = {(method, _shape(path)) for methods, path in rows for method in methods.split("·")}
    assert documented == _served_routes()


def test_answer_question_id_must_match_path(client):
    body = {"question_id": "q-other", "expected_card_version": 2, "value": "red", "unknown": False}
    res = client.post("/cases/case-1/questions/q-1/answer", json=body, headers=token())
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "question_mismatch"


def test_answer_passes_actor_case_and_body(client, monkeypatch):
    seen = {}

    async def answer(actor_id, case_id, payload):
        seen.update(actor=actor_id, case=case_id, question=payload["question_id"],
                    unknown=payload["unknown"])
        return {"status": "accepted"}

    monkeypatch.setattr(judgments, "answer_question", answer)
    body = {"question_id": "q-1", "expected_card_version": 2, "value": None, "unknown": True}
    res = client.post("/cases/case-1/questions/q-1/answer", json=body, headers=token())
    assert res.status_code == 200
    assert seen == {"actor": "user-1", "case": "case-1", "question": "q-1", "unknown": True}


def test_judgment_read_passes_actor_and_case(client, monkeypatch):
    seen = []
    view = {"task_id": "run-1", "status": "waiting_for_confirmation", "result": None,
            "questions": [{"question_id": "q-1", "answer": None}]}

    async def read(actor_id, case_id):
        seen.append((actor_id, case_id))
        return view if case_id == "case-1" else None

    monkeypatch.setattr(judgments, "get_judgment", read)
    res = client.get("/cases/case-1/judgment", headers=token())
    assert (res.status_code, res.json()) == (200, view)
    # 판단을 시작하지 않은 사건은 404가 아니라 null이다
    res = client.get("/cases/case-2/judgment", headers=token())
    assert (res.status_code, res.json()) == (200, None)
    assert seen == [("user-1", "case-1"), ("user-1", "case-2")]
    assert client.get("/cases/case-1/judgment").status_code == 401


def test_upload_passes_kind_and_content_type(client, monkeypatch):
    seen = {}
    ticket = {"storage_path": "case-files/case-1/videos/clip.mp4",
              "upload_url": "https://example.supabase.co/storage/v1/object/upload/sign/x",
              "expires_in": 7200}

    async def request_upload(actor_id, case_id, kind, content_type):
        seen.update(actor=actor_id, case=case_id, kind=kind, content_type=content_type)
        return ticket

    monkeypatch.setattr(cases, "request_upload", request_upload)
    body = {"kind": "video", "content_type": "video/mp4"}
    res = client.post("/cases/case-1/uploads", json=body, headers=token(sub="user-9"))
    # 응답 모델(UploadTicket)을 거쳐도 서비스 결과 그대로다
    assert (res.status_code, res.json()) == (200, ticket)
    assert seen == {"actor": "user-9", "case": "case-1", "kind": "video",
                    "content_type": "video/mp4"}


@pytest.mark.parametrize("body", [
    {"kind": "photo", "content_type": "image/png"},
    {"content_type": "application/pdf"},
    {"kind": "video"},
    {"kind": "video", "content_type": ""},
    {"kind": "estimate", "content_type": "a" * 101},
])
def test_upload_body_errors_are_invalid_request(client, monkeypatch, body):
    async def request_upload(*args):
        raise AssertionError("본문이 틀리면 서비스를 부르지 않는다")

    monkeypatch.setattr(cases, "request_upload", request_upload)
    res = client.post("/cases/case-1/uploads", json=body, headers=token())
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_request"


def test_every_route_calls_an_existing_service_function():
    """라우트가 부르는 담당 서비스 함수가 실제로 있어야 한다.

    서비스 함수 이름이 바뀌고 라우트가 따라가지 못하면 그 경로만 500이 된다
    (#55 → #69 사이 approve 등).
    """
    import ast

    from app.api import analysis, cases, reports

    modules = {"analysis": analysis, "cases": cases, "facts": facts, "judgments": judgments,
               "reports": reports}
    tree = ast.parse((ROOT / "app/api/routes.py").read_text(encoding="utf-8"))
    called = {
        (node.value.id, node.attr) for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id in modules
    }
    missing = sorted(f"{module}.{name}" for module, name in called
                     if not callable(getattr(modules[module], name, None)))
    assert len(called) >= 15 and not missing, missing


def test_qa_failures_map_to_api_errors_without_internal_details():
    from app.agents import qa as qa_agent

    timeout = judgments._qa_error(qa_agent.QaTimedOut("budget"), "case-1")
    assert (timeout.status_code, timeout.error["code"], timeout.error["retryable"]) \
        == (504, "qa_timeout", True)
    missing = judgments._qa_error(qa_agent.QaNotConfigured("no key"), "case-1")
    assert (missing.status_code, missing.error["code"]) == (503, "dependency_not_ready")
    failed = judgments._qa_error(RuntimeError("secret upstream detail"), "case-1")
    assert (failed.status_code, failed.error["code"], failed.error["retryable"]) \
        == (502, "qa_failed", True)
    assert "secret" not in failed.error["message"]
