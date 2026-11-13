"""A: 업로드 서명 URL — app/api/cases.request_upload와 app/storage.create_signed_upload_url.

네트워크·DB 없이 가짜 연결(cases.configure)과 가짜 Storage(httpx.MockTransport)로 확인한다.
- 권한(404·403)이 서명 URL 발급보다 먼저(가짜 발급 함수가 불리지 않음)
- 대인 담당자는 대물 담당자와 권한이 같다
- kind·content_type 확인(422 invalid_upload_type)
- 경로는 case-files/{case}/videos|estimates|medical/{uuid}.{ext}
- 서명 URL = SUPABASE_URL/storage/v1 + 응답 url, 키는 apikey 헤더로만, 오류 메시지에 키 없음
"""

import asyncio
import re
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from app import storage
from app.api import cases
from app.api.errors import ApiException
from app.config import Settings

ACTOR, OTHER = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
CASE = "33333333-3333-3333-3333-333333333333"
INJURY = "55555555-5555-5555-5555-555555555555"  # 대인 담당자
FAKE_KEY = "sb_secret_test-only-not-a-real-key"
SUPABASE_URL = "https://example.supabase.co"


def run(coro):
    return asyncio.run(coro)


class _Cursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, assignees: dict[str, tuple[str, str | None]]):
        self.assignees = assignees  # 사건 → (대물 담당자, 대인 담당자)

    async def execute(self, sql, params=()):
        assignee, injury = self.assignees.get(params[0], (None, None))
        return _Cursor(
            {"assignee_id": assignee, "injury_assignee_id": injury} if assignee else None
        )


@pytest.fixture
def fake(monkeypatch):
    """대물 담당자 ACTOR·대인 담당자 INJURY의 사건 CASE 하나. 서명 발급 호출은 signed에 기록."""
    signed: list[str] = []

    @asynccontextmanager
    async def connect():
        yield _Conn({CASE: (ACTOR, INJURY)})

    async def sign(object_key: str) -> str:
        signed.append(object_key)
        return f"{SUPABASE_URL}/storage/v1/object/upload/sign/case-files/{object_key}?token=t"

    old = cases.services
    cases.configure(connect=connect, sign_upload=sign)
    yield signed
    cases.services = old


def error(exc_info) -> tuple[int, str]:
    return exc_info.value.status_code, exc_info.value.error["code"]


def test_video_upload_path_and_response(fake):
    out = run(cases.request_upload(ACTOR, CASE, "video", "video/mp4"))
    assert re.fullmatch(rf"case-files/{CASE}/videos/[0-9a-f-]{{36}}\.mp4", out["storage_path"])
    uuid.UUID(out["storage_path"].rsplit("/", 1)[1].removesuffix(".mp4"))
    assert fake == [out["storage_path"].removeprefix("case-files/")]
    assert out["upload_url"].startswith(f"{SUPABASE_URL}/storage/v1/")
    assert out["expires_in"] == 7200


def test_estimate_upload_is_pdf_in_estimates_folder(fake):
    out = run(cases.request_upload(ACTOR, CASE, "estimate", "application/pdf"))
    assert re.fullmatch(rf"case-files/{CASE}/estimates/[0-9a-f-]{{36}}\.pdf", out["storage_path"])


@pytest.mark.parametrize(
    ("content_type", "ext"), [("application/pdf", "pdf"), ("image/jpeg", "jpg")]
)
def test_medical_document_is_pdf_or_jpeg_in_medical_folder(fake, content_type, ext):
    out = run(cases.request_upload(ACTOR, CASE, "medical_document", content_type))
    assert re.fullmatch(rf"case-files/{CASE}/medical/[0-9a-f-]{{36}}\.{ext}", out["storage_path"])


def test_injury_assignee_can_upload(fake):
    """대인 담당자(cases.injury_assignee_id)도 담당 사건에 파일을 올린다."""
    out = run(cases.request_upload(INJURY, CASE, "medical_document", "application/pdf"))
    assert out["storage_path"].startswith(f"case-files/{CASE}/medical/")
    assert fake == [out["storage_path"].removeprefix("case-files/")]


def test_each_request_gets_new_file_name(fake):
    a = run(cases.request_upload(ACTOR, CASE, "video", "video/mp4"))
    b = run(cases.request_upload(ACTOR, CASE, "video", "video/mp4"))
    assert a["storage_path"] != b["storage_path"]


@pytest.mark.parametrize(
    ("actor", "case", "status", "code"),
    [(OTHER, CASE, 403, "forbidden"), (ACTOR, str(uuid.uuid4()), 404, "not_found"),
     (ACTOR, "not-a-uuid", 404, "not_found")],
)
def test_permission_checked_before_signing(fake, actor, case, status, code):
    with pytest.raises(ApiException) as exc_info:
        run(cases.request_upload(actor, case, "video", "video/mp4"))
    assert error(exc_info) == (status, code)
    assert fake == []


@pytest.mark.parametrize(
    ("kind", "content_type"),
    [("video", "application/pdf"), ("estimate", "video/mp4"), ("video", "text/html"),
     ("photo", "image/png"), (None, "video/mp4"), ("video", None),
     ("medical_document", "image/png"), ("medical_document", "video/mp4"),
     ("estimate", "image/jpeg")],
)
def test_rejects_wrong_kind_or_type(fake, kind, content_type):
    with pytest.raises(ApiException) as exc_info:
        run(cases.request_upload(ACTOR, CASE, kind, content_type))
    assert error(exc_info) == (422, "invalid_upload_type")
    assert fake == []


def test_content_type_parameters_and_case_ignored(fake):
    out = run(cases.request_upload(ACTOR, CASE, "video", "Video/QuickTime; codecs=x"))
    assert out["storage_path"].endswith(".mov")


# ---------------------------------------------------------------- app/storage.py


def settings(*, url: str | None = SUPABASE_URL, key: str | None = FAKE_KEY) -> Settings:
    return Settings(
        app_env="local", on_vercel=False, supabase_url=url, supabase_jwt_secret=None,
        jwt_audience="authenticated", cors_allow_origins=(), dev_actor_id=None,
        supabase_secret_key=key,
    )


def sign(handler, monkeypatch, **kw) -> str:
    monkeypatch.setattr(storage, "get_settings", lambda: settings(**kw))
    return run(storage.create_signed_upload_url(
        f"{CASE}/videos/a.mp4", transport=httpx.MockTransport(handler)))


def test_sign_posts_with_apikey_and_builds_full_url(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"url": "/object/upload/sign/case-files/x?token=abc",
                                         "token": "abc"})

    url = sign(handler, monkeypatch)
    assert url == f"{SUPABASE_URL}/storage/v1/object/upload/sign/case-files/x?token=abc"
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == (f"{SUPABASE_URL}/storage/v1/object/upload/sign/case-files/"
                            f"{CASE}/videos/a.mp4")
    assert req.headers["apikey"] == FAKE_KEY
    assert "authorization" not in req.headers


def test_sign_not_configured_is_503(monkeypatch):
    with pytest.raises(ApiException) as exc_info:
        sign(lambda r: httpx.Response(200, json={"url": "/x"}), monkeypatch, key=None)
    assert error(exc_info) == (503, "storage_not_configured")


@pytest.mark.parametrize(
    ("response", "retryable"),
    [(httpx.Response(500, text="boom"), True), (httpx.Response(400, text="bad"), False),
     (httpx.Response(200, text="not json"), False), (httpx.Response(200, json={"x": 1}), False)],
)
def test_sign_errors_are_storage_unavailable_without_key(monkeypatch, response, retryable):
    with pytest.raises(ApiException) as exc_info:
        sign(lambda r: response, monkeypatch)
    assert error(exc_info) == (503, "storage_unavailable")
    assert exc_info.value.error["retryable"] is retryable
    assert FAKE_KEY not in str(exc_info.value) and exc_info.value.__cause__ is None


def test_sign_network_error_is_retryable(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(ApiException) as exc_info:
        sign(handler, monkeypatch)
    assert error(exc_info) == (503, "storage_unavailable")
    assert exc_info.value.error["retryable"] is True
