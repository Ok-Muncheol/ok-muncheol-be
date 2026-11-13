"""B 인프라: Modal 영상 분석 워커 본문(workers/modal_worker.py).

Modal·GPU·ffmpeg·cv2 없이 가짜로 돈다.
"""

import json
import logging
import subprocess
from pathlib import Path

import httpx
import pytest

from workers import modal_worker as worker

CASE = "22222222-2222-4222-8222-222222222222"
TASK = "11111111-1111-4111-8111-111111111111"
SIGNED = "https://example.supabase.co/storage/v1/object"
VIDEO_URL = f"{SIGNED}/sign/case-files/{CASE}/videos/v.mp4?token=VIDEO-SECRET"
PREVIEW_URL = f"{SIGNED}/upload/sign/case-files/{CASE}/previews/{TASK}.mp4?token=PREVIEW-SECRET"
CALLBACK = f"https://ok-muncheol-be.vercel.app/internal/analysis/{TASK}"


def slot(n: int) -> dict:
    return {"storage_path": f"case-files/{CASE}/frames/{TASK}/{n:02d}.jpg",
            "upload_url": f"{SIGNED}/upload/sign/frames/{n:02d}.jpg?token=FRAME-SECRET-{n}"}


def job(**overrides: object) -> dict:
    base = {"task_id": TASK, "case_id": CASE, "video_url": VIDEO_URL, "callback_url": CALLBACK,
            "callback_token": "CALLBACK-SECRET", "preview_upload_url": PREVIEW_URL,
            "frame_uploads": [slot(1), slot(2)]}
    base.update(overrides)
    return base


def card() -> dict:
    return {"case_id": CASE, "version": 1, "fields": {},
            "evidence_frames": [{"frame_id": "f071", "timestamp_ms": 4733, "storage_path": None},
                                {"frame_id": "f080", "timestamp_ms": 5333, "storage_path": None},
                                {"frame_id": "f090", "timestamp_ms": 6000, "storage_path": None}]}


class FakeStorage:
    """서명 URL·콜백을 흉내 낸다. 받은 요청을 순서대로 남긴다."""

    def __init__(self, callback_statuses=(200,), fail_put: str | None = None):
        self.requests: list[httpx.Request] = []
        self.callback_statuses = list(callback_statuses)
        self.fail_put = fail_put

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.method == "GET":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42 fake video")
        if request.method == "PUT":
            if self.fail_put and self.fail_put in url:
                return httpx.Response(500)
            return httpx.Response(200, json={"Key": "ok"})
        status = self.callback_statuses.pop(0) if self.callback_statuses else 200
        return httpx.Response(status, json={"status": "ok"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def puts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "PUT"]

    def callbacks(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


def fake_runner(command, **kwargs):
    Path(command[-1]).write_bytes(b"h264 preview")
    return subprocess.CompletedProcess(command, 0)


def fake_grab(video: Path, timestamp_ms: int) -> bytes:
    return b"\xff\xd8\xff jpeg " + str(timestamp_ms).encode()


def run(storage: FakeStorage, analyze=lambda case_id, path: card(), **kw) -> dict:
    sleeps: list[float] = []
    result = worker.run_job(job(**kw), analyze=analyze, grab=fake_grab, runner=fake_runner,
                            client=storage.client(), sleep=sleeps.append)
    result["sleeps"] = sleeps
    return result


# ── job 모양 ─────────────────────────────────────────────────────


def test_validate_job_keeps_only_the_contract_fields():
    clean = worker.validate_job({**job(), "extra": "x"})
    assert set(clean) == {"task_id", "case_id", "video_url", "callback_url", "callback_token",
                          "preview_upload_url", "frame_uploads"}
    minimal = worker.validate_job({k: job()[k] for k in worker._REQUIRED})
    assert (minimal["preview_upload_url"], minimal["frame_uploads"]) == (None, [])


@pytest.mark.parametrize("bad", [
    None, [], {**job(), "task_id": ""}, {**job(), "callback_token": None},
    {**job(), "video_url": "http://plain.example/v.mp4"},
    {**job(), "preview_upload_url": 3},
    {**job(), "frame_uploads": [slot(n) for n in range(13)]},
    {**job(), "frame_uploads": [{"storage_path": "case-files/x/../y.jpg", "upload_url": "https://a"}]},
    {**job(), "frame_uploads": [{"storage_path": slot(1)["storage_path"]}]},
])
def test_validate_job_rejects_bad_shapes(bad):
    with pytest.raises(worker.JobInvalid):
        worker.validate_job(bad)


# ── 처리 순서 ────────────────────────────────────────────────────


def test_success_uploads_preview_and_frames_then_calls_back_with_the_card():
    storage = FakeStorage()
    result = run(storage)
    assert (result["status"], result["callback_status"], result["preview"]) == (
        "completed", 200, True)
    puts = {str(r.url).split("?")[0]: (r.headers["Content-Type"], r.content)
            for r in storage.puts()}
    assert puts[PREVIEW_URL.split("?")[0]] == ("video/mp4", b"h264 preview")
    assert puts[slot(1)["upload_url"].split("?")[0]] == ("image/jpeg", fake_grab(Path(), 4733))
    assert puts[slot(2)["upload_url"].split("?")[0]][1] == fake_grab(Path(), 5333)
    (callback,) = storage.callbacks()
    assert str(callback.url) == CALLBACK
    assert callback.headers["Authorization"] == "Bearer CALLBACK-SECRET"
    body = json.loads(callback.content)
    assert body["status"] == "completed"
    # 칸이 있는 프레임만 storage_path를 채우고, 칸이 모자라면 null이다
    assert [f["storage_path"] for f in body["card"]["evidence_frames"]] == [
        slot(1)["storage_path"], slot(2)["storage_path"], None]


def test_preview_failure_does_not_stop_the_analysis():
    def broken_runner(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    storage = FakeStorage()
    result = worker.run_job(job(), analyze=lambda c, p: card(), grab=fake_grab,
                            runner=broken_runner, client=storage.client(), sleep=lambda s: None)
    assert (result["status"], result["preview"]) == ("completed", False)
    assert not any("previews" in str(r.url) for r in storage.puts())


def test_frame_upload_failure_leaves_that_frame_null():
    storage = FakeStorage(fail_put="frames/02.jpg")
    run(storage)
    body = json.loads(storage.callbacks()[0].content)
    assert [f["storage_path"] for f in body["card"]["evidence_frames"]][:2] == [
        slot(1)["storage_path"], None]


def test_without_optional_slots_only_the_card_is_sent():
    storage = FakeStorage()
    run(storage, preview_upload_url=None, frame_uploads=[])
    assert storage.puts() == []
    body = json.loads(storage.callbacks()[0].content)
    assert all(f["storage_path"] is None for f in body["card"]["evidence_frames"])


class VisionPipelineError(Exception):  # 정지원 파이프라인 오류와 같은 모양
    def __init__(self, stage, code, message, retryable=False):
        super().__init__(message)
        self.stage, self.code, self.retryable = stage, code, retryable


def test_pipeline_error_is_reported_as_is():
    def failing(case_id, path):
        raise VisionPipelineError("vlm", "vlm_failed", "VLM 호출 실패", True)

    storage = FakeStorage()
    result = run(storage, analyze=failing)
    body = json.loads(storage.callbacks()[0].content)
    assert result["status"] == "failed"
    assert body == {"status": "failed", "error": {
        "stage": "vlm", "code": "vlm_failed", "message": "VLM 호출 실패", "retryable": True}}
    assert not any("frames" in str(r.url) for r in storage.puts())


def test_unexpected_error_is_internal_error_without_details():
    def failing(case_id, path):
        raise KeyError("secret detail")

    storage = FakeStorage()
    run(storage, analyze=failing)
    error = json.loads(storage.callbacks()[0].content)["error"]
    assert (error["code"], error["retryable"], error["stage"]) == ("internal_error", True, "worker")
    assert "secret detail" not in error["message"]


def test_missing_module_is_named_in_the_log_but_not_in_the_callback(caplog):
    def failing(case_id, path):
        raise ModuleNotFoundError("No module named 'app'", name="app")

    caplog.set_level(logging.INFO, logger=worker.__name__)
    storage = FakeStorage()
    run(storage, analyze=failing)
    assert "ModuleNotFoundError (app)" in caplog.text
    error = json.loads(storage.callbacks()[0].content)["error"]
    assert (error["code"], error["stage"]) == ("internal_error", "worker")


def test_worker_imports_only_repo_modules_the_modal_image_ships():
    """Modal 워커 이미지에는 저장소의 workers/와 app/schemas/만 들어간다(infra/modal_app.py).

    workers/가 그 밖의 저장소 모듈(app.api·evals 등)을 import하면 로컬 테스트는 지나가도
    운영에서 ModuleNotFoundError가 난다(11/13 첫 실행).
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    repo_packages = {"app", "evals", "scripts", "tests", "infra"}
    outside = []
    for path in sorted((root / "workers").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                shipped = name == "app.schemas" or name.startswith("app.schemas.")
                if name.split(".")[0] in repo_packages and not shipped:
                    outside.append(f"{path.relative_to(root)}: {name}")
    assert outside == []
    image = (root / "infra/modal_app.py").read_text(encoding="utf-8")
    assert 'REPO / "app/schemas"' in image and 'REPO / "app/__init__.py"' in image


def test_video_download_failure_calls_back_retryable():
    class NoVideo(FakeStorage):
        def __call__(self, request):
            if request.method == "GET":
                self.requests.append(request)
                return httpx.Response(403)
            return super().__call__(request)

    storage = NoVideo()
    result = run(storage, analyze=lambda c, p: pytest.fail("영상이 없으면 분석하지 않는다"))
    error = json.loads(storage.callbacks()[0].content)["error"]
    assert result["status"] == "failed"
    assert (error["stage"], error["code"], error["retryable"]) == (
        "download", "video_unavailable", True)


def test_video_size_limit_stops_the_download(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "VIDEO_MAX_BYTES", 10)
    with pytest.raises(ValueError):
        worker.download_video(VIDEO_URL, tmp_path / "v.mp4", FakeStorage().client(), max_bytes=10)


# ── 콜백 재시도 ──────────────────────────────────────────────────


def test_callback_retries_on_server_errors_then_stops():
    storage = FakeStorage(callback_statuses=(503, 500, 200))
    result = run(storage)
    assert (result["callback_status"], len(storage.callbacks())) == (200, 3)
    assert result["sleeps"] == [2, 5]


@pytest.mark.parametrize("status", [401, 404, 409, 422])
def test_callback_does_not_retry_client_errors(status):
    storage = FakeStorage(callback_statuses=(status,))
    result = run(storage)
    assert (result["callback_status"], len(storage.callbacks()), result["sleeps"]) == (
        status, 1, [])


def test_callback_gives_up_after_four_attempts():
    storage = FakeStorage(callback_statuses=(500, 500, 500, 500, 200))
    result = run(storage)
    assert (result["callback_status"], len(storage.callbacks())) == (500, 4)


# ── 비밀 ─────────────────────────────────────────────────────────


def test_logs_never_contain_signed_urls_or_the_callback_token(caplog):
    caplog.set_level(logging.INFO, logger=worker.__name__)
    storage = FakeStorage(fail_put="frames/01.jpg", callback_statuses=(500, 200))
    run(storage)
    text = caplog.text
    assert TASK in text
    for secret in ("VIDEO-SECRET", "PREVIEW-SECRET", "FRAME-SECRET", "CALLBACK-SECRET", SIGNED):
        assert secret not in text


def test_preview_command_keeps_the_whole_video_and_even_size():
    command = worker.preview_command(Path("in.mp4"), Path("out.mp4"))
    assert command[:2] == ["ffmpeg", "-y"]
    for flag in ("-an", "libx264", "veryfast", "yuv420p", "+faststart",
                 "scale=trunc(iw/2)*2:trunc(ih/2)*2"):
        assert flag in command
    # 자르지 않는다(근거 프레임 시각이 원본 기준)
    assert "-ss" not in command and "-t" not in command and "-to" not in command
