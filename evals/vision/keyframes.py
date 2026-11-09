"""키프레임 선택: 균등 샘플링(기준선)과 움직임 적응형(adaptive) 두 전략.

적응형은 프레임 차분으로 움직임 곡선을 만들고, 누적 움직임을 등분하는 지점의 프레임을 고른다.
움직임이 큰 구간(충돌 직전·직후)에 프레임이 몰리고 정지 구간은 성기게 뽑힌다. 첫·끝 프레임과
움직임 최대 지점은 항상 넣는다. OpenCV는 함수 안에서 불러와 API 런타임 import를 막는다.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Strategy = Literal["uniform", "adaptive"]


@dataclass(frozen=True)
class Keyframe:
    frame_index: int
    timestamp_ms: int
    jpeg: bytes


def motion_signature(frame):
    """움직임 측정용 축소 흑백 프레임(160×90, 블러). 적응형 키프레임과 trajectory의 충돌 시점 추정이
    같은 신호를 쓰도록 한곳에 둔다."""
    import cv2
    import numpy as np

    small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
    return cv2.GaussianBlur(small, (5, 5), 0).astype(np.float32)


def motion_curve(frames: list) -> list[float]:
    """연속 프레임의 평균 절대 차분. 첫 프레임은 0."""
    import numpy as np

    curve = [0.0]
    prev = None
    for frame in frames:
        small = motion_signature(frame)
        if prev is not None:
            curve.append(float(np.abs(small - prev).mean()))
        prev = small
    return curve


def pick_uniform(n_frames: int, k: int) -> list[int]:
    if n_frames <= k:
        return list(range(n_frames))
    return sorted({round(i * (n_frames - 1) / (k - 1)) for i in range(k)})


def pick_adaptive(curve: list[float], k: int) -> list[int]:
    """누적 움직임을 k-1 등분하는 프레임 + 첫/끝/최대 움직임 프레임."""
    n = len(curve)
    if n <= k:
        return list(range(n))
    total = sum(curve)
    if total <= 0:
        return pick_uniform(n, k)
    peak = max(range(n), key=lambda i: curve[i])
    chosen = {0, n - 1, peak}
    # 고정 3장(첫·끝·최대 움직임)을 빼고 남은 장수만큼 누적 움직임을 등분한다
    targets = [total * j / (k - 2) for j in range(1, k - 2)]
    acc = 0.0
    t = 0
    for i, value in enumerate(curve):
        acc += value
        while t < len(targets) and acc >= targets[t]:
            chosen.add(i)
            t += 1
    picks = sorted(chosen)
    # 겹침으로 k보다 적으면 가장 긴 빈 구간의 가운데를 채운다
    while len(picks) < k:
        gaps = [(picks[i + 1] - picks[i], i) for i in range(len(picks) - 1)]
        gap, i = max(gaps)
        if gap < 2:
            break
        picks.insert(i + 1, picks[i] + gap // 2)
    return picks[:k]


def extract_keyframes(video_path: Path, k: int = 10, strategy: Strategy = "adaptive",
                      max_width: int = 768, jpeg_quality: int = 85,
                      window_sec: tuple[float, float] | None = None) -> list[Keyframe]:
    """window_sec=(start, end)를 주면 그 구간 안에서만 고른다(충돌 전 구간 집중용).
    구간이 k프레임보다 짧으면 있는 프레임을 전부 쓴다."""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    if not frames:
        raise ValueError(f"no frames: {video_path}")
    offset = 0
    if window_sec is not None:
        start = max(0, int(window_sec[0] * fps))
        end = min(len(frames), int(window_sec[1] * fps) + 1)
        if end - start >= 2:
            frames, offset = frames[start:end], start
    k = min(k, len(frames))
    if strategy == "adaptive":
        picks = pick_adaptive(motion_curve(frames), k)
    else:
        picks = pick_uniform(len(frames), k)
    out = []
    for local in picks:
        frame = frames[local]
        idx = local + offset
        h, w = frame.shape[:2]
        if w > max_width:
            frame = cv2.resize(frame, (max_width, round(h * max_width / w)))
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
        if not ok:
            raise ValueError(f"jpeg encode failed: {video_path}#{idx}")
        out.append(Keyframe(idx, round(idx * 1000 / fps), bytes(buf)))
    return out


def signal_crops(video_path: Path, collision_sec: float | None, n: int = 3,
                 top_ratio: float = 0.55, width: int = 1536,
                 jpeg_quality: int = 90) -> list[Keyframe]:
    """신호등 확인용 확대 이미지. 충돌 n초 전부터 1초 간격으로 화면 상단(top_ratio)을
    원본 해상도에 가깝게 잘라 낸다. 키프레임을 768px로 줄이면 신호등이 몇 픽셀로
    뭉개지는 문제를 보완한다."""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    anchor = collision_sec if collision_sec is not None else total / fps / 2
    crops: list[Keyframe] = []
    for back in range(n, 0, -1):
        idx = int(max(0.0, anchor - back) * fps)
        idx = min(max(idx, 0), max(total - 1, 0))
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        crop = frame[: int(h * top_ratio), :]
        if w > width:
            crop = cv2.resize(crop, (width, int(crop.shape[0] * width / w)),
                              interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        crops.append(Keyframe(frame_index=idx, timestamp_ms=int(idx / fps * 1000),
                              jpeg=buf.tobytes()))
    cap.release()
    return crops
