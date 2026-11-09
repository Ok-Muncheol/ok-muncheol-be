"""영상에서 차량 궤적 특징을 뽑는다.

VLM이 10~24장으로 놓치는 회전을 150프레임 전체로 판정한다.

- ego_yaw(video): 촬영 차량의 회전. 화면 중간 띠의 광류(Farneback) 가로 성분 중앙값을
  프레임마다 재서 누적 회전각(도)으로 바꾼다. 좌회전하면 장면이 오른쪽으로 흐른다(flow x > 0).
- track_vehicles(video): YOLO 추적으로 상대 차량 후보 궤적을 뽑는다(ultralytics, 지연 임포트).
- classify_turn(angle): 누적 회전각 → straight / left_turn / right_turn / u_turn
- describe(features): VLM 프롬프트에 붙일 한국어 요약 텍스트
"""

from dataclasses import asdict, dataclass, field
from pathlib import Path

# 블랙박스 가로 화각을 약 120도로 보고 화소당 각도를 정한다(정밀 보정 아님, 임계값과 같이 튜닝)
HORIZONTAL_FOV_DEG = 120.0
FLOW_WIDTH = 320
TURN_DEG = 25.0      # 이보다 크면 회전
U_TURN_DEG = 120.0   # 이보다 크면 유턴
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
class TrajectoryFeatures:
    fps: float
    frames: int
    ego_yaw_per_sec: list[float]   # 초 단위 누적 회전(도), 양수=좌회전
    ego_total_yaw: float
    ego_turn: str
    tracks: list[VehicleTrack]


def ego_yaw(video_path: Path) -> tuple[list[float], float, int]:
    """프레임별 회전각(도) 목록, fps, 프레임 수. 양수=좌회전."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    prev = None
    yaws: list[float] = []
    deg_per_px = HORIZONTAL_FOV_DEG / FLOW_WIDTH
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        h = int(frame.shape[0] * FLOW_WIDTH / frame.shape[1])
        gray = cv2.cvtColor(cv2.resize(frame, (FLOW_WIDTH, h)), cv2.COLOR_BGR2GRAY)
        if prev is not None:
            flow = cv2.calcOpticalFlowFarneback(prev, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            band = flow[int(h * 0.3):int(h * 0.65), :, 0]  # 하늘과 보닛을 뺀 중간 띠, 가로 성분
            yaws.append(float(np.median(band)) * deg_per_px)
        else:
            yaws.append(0.0)
        prev = gray
    cap.release()
    return yaws, fps, len(yaws)


def classify_turn(total_yaw_deg: float) -> str:
    mag = abs(total_yaw_deg)
    if mag >= U_TURN_DEG:
        return "u_turn"
    if mag >= TURN_DEG:
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


def extract(video_path: Path, with_yolo: bool = True, top_tracks: int = 3) -> TrajectoryFeatures:
    yaws, fps, n = ego_yaw(video_path)
    per_sec: list[float] = []
    acc = 0.0
    for i, y in enumerate(yaws):
        acc += y
        if (i + 1) % int(round(fps)) == 0 or i == n - 1:
            per_sec.append(round(acc, 1))
    tracks = track_vehicles(video_path) if with_yolo else []
    # 충돌 상대는 화면에서 가장 커지는 차일 가능성이 높다
    tracks = sorted(tracks, key=lambda tr: tr.peak_area, reverse=True)[:top_tracks]
    return TrajectoryFeatures(fps=fps, frames=n, ego_yaw_per_sec=per_sec,
                              ego_total_yaw=round(acc, 1), ego_turn=classify_turn(acc),
                              tracks=tracks)


def describe(f: TrajectoryFeatures) -> str:
    lines = [
        "영상 전체(150프레임)에서 자동 추출한 궤적 특징(보조 자료, 프레임과 함께 판단할 것):",
        f"- 촬영 차량 누적 회전각(광류 추정, 양수=좌회전): {f.ego_total_yaw:+.0f}도 "
        f"→ 추정 {f.ego_turn}. 초별 누적: {f.ego_yaw_per_sec}",
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
