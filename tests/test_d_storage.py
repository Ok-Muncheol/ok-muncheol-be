"""D: 견적서 Storage 내려받기 — app/storage.py와 reports.upload_estimate_from_storage.

네트워크 없이 가짜 Storage(httpx.MockTransport, 또는 download_object 바꿔치기)로 확인한다.
- 권한(404·403)·경로 확인이 내려받기보다 먼저(가짜 Storage가 불리지 않음)
- 다른 사건·'..' 경로 거절, 버킷 접두는 STORAGE_BUCKET 상수
- 키 없음 503, 파일 없음 404, 크기 초과·PDF 아님·PDF 파싱 오류 422, 코드 버그는 감싸지 않음
- 임시 파일은 성공·실패와 상관없이 지움, 저장 경로는 Storage 경로
- 키 값은 오류 메시지·예외 연결에 나오지 않음
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import storage
from app.api import reports as api
from app.api.errors import ApiException
from app.calculation.rules import RuleSet
from app.config import Settings
from tests import fixtures_d as fx

ACTOR, OTHER, CASE = "handler-1", "handler-2", "synthetic-case"
FAKE_KEY = "sb_secret_test-only-not-a-real-key"
SUPABASE_URL = "https://example.supabase.co"
SAMPLE_10M = Path(__file__).parent / "fixtures" / "estimate_sample_10m.pdf"
GOOD_PATH = f"{storage.STORAGE_BUCKET}/{CASE}/estimates/estimate 1.pdf"


def run(coro):
    return asyncio.run(coro)


def settings(*, url: str | None = SUPABASE_URL, key: str | None = FAKE_KEY) -> Settings:
    return Settings(
        app_env="local", on_vercel=False, supabase_url=url, supabase_jwt_secret=None,
        jwt_audience="authenticated", cors_allow_origins=(), dev_actor_id=None,
        supabase_secret_key=key,
    )


def api_error(exc_info: pytest.ExceptionInfo[ApiException]) -> tuple[int, str]:
    return exc_info.value.status_code, exc_info.value.error["code"]


def assert_no_key(exc: BaseException) -> None:
    """키 값이 메시지·예외 연결 어디에도 없다."""
    seen: BaseException | None = exc
    while seen is not None:
        assert FAKE_KEY not in str(seen) and FAKE_KEY not in repr(seen)
        seen = seen.__cause__ or (None if seen.__suppress_context__ else seen.__context__)


# ---------------------------------------------------------------- app/storage.py


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(storage, "get_settings", lambda: settings())


def download(tmp_path: Path, handler, *, max_bytes: int = 1024) -> tuple[int, Path]:
    dest = tmp_path / "out.pdf"
    size = run(storage.download_object(
        f"{CASE}/estimates/estimate 1.pdf", dest, max_bytes=max_bytes,
        transport=httpx.MockTransport(handler),
    ))
    return size, dest


def test_download_sends_only_apikey_header_to_object_url(configured, tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"%PDF-1.4 body")

    size, dest = download(tmp_path, handler)
    assert (size, dest.read_bytes()) == (13, b"%PDF-1.4 body")
    (request,) = seen
    assert request.method == "GET"
    # 객체 키는 버킷을 뗀 나머지, 조각마다 URL 인코딩
    assert str(request.url) == (
        f"{SUPABASE_URL}/storage/v1/object/{storage.STORAGE_BUCKET}/"
        f"{CASE}/estimates/estimate%201.pdf"
    )
    assert request.headers["apikey"] == FAKE_KEY
    assert "authorization" not in request.headers


@pytest.mark.parametrize("missing", ["url", "key"])
def test_download_without_settings_is_503(monkeypatch, tmp_path, missing):
    kw = {"url": None} if missing == "url" else {"key": None}
    monkeypatch.setattr(storage, "get_settings", lambda: settings(**kw))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("설정이 없으면 Storage를 부르지 않는다")

    with pytest.raises(ApiException) as caught:
        download(tmp_path, handler)
    assert api_error(caught) == (503, "storage_not_configured")
    assert not (tmp_path / "out.pdf").exists()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"statusCode": "404", "error": "not_found"}),
        # 옛 Supabase Storage는 없는 파일을 400 + 본문 statusCode "404"로 준다
        httpx.Response(400, json={"statusCode": "404", "error": "not_found",
                                  "message": "Object not found"}),
    ],
)
def test_missing_object_is_not_found(configured, tmp_path, response):
    with pytest.raises(storage.StorageObjectNotFound):
        download(tmp_path, lambda request: response)


@pytest.mark.parametrize(
    ("status", "body", "retryable"),
    [
        (400, json.dumps({"statusCode": "403", "error": "InvalidKey"}), False),
        (400, "not json", False),
        (401, "", False),
        (403, "", False),
        (500, "", True),
        (503, "", True),
    ],
)
def test_storage_errors_are_503_unavailable(configured, tmp_path, status, body, retryable):
    with pytest.raises(ApiException) as caught:
        download(tmp_path, lambda request: httpx.Response(status, content=body.encode()))
    assert api_error(caught) == (503, "storage_unavailable")
    assert caught.value.error["retryable"] is retryable
    assert_no_key(caught.value)


def test_network_error_is_503_without_key_or_request_in_cause(configured, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"연결 실패 {request.headers['apikey']}", request=request)

    with pytest.raises(ApiException) as caught:
        download(tmp_path, handler)
    assert api_error(caught) == (503, "storage_unavailable")
    assert caught.value.error["retryable"] is True
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert_no_key(caught.value)


def test_too_large_by_content_length(configured, tmp_path):
    with pytest.raises(storage.StorageObjectTooLarge):
        download(tmp_path, lambda request: httpx.Response(200, content=b"x" * 11), max_bytes=10)


def test_too_large_while_streaming_without_content_length(configured, tmp_path):
    sent: list[int] = []

    async def chunks():
        for _ in range(100):
            sent.append(1)
            yield b"x" * 4

    with pytest.raises(storage.StorageObjectTooLarge):
        download(tmp_path, lambda request: httpx.Response(200, content=chunks()), max_bytes=10)
    assert len(sent) < 100  # 한도를 넘는 순간 멈춘다


def test_settings_repr_hides_secret_key():
    assert FAKE_KEY not in repr(settings())


# ------------------------------------------------- reports.upload_estimate_from_storage


class FakeStorage:
    """download_object 대신: 받은 객체 키·임시 파일 경로를 기록하고 정해진 바이트를 쓴다."""

    def __init__(self, data: bytes = b"", error: Exception | None = None):
        self.data, self.error = data, error
        self.calls: list[tuple[str, Path]] = []

    async def __call__(self, object_key: str, dest: Path, *, max_bytes: int) -> int:
        self.calls.append((object_key, dest))
        assert dest.exists()  # 임시 파일은 내려받기 전에 만들어 둔다
        if self.error is not None:
            raise self.error
        dest.write_bytes(self.data)
        return len(self.data)

    def temp_files_removed(self) -> bool:
        return bool(self.calls) and not any(dest.exists() for _, dest in self.calls)


@pytest.fixture
def service():
    api.configure(repo=api.InMemoryReportRepository(),
                  rules=RuleSet(version="reviewed-test"), fetch_clauses=None)
    run(api.submit_judgment(ACTOR, fx.card(), fx.judgment()))
    yield
    api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)


def use_storage(monkeypatch, fake: FakeStorage) -> FakeStorage:
    monkeypatch.setattr(storage, "download_object", fake)
    return fake


def upload(path: str = GOOD_PATH, actor: str = ACTOR, case: str = CASE):
    return run(api.upload_estimate_from_storage(actor, case, path, "P1"))


def test_success_stores_storage_path_not_temp_file(service, monkeypatch):
    fake = use_storage(monkeypatch, FakeStorage(SAMPLE_10M.read_bytes()))
    report = upload()
    assert report is not None and report["versions"]["report_version"] == 2
    assert fake.calls[0][0] == f"{CASE}/estimates/estimate 1.pdf"  # 버킷을 뗀 객체 키
    docs = fx.record(ACTOR, CASE).documents
    estimate = next(d for d in docs if d["kind"] == "repair_estimate")
    evidence = next(d for d in docs if d["kind"] == "evidence")
    assert estimate["amount"] == 10_000_000
    assert (estimate["storage_path"], evidence["storage_path"]) == (GOOD_PATH, GOOD_PATH)
    assert fake.temp_files_removed()


@pytest.mark.parametrize(
    ("actor", "case", "expected"),
    [(ACTOR, "없는-사건", (404, "not_found")), (OTHER, CASE, (403, "forbidden"))],
)
def test_access_is_checked_before_path_and_download(service, monkeypatch, actor, case, expected):
    fake = use_storage(monkeypatch, FakeStorage(SAMPLE_10M.read_bytes()))
    # 다른 사건 경로여도 권한 오류가 먼저
    with pytest.raises(ApiException) as caught:
        upload(f"{storage.STORAGE_BUCKET}/{case}/estimates/x.pdf", actor=actor, case=case)
    assert api_error(caught) == expected
    assert fake.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "case-files/other-case/estimates/x.pdf",  # 다른 사건
        # 비ASCII: Supabase Storage가 400 InvalidKey로 거절하므로 미리 422
        f"case-files/{CASE}/estimates/견적서.pdf",
        f"case-files/{CASE}/estimates/estimate\u00a0.pdf",  # 줄바꿈 없는 공백
        f"case-files/{CASE}/estimates/ｅstimate.pdf",  # 전각 문자
        f"case-files/{CASE}/estimates/é.pdf",
        f"case-files/{CASE}/estimates/../../other-case/estimates/x.pdf",
        f"case-files/{CASE}/estimates/..",
        f"case-files/{CASE}/estimates/a..pdf",  # '..'는 어디에도 두지 않는다
        f"case-files/{CASE}/estimates/%2e%2e/x.pdf",  # 인코딩 우회
        f"case-files/{CASE}/estimates/sub\\x.pdf",
        f"case-files/{CASE}/estimates/",
        f"case-files/{CASE}/estimates//x.pdf",
        f"case-files/{CASE}/estimates/./x.pdf",
        f"case-files/{CASE}/estimates/x.pdf\n",
        f"case-files/{CASE}/documents/x.pdf",
        f"case-files/{CASE}/estimatesX/x.pdf",
        f"case-files/{CASE}-2/estimates/x.pdf",
        f"/case-files/{CASE}/estimates/x.pdf",
        f"{CASE}/estimates/x.pdf",  # 버킷 없음
        f"other-bucket/{CASE}/estimates/x.pdf",
    ],
)
def test_path_outside_case_estimates_is_rejected_before_download(service, monkeypatch, path):
    fake = use_storage(monkeypatch, FakeStorage(SAMPLE_10M.read_bytes()))
    with pytest.raises(ApiException) as caught:
        upload(path)
    assert api_error(caught) == (422, "invalid_storage_path")
    assert fake.calls == []


def test_bucket_prefix_follows_constant(service, monkeypatch):
    monkeypatch.setattr(storage, "STORAGE_BUCKET", "renamed-bucket")
    fake = use_storage(monkeypatch, FakeStorage(SAMPLE_10M.read_bytes()))
    with pytest.raises(ApiException) as caught:
        upload(f"case-files/{CASE}/estimates/x.pdf")
    assert api_error(caught) == (422, "invalid_storage_path")
    upload(f"renamed-bucket/{CASE}/estimates/x.pdf")
    assert fake.calls[0][0] == f"{CASE}/estimates/x.pdf"


def test_missing_key_is_503_and_removes_temp_file(service, monkeypatch):
    monkeypatch.setattr(storage, "get_settings", lambda: settings(key=None))
    created: list[str] = []
    real_mkstemp = api.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(name)
        return fd, name

    monkeypatch.setattr(api.tempfile, "mkstemp", spy)
    with pytest.raises(ApiException) as caught:
        upload()
    assert api_error(caught) == (503, "storage_not_configured")
    assert_no_key(caught.value)
    assert len(created) == 1 and not Path(created[0]).exists()


@pytest.mark.parametrize(
    ("fake", "expected"),
    [
        (FakeStorage(error=storage.StorageObjectNotFound("없음")),
         (404, "estimate_file_not_found")),
        (FakeStorage(error=storage.StorageObjectTooLarge("큼")),
         (422, "estimate_too_large")),
        (FakeStorage(b"hello, not a pdf"), (422, "estimate_not_pdf")),
        (FakeStorage(b"x" * 1024 + b"%PDF-1.4"), (422, "estimate_not_pdf")),  # 앞 1024바이트 밖
        (FakeStorage(b""), (422, "estimate_not_pdf")),
        # 머리는 있지만 PDF로 읽을 수 없음(pdfplumber·pdfminer 파싱 오류)
        (FakeStorage(b"%PDF-1.4\ngarbage"), (422, "estimate_not_pdf")),
    ],
)
def test_download_and_pdf_failures_remove_temp_file(service, monkeypatch, fake, expected):
    use_storage(monkeypatch, fake)
    with pytest.raises(ApiException) as caught:
        upload()
    assert api_error(caught) == expected
    assert fake.temp_files_removed()
    assert [d["kind"] for d in fx.record(ACTOR, CASE).documents] == []  # 아무것도 저장하지 않음


def test_pdf_header_within_first_1024_bytes_is_accepted(service, monkeypatch):
    fake = use_storage(monkeypatch, FakeStorage(b"\0" * 1000 + SAMPLE_10M.read_bytes()))
    upload()
    estimate = next(d for d in fx.record(ACTOR, CASE).documents if d["kind"] == "repair_estimate")
    assert estimate["amount"] == 10_000_000
    assert fake.temp_files_removed()


def test_code_bug_while_reading_pdf_is_not_wrapped(service, monkeypatch):
    fake = use_storage(monkeypatch, FakeStorage(SAMPLE_10M.read_bytes()))

    def broken(*args, **kwargs):
        raise KeyError("코드 버그")

    monkeypatch.setattr(api, "read_estimate_pdf", broken)
    with pytest.raises(KeyError):
        upload()
    assert fake.temp_files_removed()
