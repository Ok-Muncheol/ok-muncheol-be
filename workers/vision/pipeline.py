"""A: 비전 파이프라인 — 로컬 영상 파일 하나 → 팀 계약 사실 카드 v1. 로컬·Modal 공통 진입점.

영상은 Storage에서 직접 읽지 않는다. Modal(구준휘)이 서명 내려받기 URL로 받아 로컬 경로를 넘긴다.

단계
0. 입력 확인   열리는지, 길이·fps·해상도
1. 영상 맞추기 모델은 AI Hub 모양(초당 15장, 약 10초, 충돌이 5초 근처)으로 학습·평가했다.
               - 이미 초당 15장이고 20초 이하면 그대로 쓴다(평가와 같은 결과)
               - 20초 이하면 초당 15장으로 바꾼다(시간 기준으로 프레임을 고름)
               - 20초를 넘으면 초당 15장으로 바꾼 뒤 광학 흐름의 '크게 흔들린 순간'(충돌 후보) 앞뒤
                 5초를
                 자른다. 자동 자르기는 평가 전이라 충돌 시각에 늘 확인 표시(2026-11-11 사용자 결정
                 A안)
2. 비전 분석   평가(evals/vision/run_cards.py --ego --yolo --motion --video)와 같은 순서
               광학 흐름 → YOLO 상대 차·박스 판단 → 움직임 분류기·장면 → 영상 모델 v2·v3·j3·유형
               판단부
               → VLM → 카드 조립(확인 표시 규칙)
3. v1 변환     workers/vision/contract.py (영어 12칸, 자른 구간만큼 시각을 원본 기준으로 되돌림)

결과 카드의 model_info.pipeline에 영상 정보·자른 구간·모델 파일·단계별 시간을 남긴다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "data/private/models"
PIPELINE_VERSION = "2026-11-11"
TARGET_FPS = 15
CLIP_SECONDS = 10
MAX_AS_IS_SECONDS = 20  # 이 길이 이하는 자르지 않는다(AI Hub 최대 20초)
MIN_SECONDS = 2
MAX_WIDTH = 1920
# 기록용 모델 파일(크기·수정 시각을 남긴다)
MODEL_FILES = (
    "video_model_tr.pt",
    "video_model_v3.pt",
    "video_model_j3.pt",
    "type_head.pt",
    "motion_movement_B.joblib",
    "yolo11s.pt",
)


class VisionPipelineError(Exception):
    """단계별 실패. code는 API 오류 코드로 쓴다. retryable이면 같은 입력으로 다시 시도해 볼 수
    있다."""

    def __init__(self, stage: str, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.stage, self.code, self.retryable = stage, code, retryable


@dataclass
class PipelineOptions:
    use_j3: bool = True  # False면 진로변경·추월 판단을 v3가 맡는다(가볍게)
    vlm: object | None = None  # None이면 OpenAI gpt-4o(OPENAI_API_KEY 필요)
    prompt_version: str | None = None  # None이면 fact_card 기본값
    work_dir: Path | None = None  # 임시 파일 폴더. None이면 시스템 임시 폴더에 만들고 끝나면 지운다
    extra_model_info: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 0·1. 입력 확인·영상 맞추기


def probe(video_path: str) -> dict:
    """{"fps", "frames", "seconds", "width", "height"}. 못 열면 VisionPipelineError."""
    import cv2

    if not Path(video_path).is_file():
        raise VisionPipelineError("input", "video_not_found", f"영상 파일이 없습니다: {video_path}")
    cap = cv2.VideoCapture(video_path)
    try:
        if not cap.isOpened():
            raise VisionPipelineError("input", "video_unreadable", "영상을 열 수 없습니다.")
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    finally:
        cap.release()
    if fps <= 0 or frames <= 0 or width <= 0:
        raise VisionPipelineError(
            "input", "video_unreadable", "영상 정보(fps·길이)를 읽을 수 없습니다."
        )
    seconds = frames / fps
    if seconds < MIN_SECONDS:
        raise VisionPipelineError(
            "input", "video_too_short", f"영상이 {MIN_SECONDS}초보다 짧습니다."
        )
    return {
        "fps": round(fps, 3),
        "frames": frames,
        "seconds": round(seconds, 2),
        "width": width,
        "height": height,
    }


def resample(
    video_path: str, out_path: Path, start_s: float = 0.0, seconds: float | None = None
) -> int:
    """시간 기준으로 초당 TARGET_FPS장을 골라 새 mp4로 쓴다. 쓴 프레임 수를 돌려준다.

    원본 프레임 번호가 아니라 시각(k / 15초)에 가장 가까운 원본 프레임을 고른다. 폭은 MAX_WIDTH
    이하로.
    """
    import cv2

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if w > MAX_WIDTH:
        w, h = MAX_WIDTH, round(h * MAX_WIDTH / w / 2) * 2
    end_s = total / fps if seconds is None else min(total / fps, start_s + seconds)
    wanted = []
    k = 0
    while (t := start_s + k / TARGET_FPS) < end_s:
        wanted.append(min(total - 1, round(t * fps)))
        k += 1
    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), TARGET_FPS, (w, h))
    written, idx, ok, img = 0, -1, True, None
    try:
        for target in wanted:
            while idx < target and ok:
                ok, frame = cap.read()
                if ok:
                    idx, img = idx + 1, frame
            if img is None:
                break
            writer.write(img if img.shape[1] == w else cv2.resize(img, (w, h)))
            written += 1
    finally:
        cap.release()
        writer.release()
    if written < MIN_SECONDS * TARGET_FPS:
        raise VisionPipelineError(
            "normalize", "video_unreadable", "영상 프레임을 충분히 읽지 못했습니다."
        )
    return written


def normalize(video_path: str, info: dict, work: Path) -> tuple[str, dict]:
    """(분석에 쓸 영상 경로, 자른 구간 정보). 자른 구간 정보의 offset_ms를 v1 변환에 쓴다."""
    as_is = abs(info["fps"] - TARGET_FPS) < 0.5 and info["seconds"] <= MAX_AS_IS_SECONDS
    if as_is:
        return video_path, {"mode": "as_is", "offset_ms": 0, "auto_cut": False}
    if info["seconds"] <= MAX_AS_IS_SECONDS:
        out = work / "clip.mp4"
        resample(video_path, out)
        return str(out), {"mode": "resampled", "offset_ms": 0, "auto_cut": False}
    # 긴 영상: 전체를 초당 15장으로 바꾸고 충돌 후보를 찾아 그 앞뒤 5초
    from workers.vision.ego_motion import analyze_video as ego_analyze

    full = work / "full15.mp4"
    resample(video_path, full)
    ego = ego_analyze(str(full))
    half = CLIP_SECONDS / 2
    if ego.collision_frame:
        center_s = ego.collision_frame / TARGET_FPS
        start_s = max(0.0, min(center_s - half, info["seconds"] - CLIP_SECONDS))
        found = True
    else:  # 충돌 후보를 못 찾으면 앞 10초(확인 표시로 담당자가 본다)
        start_s, found = 0.0, False
    out = work / "clip.mp4"
    resample(str(full), out, start_s=start_s, seconds=CLIP_SECONDS)
    return str(out), {
        "mode": "auto_cut",
        "offset_ms": round(start_s * 1000),
        "auto_cut": True,
        "collision_found": found,
        "cut_seconds": [round(start_s, 2), round(start_s + CLIP_SECONDS, 2)],
    }


# ---------------------------------------------------------------- 2. 비전 분석


def build_detections(video: str, work: Path, *, use_j3: bool = True) -> dict:
    """평가(run_cards.py --ego --yolo --motion --video)와 같은 detections. 캐시는 이 작업
    폴더에만."""
    from workers.vision.detection import box_detections, detect_and_track, pick_opponent
    from workers.vision.ego_motion import analyze_video as ego_analyze
    from workers.vision.motion_model import motion_opinions
    from workers.vision.scene_embed import embed_video, predict_scene
    from workers.vision.type_model import type_opinions
    from workers.vision.video_model import predict

    yolo_cache = work / "yolo.json"  # 같은 영상의 YOLO는 한 번만 돌린다
    ego = ego_analyze(video)
    detections: dict = {"hint": ego.summary(), "ego": ego}
    detections.update(box_detections(video, yolo_cache))
    det = detect_and_track(video, cache_path=yolo_cache)
    track_m, why_m = pick_opponent(det, stitch=True)
    emb = embed_video(video)
    scene = {k: p for k in ("location_type", "road_priority") if (p := predict_scene(k, emb))}
    detections["motion"] = motion_opinions(det, ego, track_m, why_m, emb)
    detections["scene"] = scene
    detections["video"] = predict(video)
    detections["video_v3"] = predict(video, spec="v3")
    if use_j3:
        detections["video_j3"] = predict(video, spec="j3")
    detections["type"] = type_opinions(video)
    return detections


def default_vlm():
    from workers.vision.fact_card import OpenAIVLM

    return OpenAIVLM(model="gpt-4o")


def model_files() -> dict:
    out = {}
    for name in MODEL_FILES:
        p = MODELS / name
        if p.exists():
            st = p.stat()
            out[name] = {
                "bytes": st.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            }
        else:
            out[name] = None
    return out


# ---------------------------------------------------------------- 전체


def analyze_video_sync(
    case_id: str,
    video_path: str,
    *,
    version: int = 1,
    accident_date: str | None = None,
    options: PipelineOptions | None = None,
) -> dict:
    """로컬 영상 → 팀 계약 v1 카드(dict). 실패하면 VisionPipelineError(단계·코드·재시도 가능
    여부)."""
    import shutil
    import tempfile

    from workers.vision.contract import to_contract_card
    from workers.vision.fact_card import PROMPT_VERSION, extract_fact_card_sync

    opts = options or PipelineOptions()
    timings: dict[str, float] = {}
    own_dir = opts.work_dir is None
    work = Path(tempfile.mkdtemp(prefix="vision-")) if own_dir else Path(opts.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    try:
        t = time.time()
        info = probe(video_path)
        clip, cut = normalize(video_path, info, work)
        timings["normalize"] = round(time.time() - t, 1)

        t = time.time()
        try:
            detections = build_detections(clip, work, use_j3=opts.use_j3)
        except VisionPipelineError:
            raise
        except Exception as exc:  # 모델 파일 없음·GPU 오류 등
            raise VisionPipelineError(
                "detect", "vision_failed", f"영상 분석 실패: {exc!r}"
            ) from exc
        timings["detect"] = round(time.time() - t, 1)

        t = time.time()
        try:
            card = extract_fact_card_sync(
                case_id,
                clip,
                detections,
                viewpoint=1,
                vlm=opts.vlm or default_vlm(),
                prompt_version=opts.prompt_version or PROMPT_VERSION,
            )
        except Exception as exc:  # VLM 호출 실패는 다시 시도해 볼 수 있다
            raise VisionPipelineError("vlm", "vlm_failed", f"VLM 호출 실패: {exc!r}", True) from exc
        timings["card"] = round(time.time() - t, 1)
    finally:
        if own_dir:
            shutil.rmtree(work, ignore_errors=True)

    if cut["auto_cut"] and "collision_time_ms" in card["fields"]:
        card["fields"]["collision_time_ms"]["verification_status"] = "needs_review"
    out = to_contract_card(
        card, accident_date=accident_date, version=version, offset_ms=cut["offset_ms"]
    )
    out["model_info"]["pipeline"] = {
        "version": PIPELINE_VERSION,
        "video": info,
        "cut": cut,
        "use_j3": opts.use_j3,
        "models": model_files(),
        "seconds": timings,
        **opts.extra_model_info,
    }
    return out


async def analyze_video(
    case_id: str,
    video_path: str,
    input_version: int = 1,
    *,
    accident_date: str | None = None,
    options: PipelineOptions | None = None,
) -> dict:
    """비동기 진입점(API·Modal). 무거운 계산은 별도 스레드에서 돈다. video_path는 로컬 파일."""
    return await asyncio.to_thread(
        analyze_video_sync,
        case_id,
        video_path,
        version=input_version,
        accident_date=accident_date,
        options=options,
    )
