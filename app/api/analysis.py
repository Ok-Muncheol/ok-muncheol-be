"""A: 영상 분석 접수(POST /cases/{id}/analysis)와 Modal 결과 수신. 2026-11-11 정지원·구준휘 합의.

1. start_analysis: 담당자 확인 → 영상 경로 확인(이 사건의 videos/) → 15분 넘은 작업 정리 →
   같은 Idempotency-Key면 그 작업 → 내려받기 서명 URL(1시간, 업로드 확인 겸) →
   작업 행(queued, 일회용 토큰 해시) → dispatch(구준휘: Modal 호출, call_id 반환) → running.
   이미 진행 중이면 409 analysis_in_progress.
2. receive_result: Modal이 POST /internal/analysis/{task_id}(Bearer 토큰)로 부르면 구준휘의 경로가
   이 함수를 부른다. 토큰 확인 → completed면 카드 새 버전 저장 + 작업 completed(한 트랜잭션),
   failed면 오류 그대로. 끝난 작업에 다시 오면 바꾸지 않는다(같은 콜백은 한 번만).
3. 콜백이 안 오면 조회·재접수 때 15분 넘은 queued·running을 failed(analysis_timeout)로 바꾼다.
4. get_evidence(GET /cases/{id}/evidence): 최신 카드 근거 사진과 최신 완료 분석의 미리보기 영상을
   보기용 서명 URL(1시간)로 준다. 이 사건의 frames/·previews/ 경로만 서명한다.
   파일이 없으면 url null.

dispatch에 넘기는 값: {task_id, case_id, video_url, callback_path, callback_token,
frame_uploads, preview_upload_url}.
- frame_uploads: 근거 사진 칸 최대 12개 [{storage_path, upload_url}]. 경로는
  case-files/{case}/frames/{task}/NN.jpg. 워커가 카드 evidence_frames 순서대로 올리고
  storage_path를 채운다(workers/modal_worker.py).
- preview_upload_url: H.264 미리보기(case-files/{case}/previews/{task}.mp4, preview_path).
- 서명 업로드 URL을 못 만들면 사진 없이 분석한다(빈 목록·null). 사진은 분석을 막지 않는다.
- 콜백 카드의 evidence_frames[].storage_path는 이 작업의 frames/ 경로만 남기고 나머지는 null로 둔다.
callback_url은 BE 주소를 아는 호출 함수가 callback_path로 만든다. 콜백 본문:
{"status": "completed", "card": FactCard v1} 또는
{"status": "failed", "error": {stage?, code, message, retryable}}.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from app import db, storage
from app.api import facts
from app.api.errors import ApiException, forbidden, not_found, unprocessable
from app.workflow import runs

TIMEOUT_MINUTES = 15
VIDEO_URL_SECONDS = 3600
EVIDENCE_URL_SECONDS = 3600
CALLBACK_PATH = "/internal/analysis/{task_id}"
_VIDEO_PATH = re.compile(
    r"^case-files/(?P<case>[0-9a-f-]{36})/videos/[0-9a-f-]{36}\.(mp4|mov|avi|webm|mkv)$"
)
FRAME_UPLOADS = 12  # 워커 MAX_FRAME_UPLOADS와 같다


def frame_path(case_id: str, task_id: str, index: int) -> str:
    """근거 사진 index(1부터)의 Storage 경로(버킷 포함)."""
    return storage.storage_path_of(f"{case_id}/frames/{task_id}/{index:02d}.jpg")


def preview_path(case_id: str, task_id: str) -> str:
    """H.264 미리보기 영상의 Storage 경로(버킷 포함). 화면 재생용."""
    return storage.storage_path_of(f"{case_id}/previews/{task_id}.mp4")


TIMEOUT_ERROR = {
    "stage": "callback",
    "code": "analysis_timeout",
    "message": f"영상 분석 결과가 {TIMEOUT_MINUTES}분 안에 오지 않았습니다. 다시 접수하세요.",
    "retryable": True,
}
# 조회·재접수 때 끝나지 않은 오래된 작업을 실패로 바꾼다(cases.get_case도 쓴다)
EXPIRE_SQL = (
    "update public.analysis_tasks set status = 'failed', error = %s, completed_at = now()"
    " where case_id = %s and status in ('queued', 'running')"
    f" and coalesce(started_at, created_at) < now() - interval '{TIMEOUT_MINUTES} minutes'"
)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AnalysisRepo(Protocol):
    async def assignee(self, case_id: str) -> str | None: ...
    async def get(self, task_id: str) -> dict | None: ...
    async def by_key(self, case_id: str, key: str) -> dict | None: ...
    async def expire(self, case_id: str) -> None: ...
    async def insert(
        self, case_id: str, storage_path: str, key: str, token_digest: str, actor_id: str
    ) -> dict | None: ...
    async def mark_running(self, task_id: str, call_id: str | None) -> dict | None: ...
    async def finish(
        self, task_id: str, status: str, card_version: int | None, error: dict | None
    ) -> dict | None: ...
    async def latest_completed(self, case_id: str) -> dict | None: ...
    def transaction(self) -> Any: ...


class PostgresAnalysisRepo:
    """public.analysis_tasks. 연결 하나로 한 요청을 처리한다."""

    def __init__(self, conn: Any):
        self.conn = conn

    async def _one(self, sql: str, params: tuple) -> dict | None:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def assignee(self, case_id: str) -> str | None:
        return await runs.case_assignee(self.conn, case_id)

    async def get(self, task_id: str) -> dict | None:
        return await self._one("select * from public.analysis_tasks where id = %s", (task_id,))

    async def by_key(self, case_id: str, key: str) -> dict | None:
        return await self._one(
            "select * from public.analysis_tasks where case_id = %s and idempotency_key = %s",
            (case_id, key),
        )

    async def expire(self, case_id: str) -> None:
        await self.conn.execute(EXPIRE_SQL, (Jsonb(TIMEOUT_ERROR), case_id))

    async def insert(
        self, case_id: str, storage_path: str, key: str, token_digest: str, actor_id: str
    ) -> dict | None:
        """새 작업(queued). 같은 사건에 진행 중 작업이 있으면 None."""
        from psycopg import errors

        try:
            return await self._one(
                "insert into public.analysis_tasks"
                " (case_id, storage_path, idempotency_key, callback_token_hash, requested_by)"
                " values (%s, %s, %s, %s, %s) returning *",
                (case_id, storage_path, key, token_digest, actor_id),
            )
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "analysis_tasks_one_active_idx":
                return None
            raise

    async def mark_running(self, task_id: str, call_id: str | None) -> dict | None:
        return await self._one(
            "update public.analysis_tasks set status = 'running', modal_call_id = %s,"
            " started_at = now() where id = %s and status = 'queued' returning *",
            (call_id, task_id),
        )

    async def finish(
        self, task_id: str, status: str, card_version: int | None, error: dict | None
    ) -> dict | None:
        """끝나지 않은 작업만 끝낸다. 이미 끝났으면 None."""
        return await self._one(
            "update public.analysis_tasks set status = %s, fact_card_version = %s, error = %s,"
            " completed_at = now() where id = %s and status in ('queued', 'running') returning *",
            (status, card_version, Jsonb(error) if error else None, task_id),
        )

    async def latest_completed(self, case_id: str) -> dict | None:
        return await self._one(
            "select * from public.analysis_tasks where case_id = %s and status = 'completed'"
            " order by completed_at desc nulls last, created_at desc limit 1",
            (case_id,),
        )

    def transaction(self) -> Any:
        return self.conn.transaction()


Dispatch = Callable[[dict[str, Any]], Awaitable[str | None]]


@dataclass
class AnalysisServices:
    """테스트·배포에서 바꿔 끼운다. dispatch는 구준휘의 Modal 호출 함수(call_id를 돌려준다)."""

    connect: Callable = db.connect
    repo: Callable[[Any], AnalysisRepo] = PostgresAnalysisRepo
    fact_repo: Callable[[Any], facts.FactRepo] = facts.PostgresFactRepo
    sign_download: Callable[..., Awaitable[str | None]] = storage.create_signed_download_url
    sign_upload: Callable[..., Awaitable[str]] = storage.create_signed_upload_url
    dispatch: Dispatch | None = None


services = AnalysisServices()


def configure(**kw: object) -> AnalysisServices:
    global services
    services = AnalysisServices(**{**services.__dict__, **kw})  # type: ignore[arg-type]
    return services


def _uuid(value: object, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise not_found(f"{what}을 찾을 수 없습니다.") from exc


def task_view(row: dict) -> dict[str, object]:
    """TaskView(app/schemas/common.py) + 분석 결과(카드 버전·오류).

    영상 분석의 입력 버전은 1이다(영상 하나).
    """
    return {
        "task_id": str(row["id"]),
        "case_id": str(row["case_id"]),
        "stage": "analysis",
        "status": row["status"],
        "input_version": 1,
        "fact_card_version": row.get("fact_card_version"),
        "error": row.get("error"),
    }


async def _upload_slots(case_id: str, task_id: str) -> dict[str, object]:
    """근거 사진 12칸과 미리보기의 서명 업로드 URL. 하나라도 못 만들면 사진 없이 간다."""
    paths = [frame_path(case_id, task_id, i) for i in range(1, FRAME_UPLOADS + 1)]
    paths.append(preview_path(case_id, task_id))
    keys = [p.removeprefix(f"{storage.STORAGE_BUCKET}/") for p in paths]
    try:
        urls = await asyncio.gather(*(services.sign_upload(k) for k in keys))
    except Exception:  # noqa: BLE001 — 저장소 설정·연결 문제여도 분석은 한다
        return {"frame_uploads": [], "preview_upload_url": None}
    return {
        "frame_uploads": [
            {"storage_path": path, "upload_url": url}
            for path, url in zip(paths[:-1], urls[:-1], strict=True)
        ],
        "preview_upload_url": urls[-1],
    }


def _own_frames(card: object, case_id: str, task_id: str) -> object:
    """카드 근거 사진 경로 중 이 작업의 frames/ 경로만 남긴다(다른 사건 파일을 가리키지 않게)."""
    if not isinstance(card, dict) or not isinstance(card.get("evidence_frames"), list):
        return card
    prefix = storage.storage_path_of(f"{case_id}/frames/{task_id}/")
    out = copy.deepcopy(card)
    for frame in out["evidence_frames"]:
        if isinstance(frame, dict):
            path = frame.get("storage_path")
            if not (isinstance(path, str) and path.startswith(prefix) and ".." not in path):
                frame["storage_path"] = None
    return out


def _error(value: object) -> dict[str, object]:
    """콜백의 오류를 ApiError 모양으로. 모양이 틀리면 internal_error(다시 시도 가능)."""
    if (
        isinstance(value, dict)
        and isinstance(value.get("code"), str)
        and isinstance(value.get("message"), str)
        and isinstance(value.get("retryable"), bool)
    ):
        out = {k: value[k] for k in ("code", "message", "retryable")}
        if isinstance(value.get("stage"), str):
            out["stage"] = value["stage"]
        return out
    return {"code": "internal_error", "message": "분석 결과의 오류 형식이 잘못되었습니다.",
            "retryable": True}


async def start_analysis(
    actor_id: str, case_id: str, storage_path: str, idempotency_key: str
) -> dict[str, object]:
    """소유권·업로드 확인 후 분석 작업을 만들고 Modal에 넘긴다. 응답은 작업 상태(TaskView)."""
    case_id = _uuid(case_id, "사건")
    match = _VIDEO_PATH.match(storage_path or "")
    if not match or match["case"] != case_id:
        raise unprocessable(
            "invalid_storage_path",
            "영상 경로는 이 사건의 /uploads(kind=video)가 준 storage_path여야 합니다.",
        )
    async with services.connect() as conn:
        repo = services.repo(conn)
        assignee = await repo.assignee(case_id)
        if assignee is None:
            raise not_found("사건을 찾을 수 없습니다.")
        if assignee != actor_id:
            raise forbidden()
        await repo.expire(case_id)
        existing = await repo.by_key(case_id, idempotency_key)
        if existing is not None:
            if existing["storage_path"] != storage_path:
                raise unprocessable(
                    "idempotency_key_reused", "같은 Idempotency-Key를 다른 영상에 썼습니다."
                )
            return task_view(existing)
        object_key = storage_path.removeprefix(f"{storage.STORAGE_BUCKET}/")
        video_url = await services.sign_download(object_key, VIDEO_URL_SECONDS)
        if video_url is None:
            raise unprocessable(
                "video_not_uploaded",
                "영상이 아직 올라가지 않았습니다. 업로드를 마친 뒤 접수하세요.",
            )
        token = secrets.token_urlsafe(32)
        row = await repo.insert(case_id, storage_path, idempotency_key, token_hash(token), actor_id)
        if row is None:
            raise ApiException(
                409, "analysis_in_progress", "이 사건의 영상 분석이 이미 진행 중입니다.", False
            )
        task_id = str(row["id"])
        if services.dispatch is None:
            error = {"stage": "dispatch", "code": "analysis_not_configured",
                     "message": "영상 분석 서버(Modal)가 아직 연결되지 않았습니다.",
                     "retryable": False}
            return task_view(await repo.finish(task_id, "failed", None, error) or row)
        job = {
            "task_id": task_id, "case_id": case_id, "video_url": video_url,
            "callback_path": CALLBACK_PATH.format(task_id=task_id), "callback_token": token,
            **await _upload_slots(case_id, task_id),
        }
        try:
            call_id = await services.dispatch(job)
        except ApiException as exc:
            error = {"stage": "dispatch", **exc.error}
            return task_view(await repo.finish(task_id, "failed", None, error) or row)
        except Exception:  # noqa: BLE001 — 호출 실패는 작업 실패로 남기고 다시 접수하게 한다
            error = {"stage": "dispatch", "code": "dispatch_failed",
                     "message": "영상 분석 서버를 부르지 못했습니다.", "retryable": True}
            return task_view(await repo.finish(task_id, "failed", None, error) or row)
        return task_view(await repo.mark_running(task_id, call_id) or await repo.get(task_id))


async def receive_result(
    task_id: str, token: str | None, body: dict[str, Any]
) -> dict[str, object]:
    """Modal 콜백 반영. 토큰이 틀리면 401, 끝난 작업이면 그대로 돌려준다(한 번만 반영)."""
    task_id = _uuid(task_id, "분석 작업")
    async with services.connect() as conn:
        repo = services.repo(conn)
        row = await repo.get(task_id)
        if row is None:
            raise not_found("분석 작업을 찾을 수 없습니다.")
        if not hmac.compare_digest(row["callback_token_hash"], token_hash(token or "")):
            raise ApiException(401, "invalid_callback_token", "콜백 토큰이 맞지 않습니다.", False)
        if row["status"] in ("completed", "failed"):
            return task_view(row)
        status = (body or {}).get("status")
        if status == "failed":
            done = await repo.finish(task_id, "failed", None, _error(body.get("error")))
        elif status == "completed":
            try:
                async with repo.transaction():
                    case_id = str(row["case_id"])
                    card = await facts.store_vision_card(
                        services.fact_repo(conn), case_id,
                        _own_frames(body.get("card"), case_id, task_id),
                    )
                    done = await repo.finish(task_id, "completed", card["version"], None)
                    if done is None:  # 그 사이 다른 콜백이 끝냈다 → 카드 저장도 되돌린다
                        raise _AlreadyDone
            except _AlreadyDone:
                done = None
            except ApiException as exc:
                if exc.status_code != 422:
                    raise
                error = {"stage": "save_card", **exc.error, "retryable": False}
                done = await repo.finish(task_id, "failed", None, error)
        else:
            raise unprocessable("invalid_callback", "status는 completed 또는 failed여야 합니다.")
        return task_view(done or await repo.get(task_id) or row)


class _AlreadyDone(Exception):
    pass


async def get_evidence(actor_id: str, case_id: str) -> dict[str, object]:
    """근거 사진·미리보기 영상의 보기용 서명 URL. 담당자만. 카드가 없으면 빈 목록."""
    case_id = _uuid(case_id, "사건")
    async with services.connect() as conn:
        repo = services.repo(conn)
        assignee = await repo.assignee(case_id)
        if assignee is None:
            raise not_found("사건을 찾을 수 없습니다.")
        if assignee != actor_id:
            raise forbidden()
        card = await services.fact_repo(conn).latest(case_id)
        task = await repo.latest_completed(case_id)
    frames = [f for f in ((card or {}).get("evidence_frames") or []) if isinstance(f, dict)]
    prefix = storage.storage_path_of(f"{case_id}/frames/")

    async def sign(path: object) -> str | None:
        ok = isinstance(path, str) and path.startswith(prefix) and ".." not in path
        if not ok:
            return None
        key = str(path).removeprefix(f"{storage.STORAGE_BUCKET}/")
        return await services.sign_download(key, EVIDENCE_URL_SECONDS)

    preview = preview_path(case_id, str(task["id"])) if task else None
    urls = await asyncio.gather(*(sign(f.get("storage_path")) for f in frames))
    preview_url = None
    if preview:
        preview_url = await services.sign_download(
            preview.removeprefix(f"{storage.STORAGE_BUCKET}/"), EVIDENCE_URL_SECONDS
        )
    return {
        "frames": [
            {"frame_id": f.get("frame_id"), "timestamp_ms": f.get("timestamp_ms"), "url": url}
            for f, url in zip(frames, urls, strict=True)
        ],
        "preview_url": preview_url,
        "expires_in": EVIDENCE_URL_SECONDS,
    }
