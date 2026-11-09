"""영상에서 차량 궤적 특징을 뽑는다.

VLM이 10~24장으로 놓치는 회전을 150프레임 전체로 판정한다.

- ego_motion(video): 촬영 차량의 프레임별 회전각과 프레임 차분. 회전각은 화면 중간 띠의
  광류(Farneback) 가로 성분 중앙값을 도 단위로 바꾼 것이다. 좌회전하면 장면이 오른쪽으로
  흐른다(flow x > 0).
  차분은 keyframes.motion_signature와 같은 신호로, 충돌 시점 추정에 쓴다.
- split_at_collision(yaws, motion, fps): 차분 피크를 충돌로 보고 그 직전까지만 회전각을 누적한다.
  충돌 뒤 밀리거나 갓길로 빠지는 움직임이 회전으로 잡히는 것을 막는다
  (사람 라벨 64건에서 규칙 정확도 0.66 → 0.75, evals/vision/README.md 2026-11-09 참고).
- track_vehicles(video): YOLO 추적으로 상대 차량 후보 궤적을 뽑는다(ultralytics, 지연 임포트).
- classify_turn(angle): 누적 회전각 → straight / left_turn / right_turn / u_turn
- describe(features): VLM 프롬프트에 붙일 한국어 요약 텍스트
- cached_features(video, cache_dir, example_id): JSON 캐시. 예전 버전이면 YOLO 궤적은 두고
  광류만 다시 잰다. cached_tracks는 버전과 무관하게 궤적만 읽는다(quality.py).
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evals.vision.keyframes import motion_signature

# 블랙박스 가로 화각을 약 120도로 보고 화소당 각도를 정한다(정밀 보정 아님, 임계값과 같이 튜닝)
HORIZONTAL_FOV_DEG = 120.0
FLOW_WIDTH = 320
TURN_DEG = 25.0      # 이보다 크면 회전
U_TURN_DEG = 120.0   # 이보다 크면 유턴
COLLISION_MARGIN_SEC = 0.3  # 충돌(차분 피크) 직전까지만 회전각 누적. 0.2~0.5초 결과 같음
VERSION = 2  # 캐시 형식. 1: 전체 구간 누적 회전각, 2: 충돌 직전 절단 + 프레임별 신호 저장
VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
# 가중치는 무시 경로에 둔다(없으면 ultralytics가 이 경로로 내려받는다)
DEFAULT_YOLO = "data/private/models/yolo11n.pt"


@dataclass
class TrackPoint:
    t: float
    cx: float  # 0~1, 화면 가로 위치
    cy: float
    w: float   # 0~1, 화면 폭 대비
    h: float


@dataclass
class VehicleTrack:
    track_id: int
    cls: str
    points: list[TrackPoint] = field(default_factory=list)

    @property
    def peak_area(self) -> float:
        return max((p.w * p.h for p in self.points), default=0.0)


@dataclass
class EgoMotion:
    yaw: list[float]     # 프레임별 회전각(도), 양수=좌회전
    motion: list[float]  # 프레임별 차분(keyframes.motion_curve와 같은 값)
    fps: float


@dataclass
class TrajectoryFeatures:
    fps: float
    frames: int
    ego_yaw_per_sec: list[float]   # 초 단위 누적 회전(도), 양수=좌회전. 전체 구간
    ego_total_yaw: float           # 충돌 직전까지 누적 회전(도). 회전 판정에 쓴다
    ego_turn: str
    tracks: list[VehicleTrack]
    collision_sec: float | None = None        # 차분 피크 시각. None이면 절단하지 않았다
    ego_total_yaw_full: float | None = None   # 충돌 이후까지 포함한 전체 누적(참고)
    ego_yaw_per_frame: list[float] = field(default_factory=list)  # 규칙을 다시 돌릴 수 있게 남긴다
    motion_per_frame: list[float] = field(default_factory=list)


def ego_motion(video_path: Path) -> EgoMotion:
    """프레임별 회전각(도, 양수=좌회전)과 프레임 차분. 첫 프레임은 둘 다 0."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    prev_gray = None
    prev_sig = None
    yaws: list[float] = []
    motion: list[float] = []
    deg_per_px = HORIZONTAL_FOV_DEG / FLOW_WIDTH
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        h = int(frame.shape[0] * FLOW_WIDTH / frame.shape[1])
        gray = cv2.cvtColor(cv2.resize(frame, (FLOW_WIDTH, h)), cv2.COLOR_BGR2GRAY)
        sig = motion_signature(frame)
        if prev_gray is None:
            yaws.append(0.0)
            motion.append(0.0)
        else:
            flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            band = flow[int(h * 0.3):int(h * 0.65), :, 0]  # 하늘과 보닛을 뺀 중간 띠, 가로 성분
            yaws.append(float(np.median(band)) * deg_per_px)
            motion.append(float(np.abs(sig - prev_sig).mean()))
        prev_gray, prev_sig = gray, sig
    cap.release()
    return EgoMotion(yaws, motion, fps)


def collision_frame(motion: list[float]) -> int | None:
    """프레임 차분이 가장 큰 프레임을 충돌로 본다(keyframes.pick_adaptive의 peak와 같은 신호).
    YOLO 최대 박스 시점(video_quality.collision_sec)은 회전 도중의 다른 차를 잡는 일이 있어
    쓰지 않는다."""
    if not motion or max(motion) <= 0:
        return None
    return max(range(len(motion)), key=motion.__getitem__)


def split_at_collision(yaws: list[float], motion: list[float], fps: float,
                       margin_sec: float = COLLISION_MARGIN_SEC,
                       ) -> tuple[float, float, float | None]:
    """(충돌 직전까지 누적 회전각, 전체 누적 회전각, 충돌 추정 시각). 피크가 없으면 앞 둘이 같다."""
    full = float(sum(yaws))
    peak = collision_frame(motion)
    if peak is None:
        return full, full, None
    cut = max(0, peak - int(round(margin_sec * fps)))
    return float(sum(yaws[:cut])), full, round(peak / fps, 2)


def classify_turn(total_yaw_deg: float, turn_deg: float = TURN_DEG,
                  u_turn_deg: float = U_TURN_DEG) -> str:
    mag = abs(total_yaw_deg)
    if mag >= u_turn_deg:
        return "u_turn"
    if mag >= turn_deg:
        return "left_turn" if total_yaw_deg > 0 else "right_turn"
    return "straight"


def track_vehicles(video_path: Path, model_name: str = DEFAULT_YOLO, stride: int = 2,
                   min_points: int = 5) -> list[VehicleTrack]:
    """YOLO 추적(ByteTrack)으로 차량 궤적을 뽑는다. stride 프레임마다 한 번 추론한다."""
    from ultralytics import YOLO

    model = YOLO(model_name)
    tracks: dict[int, VehicleTrack] = {}
    results = model.track(str(video_path), stream=True, persist=True, verbose=False,
                          vid_stride=stride, classes=list(VEHICLE_CLASSES), conf=0.3,
                          tracker="bytetrack.yaml", imgsz=640)
    for i, r in enumerate(results):
        if r.boxes is None or r.boxes.id is None:
            continue
        fh, fw = r.orig_shape
        t = i * stride / 15.0
        for box, tid, cls in zip(r.boxes.xywh.tolist(), r.boxes.id.int().tolist(),
                                 r.boxes.cls.int().tolist(), strict=True):
            cx, cy, w, h = box
            track = tracks.setdefault(tid, VehicleTrack(tid, VEHICLE_CLASSES.get(cls, str(cls))))
            track.points.append(TrackPoint(round(t, 2), round(cx / fw, 3), round(cy / fh, 3),
                                           round(w / fw, 3), round(h / fh, 3)))
    return [tr for tr in tracks.values() if len(tr.points) >= min_points]


def tracks_from_dict(items: list[dict]) -> list[VehicleTrack]:
    """to_dict()로 저장한 궤적을 되살린다(캐시 업그레이드용)."""
    return [VehicleTrack(it["track_id"], it["cls"], [TrackPoint(**p) for p in it["points"]])
            for it in items]


def extract(video_path: Path, with_yolo: bool = True, top_tracks: int = 3,
            tracks: list[VehicleTrack] | None = None) -> TrajectoryFeatures:
    """tracks를 주면(캐시 업그레이드) YOLO를 다시 돌리지 않는다."""
    ego = ego_motion(video_path)
    n = len(ego.yaw)
    step = max(1, int(round(ego.fps)))
    per_sec: list[float] = []
    acc = 0.0
    for i, y in enumerate(ego.yaw):
        acc += y
        if (i + 1) % step == 0 or i == n - 1:
            per_sec.append(round(acc, 1))
    pre, full, collision = split_at_collision(ego.yaw, ego.motion, ego.fps)
    if tracks is None:
        tracks = track_vehicles(video_path) if with_yolo else []
    # 충돌 상대는 화면에서 가장 커지는 차일 가능성이 높다
    tracks = sorted(tracks, key=lambda tr: tr.peak_area, reverse=True)[:top_tracks]
    return TrajectoryFeatures(
        fps=ego.fps, frames=n, ego_yaw_per_sec=per_sec, ego_total_yaw=round(pre, 1),
        ego_turn=classify_turn(pre), tracks=tracks, collision_sec=collision,
        ego_total_yaw_full=round(full, 1),
        ego_yaw_per_frame=[round(y, 3) for y in ego.yaw],
        motion_per_frame=[round(m, 3) for m in ego.motion])


def describe(f: TrajectoryFeatures) -> str:
    if f.collision_sec is not None:
        span = f"충돌 추정 t{f.collision_sec:.1f}s 직전까지, 이후는 판정에서 뺌"
    else:
        span = "전체 구간"
    lines = [
        f"영상 전체({f.frames}프레임)에서 자동 추출한 궤적 특징"
        "(보조 자료, 프레임과 함께 판단할 것):",
        f"- 촬영 차량 누적 회전각(광류 추정, 양수=좌회전, {span}): {f.ego_total_yaw:+.0f}도 "
        f"→ 추정 {f.ego_turn}. 초별 누적(전체 구간): {f.ego_yaw_per_sec}",
    ]
    for tr in f.tracks:
        pts = tr.points[:: max(1, len(tr.points) // 8)]
        path = " → ".join(f"t{p.t:.1f}s(x{p.cx:.2f},y{p.cy:.2f},w{p.w:.2f})" for p in pts)
        lines.append(f"- 상대 차량 후보 #{tr.track_id}({tr.cls}, 최대 크기 {tr.peak_area:.3f}): "
                     f"{path}")
    if not f.tracks:
        lines.append("- 추적된 상대 차량 없음")
    lines.append("x는 화면 가로 위치(0=왼쪽,1=오른쪽), y는 세로(1=아래), "
                 "w는 화면 폭 대비 차 폭(가까울수록 큼).")
    return "\n".join(lines)


def to_dict(f: TrajectoryFeatures) -> dict:
    return asdict(f)


def cached_features(video_path: Path, cache_dir: Path, example_id: str) -> dict:
    """{"version", "text", "ego_turn", "ego_total_yaw", "collision_sec", "features"}를
    JSON으로 캐시한다. 예전 버전 캐시는 YOLO 궤적을 그대로 두고 광류만 다시 재서 올린다
    (건당 1초 미만)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{example_id}.json"
    old = json.loads(path.read_text()) if path.exists() else None
    if old and old.get("version") == VERSION:
        return old
    tracks = None
    if old and isinstance((old.get("features") or {}).get("tracks"), list):
        tracks = tracks_from_dict(old["features"]["tracks"])
    features = extract(video_path, tracks=tracks)
    data = {"version": VERSION, "text": describe(features), "ego_turn": features.ego_turn,
            "ego_total_yaw": features.ego_total_yaw, "collision_sec": features.collision_sec,
            "features": to_dict(features)}
    path.write_text(json.dumps(data, ensure_ascii=False))
    return data


def cached_tracks(video_path: Path, cache_dir: Path, example_id: str) -> list[dict]:
    """캐시의 상대 차량 궤적(dict 목록). 버전과 무관하게 읽고, 캐시가 없을 때만 계산한다."""
    path = cache_dir / f"{example_id}.json"
    if path.exists():
        return (json.loads(path.read_text()).get("features") or {}).get("tracks") or []
    return cached_features(video_path, cache_dir, example_id)["features"]["tracks"]
