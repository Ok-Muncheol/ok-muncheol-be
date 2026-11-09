"""신호등 램프 위치로 신호 상태를 정하는 규칙 기반 검출기.

VLM은 압축된 영상에서 녹색 화살표와 녹색, 적색과 황색을 색으로 구분하지 못한다.
국내 가로형 신호등은 램프 순서가 고정이라(4구: 적·황·좌회전 화살표·녹, 3구: 적·황·녹)
"켜진 램프가 왼쪽에서 몇 번째인가"만 알면 된다.

흐름: YOLO(COCO traffic light)로 화면 상단의 신호등 박스를 찾고 → 가로형 박스만 골라
켜진 램프 덩어리의 가로 위치(0=왼쪽, 1=오른쪽)와 색상으로 상태를 정하고 →
충돌 전 몇 초 동안의 프레임에서 박스 크기·신뢰도로 가중 투표한다.

사용:
    python -m evals.vision.signal_detect --gt data/private/aihub/vision_gt_human_signalized.jsonl \
        --out evals/results/signal_detect.jsonl
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_YOLO = "data/private/models/yolo11n.pt"
TRAFFIC_LIGHT_CLASS = 9  # COCO
MIN_BOX_WIDTH = 24  # px. 이보다 작으면 램프 위치를 읽을 수 없다
MIN_ASPECT = 1.2  # 가로/세로. 세로형·정사각형 박스(후미등, 단일 램프)는 버린다
MAX_BLOB_RATIO = 0.45  # 덩어리가 박스 폭의 이 비율을 넘으면 신호등 램프가 아니다
MAX_CENTER_Y = 0.42  # 박스 중심이 화면 높이의 이 비율보다 아래면 버린다(앞차 후미등)


@dataclass(frozen=True)
class LampVote:
    t_sec: float
    state: str
    weight: float
    box_width: int


def lamp_state(crop) -> tuple[str | None, float]:
    """가로형 신호등 크롭에서 켜진 램프 덩어리를 찾아 (상태, 확신도 0~1)를 돌려준다.

    따뜻한 색(적·황)은 가로 위치 p<0.3이면 red, 아니면 yellow. 초록 계열은 덩어리가 둘이거나
    p<0.72면 green_left_arrow, 아니면 green. 덩어리가 없거나 너무 크면 None."""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]
    hue = hsv[..., 0]
    sat = hsv[..., 1].astype(np.float32)
    val = hsv[..., 2].astype(np.float32)
    lit = val * (0.4 + 0.6 * sat / 255.0)  # 켜진 램프는 밝고 채도가 높다
    thresh = max(float(lit.mean()) * 1.8, 90.0)
    mask = (lit >= thresh).astype(np.uint8)
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = []
    for k in range(1, n):
        area = int(stats[k, cv2.CC_STAT_AREA])
        bw = int(stats[k, cv2.CC_STAT_WIDTH])
        if area < max(4, int(0.01 * h * w)) or bw > MAX_BLOB_RATIO * w:
            continue
        hues = hue[labels == k]
        blobs.append({
            "p": float(centroids[k][0] / w), "area": area,
            "cx": float(centroids[k][0]), "cy": float(centroids[k][1]),
            "warm": float(np.mean((hues < 15) | (hues > 160))),
            "yellow": float(np.mean((hues >= 15) & (hues < 35))),
            "green": float(np.mean((hues >= 35) & (hues < 100))),
        })
    if not blobs:
        return None, 0.0
    blobs.sort(key=lambda b: -b["area"])
    main = blobs[0]
    main["p"] = _housing_position(val, int(main["cy"]), int(main["cx"]))
    strength = float(min(lit.max() / (lit.mean() + 1e-6), 6.0) / 6.0)
    if main["green"] >= max(main["warm"], main["yellow"]):
        greens = [b for b in blobs if b["green"] >= 0.4 and abs(b["cy"] - main["cy"]) < 0.3 * h]
        if len(greens) >= 2 or main["p"] < 0.62:
            return "green_left_arrow", strength
        return "green", strength
    if main["p"] < 0.35:
        return "red", strength
    return "yellow", strength


def _housing_position(val, cy: int, cx: int, dark: float = 90.0) -> float:
    """켜진 램프(cx, cy)가 속한 검은 몸체 안에서의 가로 위치(0~1).

    YOLO 박스는 기둥·옆 표지판·이웃 신호등까지 같이 잡는 일이 많아 박스 기준 위치는 믿을 수 없다.
    램프가 있는 행에서 어두운 픽셀이 이어지는 구간(몸체)을 찾아 그 안에서의 위치를 쓴다.
    램프 자체는 밝으므로 램프 폭만큼은 어두움 조건을 건너뛴다."""
    h, w = val.shape
    y0, y1 = max(0, cy - max(1, h // 8)), min(h, cy + max(1, h // 8) + 1)
    row = val[y0:y1].mean(axis=0)
    is_dark = row < dark
    # 램프 주변의 밝은 픽셀은 몸체로 친다
    lamp_half = max(2, w // 10)
    is_dark[max(0, cx - lamp_half): min(w, cx + lamp_half + 1)] = True
    left = cx
    while left > 0 and is_dark[left - 1]:
        left -= 1
    right = cx
    while right < w - 1 and is_dark[right + 1]:
        right += 1
    span = right - left + 1
    if span < 3 * lamp_half:  # 몸체를 못 찾았으면 박스 기준으로 돌아간다
        return cx / w
    return float((cx - left) / span)


def detect_votes(video_path: Path, collision_sec: float | None, model_name: str = DEFAULT_YOLO,
                 before_sec: float = 4.0, until_sec: float = 0.5, step_sec: float = 0.5,
                 top_ratio: float = 0.6, device: str | None = None) -> list[LampVote]:
    import cv2
    import numpy as np
    from ultralytics import YOLO

    model = YOLO(model_name)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    anchor = collision_sec if collision_sec is not None else total / fps / 2
    votes: list[LampVote] = []
    for t in np.arange(max(0.2, anchor - before_sec), max(0.3, anchor - until_sec), step_sec):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, frame = cap.read()
        if not ok:
            continue
        frame_h = frame.shape[0]
        top = frame[: int(frame_h * top_ratio)]
        result = model.predict(top, imgsz=1280, conf=0.15, classes=[TRAFFIC_LIGHT_CLASS],
                               verbose=False, device=device)[0]
        boxes = result.boxes.xyxy.cpu().numpy()
        confs = result.boxes.conf.cpu().numpy()
        for box, conf in zip(boxes, confs, strict=True):
            x1, y1, x2, y2 = (int(v) for v in box)
            bw, bh = x2 - x1, y2 - y1
            if bw < MIN_BOX_WIDTH or bw / max(bh, 1) < MIN_ASPECT:
                continue
            if (y1 + y2) / 2 > MAX_CENTER_Y * frame_h:
                continue  # 화면 아래쪽 광원은 앞차 후미등일 가능성이 높다
            state, strength = lamp_state(top[y1:y2, x1:x2])
            if state is None:
                continue
            votes.append(LampVote(t_sec=round(float(t), 2), state=state,
                                  weight=float(bw * conf * (0.5 + strength)), box_width=bw))
    cap.release()
    return votes


def decide(votes: list[LampVote]) -> dict:
    """가중 투표. confidence는 1위 상태의 가중치 비율."""
    if not votes:
        return {"state": None, "confidence": 0.0, "votes": {}, "n_boxes": 0, "max_width": 0}
    tally: dict[str, float] = {}
    for v in votes:
        tally[v.state] = tally.get(v.state, 0.0) + v.weight
    total = sum(tally.values())
    state = max(tally, key=lambda k: tally[k])
    return {
        "state": state,
        "confidence": round(tally[state] / total, 3),
        "votes": {k: round(w / total, 3) for k, w in sorted(tally.items())},
        "n_boxes": len(votes),
        "max_width": max(v.box_width for v in votes),
    }


def detect_signal(video_path: Path, collision_sec: float | None, **kwargs) -> dict:
    return decide(detect_votes(video_path, collision_sec, **kwargs))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_YOLO)
    parser.add_argument("--device", default=None)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.gt.read_text().splitlines() if line.strip()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as out:
        for row in rows:
            collision = (row.get("video_quality") or {}).get("collision_sec")
            result = detect_signal(Path(row["video_path"]), collision, model_name=args.model,
                                   device=args.device)
            result["example_id"] = row["example_id"]
            result["gt_signal_B"] = row["fields"].get("signal_B")
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(row["example_id"], result["state"], result["confidence"], result["gt_signal_B"])


if __name__ == "__main__":
    main()
