"""B(통합): 인증·오류 응답·경로 배선 검증. 서비스 함수는 바꿔 끼우고 DB·모델 없이 돈다."""

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api import facts, judgments
from app.api.errors import forbidden
from app.config import Settings, get_settings
from app.main import app, create_app

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
    # FE src/lib/api/client.ts ApiClient 메서드 14개
    assert len([p for p in app.openapi()["paths"] if p != "/health"]) == 13
    ops = sum(len(v) for k, v in app.openapi()["paths"].items() if k != "/health")
    assert ops == 14


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
    assert client.get("/cases/case-1").status_code == 501  # 인증 통과 → A 미구현
    app.dependency_overrides[get_settings] = lambda: settings(
        app_env="local", on_vercel=True, dev_actor_id="dev-1", supabase_jwt_secret=None)
    assert client.get("/cases/case-1").status_code == 503


def test_unimplemented_service_is_501(client):
    res = client.get("/cases/case-1", headers=token())
    assert res.status_code == 501
    assert res.json()["error"]["code"] == "not_implemented"


def test_validation_error_shape(client):
    res = client.post("/cases/case-1/facts/confirm", json={"expected_version": 0},
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
    res = client.get("/cases/case-1/facts", headers=token())
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"


def test_unexpected_error_hides_details(client, monkeypatch):
    async def get_card(actor_id, case_id):
        raise RuntimeError("db password=hunter2")
    monkeypatch.setattr(facts, "get_fact_card", get_card)
    res = client.get("/cases/case-1/facts", headers=token())
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
            return private.public_key()

    url = "https://proj.supabase.co"
    monkeypatch.setattr(deps, "_jwks_client", lambda _url: FakeJwks())
    app.dependency_overrides[get_settings] = lambda: settings(
        supabase_url=url, supabase_jwt_secret=None)

    def es256(iss: str) -> dict[str, str]:
        payload = {"sub": "user-es", "aud": "authenticated", "iss": iss,
                   "exp": int(time.time()) + 600}
        return {"Authorization": "Bearer " + jwt.encode(payload, private, algorithm="ES256")}

    assert client.get("/cases/case-1", headers=es256(f"{url}/auth/v1")).status_code == 501
    assert client.get("/cases/case-1", headers=es256("https://other/auth/v1")).status_code == 401
    # 비밀키 미설정이면 HS256 토큰은 받지 않는다(알고리즘 혼동 방지)
    assert client.get("/cases/case-1", headers=token()).status_code == 401
