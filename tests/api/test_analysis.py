"""A: 영상 분석 접수·Modal 결과 수신 — app/api/analysis.py, app/storage.create_signed_download_url.

DB·Storage·Modal 없이 메모리 저장소와 가짜 함수로 확인한다. SQL은 Docker Postgres로 따로 확인했다.
- 접수: 경로(이 사건 videos/만)·권한·업로드 여부, 같은 Idempotency-Key는 같은 작업, 진행 중이면 409
- Modal 호출: 넘기는 값, 토큰은 해시만 저장, 호출 실패·미연결은 작업 failed
- 콜백: 토큰 401, completed면 카드 새 버전 + 작업 completed, 같은 콜백은 한 번만,
  잘못된 카드는 failed
- 시간 초과: 15분 넘은 작업은 failed(analysis_timeout)로 바뀌고 다시 접수할 수 있다
"""

import asyncio
import contextlib
import copy
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app import storage
from app.api import analysis
from app.api.errors import ApiException
from app.config import Settings
from tests.api.test_fact_cards import MemoryRepo, assignees_of

ACTOR, OTHER = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
CASE = "33333333-3333-3333-3333-333333333333"
INJURY = "55555555-5555-5555-5555-555555555555"  # 대인 담당자
VIDEO = f"case-files/{CASE}/videos/{uuid.UUID(int=7)}.mp4"
KEY = "analysis-key-0001"
EXAMPLE = Path(__file__).resolve().parents[2] / "docs/contracts/fact-card-v1.example.json"


def run(coro):
    return asyncio.run(coro)


class MemoryAnalysisRepo:
    def __init__(self, assignees):
        self.by_case = assignees  # 사건 → 담당자. 대인 담당자가 있으면 (대물 담당자, 대인 담당자)
        self.tasks: dict[str, dict] = {}
        self.stale: set[str] = set()  # 15분이 지났다고 칠 작업

    async def assignees(self, case_id):
        return assignees_of(self.by_case, case_id)

    async def get(self, task_id):
        return copy.deepcopy(self.tasks.get(task_id))

    async def by_key(self, case_id, key):
        return next((copy.deepcopy(t) for t in self.tasks.values()
                     if t["case_id"] == case_id and t["idempotency_key"] == key), None)

    async def expire(self, case_id):
        for t in self.tasks.values():
            if t["case_id"] == case_id and t["id"] in self.stale and t["status"] in (
                    "queued", "running"):
                t.update(status="failed", error=dict(analysis.TIMEOUT_ERROR))

    async def insert(self, case_id, storage_path, key, token_digest, actor_id):
        if any(t["case_id"] == case_id and t["status"] in ("queued", "running")
               for t in self.tasks.values()):
            return None
        task = {"id": str(uuid.uuid4()), "case_id": case_id, "storage_path": storage_path,
                "idempotency_key": key, "callback_token_hash": token_digest, "status": "queued",
                "modal_call_id": None, "fact_card_version": None, "error": None,
                "requested_by": actor_id, "created_at": datetime.now(UTC)}
        self.tasks[task["id"]] = task
        return copy.deepcopy(task)

    async def mark_running(self, task_id, call_id):
        t = self.tasks[task_id]
        if t["status"] != "queued":
            return None
        t.update(status="running", modal_call_id=call_id)
        return copy.deepcopy(t)

    async def finish(self, task_id, status, card_version, error):
        t = self.tasks[task_id]
        if t["status"] not in ("queued", "running"):
            return None
        t.update(status=status, fact_card_version=card_version, error=error)
        return copy.deepcopy(t)

    async def latest_completed(self, case_id):
        done = [t for t in self.tasks.values() if t["case_id"] == case_id
                and t["status"] == "completed"]
        return copy.deepcopy(done[-1]) if done else None

    def transaction(self):
        return contextlib.AsyncExitStack()


@pytest.fixture
def env():
    """담당자 ACTOR의 사건 CASE. 영상은 VIDEO만 올라가 있다. dispatch 호출은 jobs에 쌓인다."""
    tasks = MemoryAnalysisRepo({CASE: ACTOR})
    cards = MemoryRepo({CASE: ACTOR})
    jobs: list[dict] = []

    @asynccontextmanager
    async def connect():
        yield None

    async def sign(object_key, expires_in):
        assert expires_in == analysis.VIDEO_URL_SECONDS
        return f"https://x/storage/v1/object/sign/{object_key}?token=t" if (
            f"case-files/{object_key}" == VIDEO) else None

    async def dispatch(job):
        jobs.append(job)
        return "fc-modal-call-1"

    async def sign_upload(object_key):
        return f"https://x/storage/v1/object/upload/sign/case-files/{object_key}?token=u"

    old = analysis.services
    analysis.configure(connect=connect, repo=lambda conn: tasks, fact_repo=lambda conn: cards,
                       sign_download=sign, sign_upload=sign_upload, dispatch=dispatch)
    yield tasks, cards, jobs
    analysis.services = old


def start(actor=ACTOR, case=CASE, path=VIDEO, key=KEY):
    return run(analysis.start_analysis(actor, case, path, key))


def error(exc_info) -> tuple[int, str]:
    return exc_info.value.status_code, exc_info.value.error["code"]


def card() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 접수


def test_start_dispatches_and_runs(env):
    tasks, _, jobs = env
    out = start()
    assert (out["stage"], out["status"], out["case_id"]) == ("analysis", "running", CASE)
    job = jobs[0]
    assert job["task_id"] == out["task_id"] and job["case_id"] == CASE
    assert job["video_url"].startswith("https://x/storage/v1/object/sign/")
    assert job["callback_path"] == f"/internal/analysis/{out['task_id']}"
    stored = tasks.tasks[out["task_id"]]
    assert stored["callback_token_hash"] == analysis.token_hash(job["callback_token"])
    assert job["callback_token"] not in json.dumps(stored, default=str)  # 원문은 저장하지 않는다
    assert stored["modal_call_id"] == "fc-modal-call-1"


@pytest.mark.parametrize(
    ("kw", "code"),
    [({"path": f"case-files/{uuid.UUID(int=9)}/videos/{uuid.UUID(int=7)}.mp4"},
      (422, "invalid_storage_path")),  # 다른 사건 폴더
     ({"path": f"case-files/{CASE}/estimates/{uuid.UUID(int=7)}.pdf"},
      (422, "invalid_storage_path")),
     ({"path": f"case-files/{CASE}/videos/../x.mp4"}, (422, "invalid_storage_path")),
     ({"path": f"case-files/{CASE}/videos/{uuid.UUID(int=8)}.mp4"},
      (422, "video_not_uploaded")),
     ({"actor": OTHER}, (403, "forbidden")),
     ({"case": "not-a-uuid"}, (404, "not_found"))],
)
def test_start_rejects(env, kw, code):
    tasks, _, jobs = env
    with pytest.raises(ApiException) as e:
        start(**kw)
    assert error(e) == code
    assert not tasks.tasks and not jobs


def test_same_key_returns_same_task(env):
    _, _, jobs = env
    first = start()
    assert start() == first and len(jobs) == 1
    with pytest.raises(ApiException) as e:
        start(path=f"case-files/{CASE}/videos/{uuid.UUID(int=8)}.mp4")
    assert error(e) == (422, "idempotency_key_reused")


def test_one_active_analysis_per_case(env):
    start()
    with pytest.raises(ApiException) as e:
        start(key="analysis-key-0002")
    assert error(e) == (409, "analysis_in_progress")


def test_dispatch_missing_or_failing_marks_failed(env):
    analysis.configure(dispatch=None)
    out = start()
    assert out["status"] == "failed" and out["error"]["code"] == "analysis_not_configured"

    async def boom(job):
        raise RuntimeError("modal down")

    analysis.configure(dispatch=boom)
    out = start(key="analysis-key-0002")
    assert out["status"] == "failed"
    assert (out["error"]["code"], out["error"]["retryable"]) == ("dispatch_failed", True)


def test_timeout_frees_case_for_new_analysis(env):
    tasks, _, _ = env
    first = start()
    tasks.stale.add(first["task_id"])
    second = start(key="analysis-key-0002")
    assert second["status"] == "running"
    old = tasks.tasks[first["task_id"]]
    assert old["status"] == "failed" and old["error"]["code"] == "analysis_timeout"


# ---------------------------------------------------------------- 콜백


def callback(task_id, token, body):
    return run(analysis.receive_result(task_id, token, body))


def test_callback_completed_saves_card_once(env):
    tasks, cards, jobs = env
    task = start()
    token = jobs[0]["callback_token"]
    out = callback(task["task_id"], token, {"status": "completed", "card": card()})
    assert (out["status"], out["fact_card_version"]) == ("completed", 1)
    assert [r["origin"] for r in cards.rows] == ["vision"]
    assert cards.rows[0]["card"]["case_id"] == CASE and cards.rows[0]["card"]["status"] == "draft"
    again = callback(task["task_id"], token, {"status": "completed", "card": card()})
    assert again == out and len(cards.rows) == 1  # 같은 콜백은 한 번만


def test_callback_wrong_token_is_401(env):
    _, cards, _ = env
    task = start()
    for token in ("wrong", None):
        with pytest.raises(ApiException) as e:
            callback(task["task_id"], token, {"status": "completed", "card": card()})
        assert error(e) == (401, "invalid_callback_token")
    assert not cards.rows


def test_callback_failed_keeps_pipeline_error(env):
    _, _, jobs = env
    task = start()
    err = {"stage": "normalize", "code": "video_too_short", "message": "영상이 짧습니다",
           "retryable": False}
    out = callback(task["task_id"], jobs[0]["callback_token"], {"status": "failed", "error": err})
    assert out["status"] == "failed" and out["error"] == err
    bad = start(key="analysis-key-0002")
    out = callback(bad["task_id"], jobs[1]["callback_token"], {"status": "failed", "error": "x"})
    assert (out["error"]["code"], out["error"]["retryable"]) == ("internal_error", True)


def test_callback_invalid_card_fails_task(env):
    _, cards, jobs = env
    task = start()
    korean = card()
    korean["fields"]["movement_A"]["value"] = "추월"  # v1 변환 전 내부 값
    out = callback(task["task_id"], jobs[0]["callback_token"],
                   {"status": "completed", "card": korean})
    assert out["status"] == "failed"
    assert (out["error"]["stage"], out["error"]["code"]) == ("save_card", "invalid_fact_value")
    assert not cards.rows


def test_callback_unknown_task_and_bad_status(env):
    with pytest.raises(ApiException) as e:
        callback(str(uuid.uuid4()), "t", {"status": "completed"})
    assert error(e) == (404, "not_found")
    _, _, jobs = env
    task = start()
    with pytest.raises(ApiException) as e:
        callback(task["task_id"], jobs[0]["callback_token"], {"status": "done"})
    assert error(e) == (422, "invalid_callback")


# ---------------------------------------------------------------- 내려받기 서명 URL


def _settings():
    return Settings(app_env="local", on_vercel=False, supabase_url="https://example.supabase.co",
                    supabase_jwt_secret=None, jwt_audience="authenticated",
                    cors_allow_origins=(), dev_actor_id=None,
                    supabase_secret_key="sb_secret_test-only-not-a-real-key")


def test_signed_download_url(monkeypatch):
    monkeypatch.setattr(storage, "get_settings", _settings)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"signedURL": "/object/sign/case-files/a.mp4?token=t"})

    url = run(storage.create_signed_download_url(
        f"{CASE}/videos/a.mp4", 3600, transport=httpx.MockTransport(handler)))
    assert url == "https://example.supabase.co/storage/v1/object/sign/case-files/a.mp4?token=t"
    req = seen[0]
    assert str(req.url).endswith(f"/storage/v1/object/sign/case-files/{CASE}/videos/a.mp4")
    assert json.loads(req.content) == {"expiresIn": 3600}


def test_signed_download_url_missing_file_is_none(monkeypatch):
    monkeypatch.setattr(storage, "get_settings", _settings)

    def handler(request):
        return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})

    assert run(storage.create_signed_download_url(
        "x/videos/a.mp4", transport=httpx.MockTransport(handler))) is None


# ---------------------------------------------------------------- 근거 사진·미리보기


def test_start_passes_frame_and_preview_upload_slots(env):
    """워커(workers/modal_worker.py)가 받는 모양: 12칸 {storage_path, upload_url} + 미리보기 URL."""
    import re

    _, _, jobs = env
    task = start()["task_id"]
    slots = jobs[0]["frame_uploads"]
    assert len(slots) == analysis.FRAME_UPLOADS == 12
    worker_path = re.compile(  # workers/modal_worker.py _FRAME_PATH
        r"^case-files/[0-9a-f-]{36}/frames/[0-9a-f-]{36}/[0-9A-Za-z._-]+\.jpg$")
    for i, slot in enumerate(slots, start=1):
        assert slot["storage_path"] == f"case-files/{CASE}/frames/{task}/{i:02d}.jpg"
        assert worker_path.match(slot["storage_path"])
        assert slot["upload_url"].endswith(f"{slot['storage_path']}?token=u")
    assert jobs[0]["preview_upload_url"].endswith(f"case-files/{CASE}/previews/{task}.mp4?token=u")
    assert analysis.preview_path(CASE, task) == f"case-files/{CASE}/previews/{task}.mp4"


def test_upload_signing_failure_still_dispatches_without_photos(env):
    _, _, jobs = env

    async def broken(object_key):
        raise ApiException(503, "storage_not_configured", "x", False)

    analysis.configure(sign_upload=broken)
    out = start()
    assert out["status"] == "running"
    assert jobs[0]["frame_uploads"] == [] and jobs[0]["preview_upload_url"] is None


def test_callback_keeps_only_this_tasks_frame_paths(env):
    _, cards, jobs = env
    task = start()["task_id"]
    body = card()
    own = f"case-files/{CASE}/frames/{task}/01.jpg"
    other_case = "case-files/99999999-9999-9999-9999-999999999999/frames/x/01.jpg"
    body["evidence_frames"] = [
        {"frame_id": "f001", "timestamp_ms": 0, "storage_path": own},
        {"frame_id": "f002", "timestamp_ms": 10, "storage_path": other_case},
        {"frame_id": "f003", "timestamp_ms": 20, "storage_path": f"case-files/{CASE}/videos/a.mp4"},
        {"frame_id": "f004", "timestamp_ms": 30, "storage_path": None},
    ]
    callback(task, jobs[0]["callback_token"], {"status": "completed", "card": body})
    saved = cards.rows[-1]["card"]
    assert [f["storage_path"] for f in saved["evidence_frames"]] == [own, None, None, None]


# ---------------------------------------------------------------- 근거 보기 URL


def _evidence_env(env, uploaded):
    """sign_download이 uploaded에 있는 경로만 서명한다(Storage에 있는 파일)."""
    tasks, cards, jobs = env

    async def sign(object_key, expires_in):
        path = f"case-files/{object_key}"
        if path == VIDEO:
            return "https://x/video"
        assert expires_in == analysis.EVIDENCE_URL_SECONDS
        return f"https://x/view/{path}" if path in uploaded else None

    analysis.configure(sign_download=sign)
    return tasks, cards, jobs


def test_evidence_signs_own_frames_and_preview(env):
    uploaded: set[str] = set()
    _, _, jobs = _evidence_env(env, uploaded)
    task = start()["task_id"]
    own = [f"case-files/{CASE}/frames/{task}/0{i}.jpg" for i in (1, 2)]
    uploaded.update([own[0], f"case-files/{CASE}/previews/{task}.mp4"])
    body = card()
    body["evidence_frames"] = [
        {"frame_id": "f001", "timestamp_ms": 100, "storage_path": own[0]},
        {"frame_id": "f002", "timestamp_ms": 200, "storage_path": own[1]},  # 아직 안 올라감
        {"frame_id": "f003", "timestamp_ms": 300, "storage_path": None},
    ]
    callback(task, jobs[0]["callback_token"], {"status": "completed", "card": body})
    out = run(analysis.get_evidence(ACTOR, CASE))
    assert [(f["frame_id"], f["url"]) for f in out["frames"]] == [
        ("f001", f"https://x/view/{own[0]}"), ("f002", None), ("f003", None)]
    assert out["preview_url"] == f"https://x/view/case-files/{CASE}/previews/{task}.mp4"
    assert out["expires_in"] == 3600


def test_evidence_never_signs_other_paths(env):
    _, cards, _ = _evidence_env(env, uploaded=set())
    body = card()
    body["evidence_frames"] = [
        {"frame_id": "f001", "timestamp_ms": 0, "storage_path": VIDEO},
        {"frame_id": "f002", "timestamp_ms": 0,
         "storage_path": f"case-files/{CASE}/frames/../videos/a.mp4"},
    ]
    cards.rows.append({"card": {**body, "case_id": CASE, "version": 1}, "origin": "vision",
                       "reason": None, "actor": None})
    out = run(analysis.get_evidence(ACTOR, CASE))
    assert [f["url"] for f in out["frames"]] == [None, None]
    assert out["preview_url"] is None


def test_injury_assignee_starts_analysis_and_reads_evidence(env):
    """대인 담당자(cases.injury_assignee_id)도 분석을 접수하고 근거를 본다."""
    tasks, _, _ = _evidence_env(env, uploaded=set())
    tasks.by_case[CASE] = (ACTOR, INJURY)
    out = start(actor=INJURY)
    assert out["status"] == "running" and tasks.tasks[out["task_id"]]["requested_by"] == INJURY
    assert run(analysis.get_evidence(INJURY, CASE))["frames"] == []
    with pytest.raises(ApiException) as e:
        start(actor=OTHER, key="analysis-key-0002")
    assert error(e) == (403, "forbidden")


def test_evidence_empty_before_analysis_and_checks_assignee(env):
    _evidence_env(env, uploaded=set())
    assert run(analysis.get_evidence(ACTOR, CASE)) == {
        "frames": [], "preview_url": None, "expires_in": 3600}
    with pytest.raises(ApiException) as e:
        run(analysis.get_evidence("99999999-9999-9999-9999-999999999999", CASE))
    assert error(e) == (403, "forbidden")
    with pytest.raises(ApiException) as e:
        run(analysis.get_evidence(ACTOR, "not-a-uuid"))
    assert error(e)[0] == 404
