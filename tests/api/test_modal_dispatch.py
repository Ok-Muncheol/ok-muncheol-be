"""B 인프라: 영상 분석 Modal 호출(app/modal_dispatch.py)과 Modal 콜백 경로.

네트워크 없이 가짜 Modal로 돈다.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import modal_dispatch
from app.api import analysis
from app.api.errors import ApiException
from app.config import Settings, _public_backend_url
from app.main import app, create_app

KEY, SECRET = "mk-test-only", "ms-test-only"
TASK = "11111111-1111-4111-8111-111111111111"
JOB = {
    "task_id": TASK,
    "case_id": "22222222-2222-4222-8222-222222222222",
    "video_url": "https://example.supabase.co/storage/v1/object/sign/case-files/v.mp4?token=VIDEO-SECRET",
    "callback_path": f"/internal/analysis/{TASK}",
    "callback_token": "CALLBACK-SECRET",
}
ENDPOINT = "https://workspace--ok-muncheol-vision-analyze.modal.run"


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "production", "on_vercel": True, "supabase_url": None,
        "supabase_jwt_secret": None, "jwt_audience": "authenticated", "cors_allow_origins": (),
        "dev_actor_id": None, "modal_analyze_url": ENDPOINT, "modal_proxy_key": KEY,
        "modal_proxy_secret": SECRET, "public_backend_url": "https://ok-muncheol-be.vercel.app",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(modal_dispatch, "get_settings", lambda: settings())


def dispatch(handler) -> str:
    return asyncio.run(modal_dispatch.dispatch_to_modal(
        dict(JOB), transport=httpx.MockTransport(handler)))


def test_dispatch_posts_the_job_with_proxy_auth_and_returns_call_id(configured):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"call_id": "fc-123"})

    assert dispatch(handler) == "fc-123"
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", ENDPOINT)
    assert (request.headers["Modal-Key"], request.headers["Modal-Secret"]) == (KEY, SECRET)
    # Modal에는 서버 키 없이 이것만 간다. 콜백 주소는 BE 공개 주소 + 정지원 callback_path
    assert json.loads(request.content) == {
        "task_id": TASK, "case_id": JOB["case_id"], "video_url": JOB["video_url"],
        "callback_url": f"https://ok-muncheol-be.vercel.app/internal/analysis/{TASK}",
        "callback_token": "CALLBACK-SECRET",
    }


@pytest.mark.parametrize(("response", "retryable"), [
    (httpx.Response(500), True), (httpx.Response(503), True), (httpx.Response(429), True),
    (httpx.Response(401), False), (httpx.Response(404), False),
    (httpx.Response(200, json={}), False), (httpx.Response(200, content=b"not json"), False),
    (httpx.Response(200, json=["fc-1"]), False),
])
def test_dispatch_failures_are_503_and_say_whether_to_retry(configured, response, retryable):
    with pytest.raises(ApiException) as caught:
        dispatch(lambda request: response)
    error = caught.value.error
    assert (caught.value.status_code, error["code"], error["retryable"]) == (
        503, "analysis_dispatch_failed", retryable)


def test_network_error_does_not_leak_the_signed_url_token_or_keys(configured):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {request.content!r} {request.headers}", request=request)

    with pytest.raises(ApiException) as caught:
        dispatch(handler)
    exc = caught.value
    assert exc.error["retryable"] is True
    assert exc.__cause__ is None and exc.__suppress_context__
    for secret in ("CALLBACK-SECRET", "VIDEO-SECRET", KEY, SECRET):
        assert secret not in str(exc) and secret not in repr(exc.error)


@pytest.mark.parametrize(
    "missing", ["modal_analyze_url", "modal_proxy_key", "modal_proxy_secret", "public_backend_url"])
def test_dispatch_without_settings_does_not_call_modal(monkeypatch, missing):
    monkeypatch.setattr(modal_dispatch, "get_settings", lambda: settings(**{missing: None}))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("설정이 없으면 Modal을 부르지 않는다")

    with pytest.raises(ApiException) as caught:
        dispatch(handler)
    assert caught.value.error["code"] == "analysis_not_configured"


def test_public_backend_url_prefers_the_explicit_value_then_the_vercel_domain():
    assert _public_backend_url({"PUBLIC_BACKEND_URL": " https://be.example/ ",
                                "VERCEL_PROJECT_PRODUCTION_URL": "x.vercel.app"}) == "https://be.example"
    assert _public_backend_url({"VERCEL_PROJECT_PRODUCTION_URL": "ok-muncheol-be.vercel.app"}) \
        == "https://ok-muncheol-be.vercel.app"
    assert _public_backend_url({}) is None


def test_settings_repr_hides_modal_secrets():
    text = repr(settings())
    assert KEY not in text and SECRET not in text


def test_create_app_plugs_dispatch_only_when_modal_is_configured(monkeypatch):
    original = analysis.services.dispatch
    try:
        analysis.configure(dispatch=None)
        monkeypatch.setattr("app.main.get_settings", lambda: settings(modal_proxy_secret=None))
        create_app()
        assert analysis.services.dispatch is None
        monkeypatch.setattr("app.main.get_settings", lambda: settings())
        create_app()
        assert analysis.services.dispatch is modal_dispatch.dispatch_to_modal
    finally:
        analysis.configure(dispatch=original)


# ── Modal 콜백 경로 ───────────────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_callback_passes_the_bearer_token_and_body_to_the_service(client, monkeypatch):
    seen = []

    async def receive(task_id, token, body):
        seen.append((task_id, token, body))
        return {"task_id": task_id, "status": "failed"}

    monkeypatch.setattr(analysis, "receive_result", receive)
    body = {"status": "failed",
            "error": {"stage": "detect", "code": "x", "message": "m", "retryable": True}}
    res = client.post(f"/internal/analysis/{TASK}", json=body,
                      headers={"Authorization": "Bearer tok-1"})
    assert (res.status_code, res.json()) == (200, {"task_id": TASK, "status": "failed"})
    # 토큰이 없거나 Bearer가 아니면 None을 넘긴다 — 401은 서비스(토큰 해시 대조)가 정한다
    client.post(f"/internal/analysis/{TASK}", json=body)
    client.post(f"/internal/analysis/{TASK}", json=body, headers={"Authorization": "Basic abc"})
    assert seen == [(TASK, "tok-1", body), (TASK, None, body), (TASK, None, body)]


def test_callback_rejects_large_or_non_object_bodies_before_the_service(client, monkeypatch):
    async def receive(*args):
        raise AssertionError("서비스까지 가지 않는다")

    monkeypatch.setattr(analysis, "receive_result", receive)
    big = b'{"status": "completed", "pad": "' + b"x" * (3 * 1024 * 1024) + b'"}'
    res = client.post(f"/internal/analysis/{TASK}", content=big,
                      headers={"Content-Type": "application/json", "Authorization": "Bearer t"})
    assert (res.status_code, res.json()["error"]["code"]) == (413, "callback_too_large")
    for raw in (b"not json", b"[1, 2]", b'"text"'):
        res = client.post(f"/internal/analysis/{TASK}", content=raw,
                          headers={"Content-Type": "application/json"})
        assert (res.status_code, res.json()["error"]["code"]) == (422, "invalid_callback")


def test_callback_is_not_part_of_the_public_api():
    assert not any(path.startswith("/internal") for path in app.openapi()["paths"])
