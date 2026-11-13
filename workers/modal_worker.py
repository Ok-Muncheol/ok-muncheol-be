"""B(인프라): Modal 영상 분석 워커 본문. `infra/modal_app.py`가 GPU 컨테이너에서 `run_job`을 부른다.

job은 BE `app/modal_dispatch.py`가 보낸다(11/11 정지원·구준휘 합의, 11/12 미리보기·프레임 추가).
- 필수: `task_id`, `case_id`, `video_url`(서명 내려받기), `callback_url`, `callback_token`
- 선택: `preview_upload_url`(H.264 재생 사본),
  `frame_uploads`([{storage_path, upload_url}], 최대 12칸)

순서
1. 영상 받기(서명 URL, 크기 한도)
2. 미리보기: 분석과 나란히 H.264 사본을 만들어 PUT한다. AI Hub 영상이 mp4v라 Chrome에서 재생되지
   않아서다. 원본 전체를 자르지 않는다(근거 프레임 시각이 원본 기준). 실패해도 분석은 계속한다
3. 분석: 정지원 `workers.vision.pipeline.analyze_video_sync` → 카드 v1
4. 근거 프레임: 카드 `evidence_frames` 순서대로 원본에서 그 시각 프레임을 JPG로 뽑아 k번째 칸에
   PUT하고 `storage_path`를 채운다. 못 올리면 null로 둔다
5. 콜백: `Authorization: Bearer <callback_token>`. 연결 실패·5xx·429는 몇 번 다시 보낸다

서명 URL·콜백 토큰은 로그에 남기지 않는다(task_id만). 무거운 패키지(cv2·파이프라인)는 함수
안에서만 불러서 이 모듈은 CI(`scripts/check_scaffold.py`)에서도 import된다.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

VIDEO_MAX_BYTES = 60 * 1024 * 1024  # Supabase 무료 플랜 업로드 한도(50MB)보다 넉넉히
MAX_FRAME_UPLOADS = 12
FRAME_MAX_WIDTH = 1280
FRAME_JPEG_QUALITY = 85
PREVIEW_TIMEOUT_SECONDS = 300
PREVIEW_JOIN_SECONDS = 120  # 분석이 끝난 뒤 미리보기를 기다리는 최대 시간
HTTP_TIMEOUT_SECONDS = 60.0
CALLBACK_ATTEMPTS = 4
CALLBACK_BACKOFF_SECONDS = (2, 5, 10)
_FRAME_PATH = re.compile(r"^case-files/[0-9a-f-]{36}/frames/[0-9a-f-]{36}/[0-9A-Za-z._-]+\.jpg$")
_REQUIRED = ("task_id", "case_id", "video_url", "callback_url", "callback_token")

Runner = Callable[..., Any]
Grab = Callable[[Path, int], bytes | None]
Analyze = Callable[[str, str], dict]


class JobInvalid(ValueError):
    """job 모양이 계약과 다르다(엔드포인트가 422로 돌려준다)."""


def validate_job(job: object) -> dict[str, Any]:
    """필수 칸·https 주소·프레임 칸 모양을 확인하고 정리한 사본을 돌려준다."""
    if not isinstance(job, dict):
        raise JobInvalid("job은 객체여야 한다")
    for key in _REQUIRED:
        if not isinstance(job.get(key), str) or not job[key]:
            raise JobInvalid(f"{key}가 없다")
    urls = [job["video_url"], job["callback_url"]]
    preview = job.get("preview_upload_url")
    if preview is not None:
        if not isinstance(preview, str) or not preview:
            raise JobInvalid("preview_upload_url은 문자열이어야 한다")
        urls.append(preview)
    slots = job.get("frame_uploads") or []
    if not isinstance(slots, list) or len(slots) > MAX_FRAME_UPLOADS:
        raise JobInvalid(f"frame_uploads는 {MAX_FRAME_UPLOADS}칸 이하 목록이어야 한다")
    clean_slots = []
    for slot in slots:
        if not (isinstance(slot, dict) and isinstance(slot.get("upload_url"), str)
                and isinstance(slot.get("storage_path"), str)
                and _FRAME_PATH.match(slot["storage_path"])):
            raise JobInvalid("frame_uploads 칸은 {storage_path, upload_url}이어야 한다")
        urls.append(slot["upload_url"])
        clean_slots.append({"storage_path": slot["storage_path"], "upload_url": slot["upload_url"]})
    if any(not url.startswith("https://") for url in urls):
        raise JobInvalid("주소는 https여야 한다")
    return {**{key: job[key] for key in _REQUIRED}, "preview_upload_url": preview,
            "frame_uploads": clean_slots}


def download_video(
    url: str, dest: Path, client: httpx.Client, max_bytes: int = VIDEO_MAX_BYTES
) -> int:
    """서명 URL의 영상을 받아 쓴다. 한도를 넘으면 그 자리에서 멈춘다(ValueError)."""
    size = 0
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as out:
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("영상이 크기 한도를 넘는다")
                out.write(chunk)
    return size


def preview_command(src: Path, dst: Path) -> list[str]:
    """H.264 재생 사본(소리 없음). yuv420p는 짝수 크기만 받아서 크기를 짝수로 맞춘다."""
    return [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-an",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(dst),
    ]


def make_preview(src: Path, work: Path, upload_url: str, client: httpx.Client,
                 runner: Runner = subprocess.run) -> bool:
    """변환·업로드. 실패는 기록만 하고 False(미리보기는 없어도 되는 기능이다)."""
    dst = work / "preview.mp4"
    try:
        runner(preview_command(src, dst), check=True, capture_output=True,
               timeout=PREVIEW_TIMEOUT_SECONDS)
        resp = client.put(upload_url, content=dst.read_bytes(),
                          headers={"Content-Type": "video/mp4"})
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — 미리보기 실패는 분석을 막지 않는다
        logger.warning("미리보기 실패: %s", type(exc).__name__)
        return False


def grab_frame_jpeg(video: Path, timestamp_ms: int) -> bytes | None:
    """원본 영상의 그 시각 프레임 → JPG(긴 변 FRAME_MAX_WIDTH 이하). 못 읽으면 None."""
    import cv2  # vision-eval 의존성(Modal 이미지에만 있다)

    cap = cv2.VideoCapture(str(video))
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(max(timestamp_ms, 0)))
        ok, image = cap.read()
        if not ok or image is None:
            return None
        height, width = image.shape[:2]
        if width > FRAME_MAX_WIDTH:
            scale = FRAME_MAX_WIDTH / width
            image = cv2.resize(image, (FRAME_MAX_WIDTH, round(height * scale)))
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, FRAME_JPEG_QUALITY])
        return encoded.tobytes() if ok else None
    finally:
        cap.release()


def upload_frames(video: Path, card: dict, slots: list[dict], client: httpx.Client,
                  grab: Grab = grab_frame_jpeg) -> int:
    """카드 근거 프레임을 칸 순서대로 올리고 storage_path를 채운다. 올린 장 수를 돌려준다."""
    frames = card.get("evidence_frames") or []
    uploaded = 0
    for index, frame in enumerate(frames):
        frame["storage_path"] = None
        if index >= len(slots):
            continue
        try:
            jpeg = grab(video, int(frame.get("timestamp_ms") or 0))
            if not jpeg:
                continue
            resp = client.put(slots[index]["upload_url"], content=jpeg,
                              headers={"Content-Type": "image/jpeg"})
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — 프레임 하나 실패는 카드를 막지 않는다
            logger.warning("근거 프레임 %d 업로드 실패: %s", index, type(exc).__name__)
            continue
        frame["storage_path"] = slots[index]["storage_path"]
        uploaded += 1
    return uploaded


def failure_body(exc: BaseException) -> dict[str, Any]:
    """VisionPipelineError(stage·code·retryable)는 그대로, 그 밖은 internal_error(retryable)."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and isinstance(getattr(exc, "stage", None), str):
        return {"status": "failed", "error": {
            "stage": exc.stage, "code": code, "message": str(exc)[:500],  # type: ignore[attr-defined]
            "retryable": bool(getattr(exc, "retryable", False)),
        }}
    return {"status": "failed", "error": {
        "stage": "worker", "code": "internal_error",
        "message": "영상 분석 중 예기치 못한 오류가 났습니다.", "retryable": True,
    }}


def post_callback(url: str, token: str, body: dict, client: httpx.Client,
                  sleep: Callable[[float], None] = time.sleep) -> int | None:
    """콜백을 보낸다. 연결 실패·5xx·429만 다시 보낸다. 마지막 상태 코드(못 보냈으면 None)."""
    status: int | None = None
    for attempt in range(CALLBACK_ATTEMPTS):
        try:
            resp = client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
            status = resp.status_code
            if status < 500 and status != 429:
                return status
        except httpx.HTTPError as exc:
            logger.warning("콜백 연결 실패(%d번째): %s", attempt + 1, type(exc).__name__)
            status = None
        if attempt < CALLBACK_ATTEMPTS - 1:
            sleep(CALLBACK_BACKOFF_SECONDS[min(attempt, len(CALLBACK_BACKOFF_SECONDS) - 1)])
    return status


def _default_analyze(case_id: str, video_path: str) -> dict:
    from workers.vision.pipeline import analyze_video_sync

    return analyze_video_sync(case_id, video_path)


def run_job(job: dict, *, analyze: Analyze = _default_analyze, grab: Grab = grab_frame_jpeg,
            runner: Runner = subprocess.run, client: httpx.Client | None = None,
            sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """작업 하나를 끝까지 처리하고 콜백 결과를 돌려준다(로그·테스트용, 비밀 없음)."""
    job = validate_job(job)
    task = job["task_id"]
    own_client = client is None
    http = client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
    work = Path(tempfile.mkdtemp(prefix="okm-"))
    video = work / "input.mp4"
    preview_ok: list[bool] = []
    preview_thread: threading.Thread | None = None
    try:
        try:
            download_video(job["video_url"], video, http)
        except Exception as exc:  # noqa: BLE001 — 받지 못하면 다시 접수할 수 있다
            logger.warning("작업 %s 영상 받기 실패: %s", task, type(exc).__name__)
            body = {"status": "failed", "error": {
                "stage": "download", "code": "video_unavailable",
                "message": "영상을 받지 못했습니다. 다시 접수하세요.", "retryable": True,
            }}
        else:
            if job["preview_upload_url"]:
                preview_thread = threading.Thread(
                    target=lambda: preview_ok.append(
                        make_preview(video, work, job["preview_upload_url"], http, runner)),
                    daemon=True,
                )
                preview_thread.start()
            try:
                card = analyze(job["case_id"], str(video))
            except Exception as exc:  # noqa: BLE001 — 모든 실패를 콜백으로 알린다
                # 예외 메시지는 남기지 않는다(비밀·사건 내용이 섞일 수 있다). 이미지에 모듈이
                # 빠진 배포 문제만 모듈 이름을 남긴다(11/13 운영 첫 실행: app 없음)
                missing = f" ({exc.name})" if isinstance(exc, ImportError) and exc.name else ""
                logger.warning("작업 %s 분석 실패: %s%s", task, type(exc).__name__, missing)
                body = failure_body(exc)
            else:
                frames = upload_frames(video, card, job["frame_uploads"], http, grab)
                logger.info("작업 %s 분석 완료, 근거 프레임 %d장", task, frames)
                body = {"status": "completed", "card": card}
        if preview_thread is not None:
            preview_thread.join(PREVIEW_JOIN_SECONDS)
        status = post_callback(job["callback_url"], job["callback_token"], body, http, sleep)
        logger.info("작업 %s 콜백 %s", task, status)
        return {"task_id": task, "status": body["status"], "callback_status": status,
                "preview": bool(preview_ok and preview_ok[0])}
    finally:
        if own_client:
            http.close()
        # 미리보기 스레드가 아직 쓰고 있어도 지운다(그 스레드의 실패는 make_preview가 삼킨다)
        shutil.rmtree(work, ignore_errors=True)
