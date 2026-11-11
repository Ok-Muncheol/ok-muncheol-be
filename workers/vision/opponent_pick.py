"""A: 상대 차(A) 고르기를 학습한 모델로 한다(2026-11-09 시험). 1인칭 영상.

지금 규칙(detection.pick_opponent: 충돌 무렵 가장 크게 보인 차)은 개선용 8/10. 틀리면 충돌 부위 B·움직임 분류기 등이 함께 무너진다.
AI Hub 이미지 라벨의 상대 차 정답 박스로 'YOLO 추적 번호마다 상대 차인가'를 학습한다(evals/vision/train_pick.py).
영상마다 확률이 가장 높은 번호를 고르고, 끊긴 추적은 detection.stitch_back으로 잇는다.
"""

from __future__ import annotations

import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = ROOT / "data/private/models/opponent_pick.joblib"
CLASSES = ("car", "truck", "bus", "motorcycle")


def track_features(det: dict, anchor: int) -> dict[str, dict]:
    """{추적 번호: 특징}. anchor: 충돌 순간 추정(카메라 계산, 없으면 75)."""
    import numpy as np

    width, height = det["size"]
    full = width * height
    rows = {}
    peaks = {}
    for tid, t in det["tracks"].items():
        b = sorted(t["boxes"], key=lambda x: x[0])
        frames = [x[0] for x in b]
        area = np.array([x[3] * x[4] / full for x in b])
        cx = np.array([(x[1] + x[3] / 2) / width for x in b])
        bottom = np.array([(x[2] + x[4]) / height for x in b])
        near = [i for i, f in enumerate(frames) if abs(f - anchor) <= 10]
        win = [i for i, f in enumerate(frames) if 40 <= f <= 110]
        k = int(area.argmax())
        rows[tid] = {
            "n_boxes": len(b), "first": frames[0], "last": frames[-1], "span": frames[-1] - frames[0],
            "n_win": len(win), "n_near": len(near),
            "peak_area": float(area[k]), "peak_frame": frames[k], "peak_from_anchor": frames[k] - anchor,
            "peak_cx": float(cx[k]), "peak_center_dist": abs(float(cx[k]) - 0.5), "peak_bottom": float(bottom[k]),
            "near_area": float(area[near].max()) if near else 0.0,
            "near_cx": float(cx[near].mean()) if near else float("nan"),
            "win_area": float(area[win].max()) if win else 0.0,
            "mean_conf": float(np.mean([x[5] for x in b])),
            "grow": float(np.log((area[k] + 1e-6) / (area[0] + 1e-6))),
            "dcx": float(cx[-1] - cx[0]),
            "hood": float(np.mean((bottom > 0.97) & (np.array([x[3] / width for x in b]) > 0.5))),
            **{f"cls_{c}": float(t["cls"] == c) for c in CLASSES},
        }
        peaks[tid] = (rows[tid]["near_area"], rows[tid]["win_area"], rows[tid]["peak_area"])
    # 영상 안에서의 순위(다른 차와 비교)
    for key, idx in (("rank_near", 0), ("rank_win", 1), ("rank_peak", 2)):
        order = sorted(peaks, key=lambda t: -peaks[t][idx])
        for r, tid in enumerate(order):
            rows[tid][key] = r
    for tid in rows:
        rows[tid]["n_tracks"] = len(rows)
    return rows


def pick(det: dict, anchor: int | None, stitch: bool = True) -> tuple[list, dict]:
    """상대 차 고르기 진입점: 학습한 모델이 있으면 그것(라벨 1,249건 78%), 없으면 규칙(68%)."""
    from workers.vision.detection import pick_opponent

    res = pick_learned(det, anchor or 75, stitch=stitch)
    return res if res is not None else pick_opponent(det, stitch=stitch)


def pick_learned(det: dict, anchor: int, stitch: bool = True) -> tuple[list, dict] | None:
    """학습한 모델로 상대 차 궤적 [(frame,x,y,w,h)]와 근거. 모델이 없거나 차가 없으면 None."""
    import joblib

    if not MODEL_PATH.exists() or not det["tracks"]:
        return None
    from workers.vision.detection import stitch_back

    bundle = joblib.load(MODEL_PATH)
    feats = track_features(det, anchor)
    tids = list(feats)
    X = [[feats[t].get(k, float("nan")) for k in bundle["columns"]] for t in tids]
    prob = bundle["model"].predict_proba(X)[:, list(bundle["model"].classes_).index(1)]
    order = sorted(range(len(tids)), key=lambda i: -prob[i])
    tid = tids[order[0]]
    why = {"track": tid, "prob": round(float(prob[order[0]]), 2), "cls": det["tracks"][tid]["cls"],
           "runner_up_prob": round(float(prob[order[1]]), 2) if len(order) > 1 else None,
           "peak_frame": feats[tid]["peak_frame"], "peak_area": round(feats[tid]["peak_area"], 3),
           "runner_up_area": None, "method": "learned"}
    boxes = stitch_back(det, tid) if stitch else [tuple(b[:5]) for b in det["tracks"][tid]["boxes"]]
    return boxes, why
