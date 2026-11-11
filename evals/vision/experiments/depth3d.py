"""A: 깊이 추정 + 카메라 움직임으로 상대 차(A)의 실제(위에서 본) 궤적을 어림하고 A의 움직임을
판단한다(시험, 2026-11-09).

왜: 화면 속 2D 위치만으로는 내 차가 돌거나 움직이면 상대 차 방향이 반대로 보인다(VLM4D, 우리 꼬리표
실험 160225).
iFinder는 SLAM과 깊이 모델로 3D 위치를 구했다. 여기서는 가볍게:
- 깊이: Depth Anything V2 Metric Outdoor Small(미터 단위, 학습 없이 그대로). 박스 가운데 부분의 깊이
  중앙값 = A까지 거리
- 옆 위치: 화면 가로 위치와 거리로 계산(카메라 화각 HFOV 가정, 어안 왜곡은 무시)
- 내 차 회전: ego_motion의 광학 흐름(dx)
- 내 차 전진: 도로 위 점을 몇 프레임 따라가 그 점들의 깊이가 줄어든 양(SLAM 대신)
좌표: x 오른쪽, z 앞(첫 프레임의 내 차 방향 기준), 미터. 학습 없음. 기준값은 사람이 정한 숫자.
torch·transformers는 비전 환경(.venv)에만 있으므로 함수 안에서 불러온다.
"""

from __future__ import annotations

import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODEL = "depth-anything/Depth-Anything-V2-Metric-Outdoor-Small-hf"
HFOV_DEG = 110.0  # 블랙박스 가로 화각 가정(보통 100~130도)
EGO_WIDTH = 320  # ego_motion 계산 화면 폭
STEP = 3  # 몇 프레임마다 깊이를 잴지
SPAN = 45  # 충돌 순간 앞 몇 프레임을 볼지(3초)
EDGE = 0.01  # 박스가 화면 가장자리에서 이 비율 안이면 잘린 것으로 보고 뺀다
RULES = {"turn_deg": 30.0, "still_mps": 1.0, "min_points": 4}
_PIPE = None


def _pipe():
    global _PIPE
    if _PIPE is None:
        import os

        os.environ.setdefault(
            "HF_HOME", str(ROOT / "data/private/models/hf")
        )  # 모델 파일은 git 제외 폴더에
        from transformers import pipeline

        _PIPE = pipeline("depth-estimation", model=MODEL, device=0)
    return _PIPE


def _depth(img):
    import cv2
    import numpy as np
    from PIL import Image

    out = _pipe()(Image.fromarray(img[:, :, ::-1]))["predicted_depth"]
    d = out.squeeze().cpu().numpy() if hasattr(out, "cpu") else np.array(out)
    return cv2.resize(d.astype("float32"), (img.shape[1], img.shape[0]))


def _ego_forward(img0, img1, d0, d1, boxes, H) -> float | None:
    """img0→img1 사이 내 차가 앞으로 간 거리(m). 화면 아래쪽 도로 점을 따라가 깊이가 줄어든 양의
    중앙값."""
    import cv2
    import numpy as np

    g0, g1 = cv2.cvtColor(img0, cv2.COLOR_BGR2GRAY), cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    mask = np.zeros_like(g0)
    mask[int(H * 0.45) : int(H * 0.8), :] = 255  # 도로가 보이는 아래쪽(보닛·글자 제외)
    for _, x, y, w, h in boxes:  # 차는 움직이므로 뺀다
        mask[int(y) : int(y + h), int(x) : int(x + w)] = 0
    pts = cv2.goodFeaturesToTrack(g0, 200, 0.01, 10, mask=mask)
    if pts is None:
        return None
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(g0, g1, pts, None)
    dz = []
    for p, q, ok in zip(pts.reshape(-1, 2), nxt.reshape(-1, 2), st.reshape(-1)):
        if not ok or not (0 <= q[0] < g1.shape[1] and 0 <= q[1] < g1.shape[0]):
            continue
        z0, z1 = d0[int(p[1]), int(p[0])], d1[int(q[1]), int(q[0])]
        if 1 < z0 < 60 and 1 < z1 < 60:
            dz.append(z0 - z1)
    return float(np.median(dz)) if len(dz) >= 10 else None


def opponent_world_track(
    video_path: str, track_a: list, ego_dx: list, hit_frame: int, all_boxes: dict | None = None
) -> list[dict]:
    """A의 위에서 본 궤적 [{"frame", "x", "z", "dist"}]. 내 차 첫 위치·방향 기준 미터.

    all_boxes: {frame: [(frame,x,y,w,h), ...]} 모든 차 박스(전진 거리 잴 때 차 영역 빼기용).
    """
    import cv2

    by_frame = {p[0]: p for p in track_a}
    start = max(1, hit_frame - SPAN)
    frames = [f for f in range(start, hit_frame + 1, STEP)]
    cap = cv2.VideoCapture(video_path)
    imgs, depths = {}, {}
    for f in frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f - 1)
        ok, img = cap.read()
        if ok:
            imgs[f], depths[f] = img, _depth(img)
    if not imgs:
        return []
    H, W = next(iter(imgs.values())).shape[:2]
    fx = (W / 2) / math.tan(math.radians(HFOV_DEG / 2))
    yaw, ex, ez = 0.0, 0.0, 0.0  # 내 차 방향(왼쪽 +), 위치
    out, prev = [], None
    for f in frames:
        if f not in imgs:
            continue
        if prev is not None:
            # 회전: 배경이 오른쪽으로 흐르면(dx>0) 왼쪽으로 돈 것
            px = (
                sum(ego_dx[k - 1] for k in range(prev, f) if 0 <= k - 1 < len(ego_dx))
                * W
                / EGO_WIDTH
            )
            yaw += math.atan2(px, fx)
            fwd = (
                _ego_forward(
                    imgs[prev], imgs[f], depths[prev], depths[f], (all_boxes or {}).get(prev, []), H
                )
                or 0.0
            )
            fwd = max(
                0.0, fwd
            )  # 후진은 드물어 0으로 막는다(깊이 잡음이 뒤로 가는 것처럼 보이는 것 방지)
            ex += -math.sin(yaw) * fwd
            ez += math.cos(yaw) * fwd
        prev = f
        b = by_frame.get(f) or next(
            (by_frame[g] for g in (f - 1, f + 1, f - 2, f + 2) if g in by_frame), None
        )
        if b is None:
            continue
        _, x, y, w, h = b
        if x < W * EDGE or x + w > W * (1 - EDGE):
            continue  # 화면에 잘린 차는 깊이·위치가 튄다
        d = depths[f][int(y + h * 0.3) : int(y + h * 0.8), int(x + w * 0.25) : int(x + w * 0.75)]
        if d.size == 0:
            continue
        import numpy as np

        z_rel = float(np.median(d))
        x_rel = ((x + w / 2) - W / 2) / fx * z_rel
        # 내 차 좌표 → 첫 프레임 기준 좌표(왼쪽 회전 yaw만큼 돌림)
        xw = ex + x_rel * math.cos(yaw) - z_rel * math.sin(yaw)
        zw = ez + x_rel * math.sin(yaw) + z_rel * math.cos(yaw)
        out.append(
            {
                "frame": f,
                "x": round(xw, 2),
                "z": round(zw, 2),
                "dist": round(z_rel, 1),
                "ego": (round(ex, 2), round(ez, 2), round(math.degrees(yaw), 1)),
            }
        )
    return out


def classify_movement(traj: list[dict], fps: int = 15) -> dict:
    """궤적 → {"value", "confidence", "why"}. 앞 1/3과 뒤 1/3의 진행 방향 차이로 회전, 속도로 정지를
    본다."""
    if len(traj) < RULES["min_points"]:
        return {"value": None, "confidence": 0.0, "why": {"points": len(traj)}}
    import numpy as np

    n = len(traj)
    #  깊이가 프레임마다 튀므로 x(t), z(t)를 2차 곡선으로 맞춰 매끄럽게 한 뒤 처음·끝 진행 방향을
    # 비교한다
    t = np.array([p["frame"] for p in traj], dtype=float)
    t = (t - t[0]) / fps
    cx = np.polyfit(t, [p["x"] for p in traj], 2)
    cz = np.polyfit(t, [p["z"] for p in traj], 2)
    vx, vz = np.polyder(cx), np.polyder(cz)
    h0 = math.degrees(math.atan2(np.polyval(vx, t[0]), np.polyval(vz, t[0])))
    h1 = math.degrees(math.atan2(np.polyval(vx, t[-1]), np.polyval(vz, t[-1])))
    x0, x1 = np.polyval(cx, t[0]), np.polyval(cx, t[-1])
    z0, z1 = np.polyval(cz, t[0]), np.polyval(cz, t[-1])
    total = math.hypot(x1 - x0, z1 - z0)
    seconds = t[-1] - t[0]
    speed = total / seconds if seconds > 0 else 0.0
    turn = (h1 - h0 + 180) % 360 - 180  # 오른쪽 +
    why = {
        "points": n,
        "speed_mps": round(speed, 1),
        "heading0": round(h0),
        "heading1": round(h1),
        "turn_deg": round(turn),
    }
    if speed < RULES["still_mps"]:
        return {"value": "주정차", "confidence": 0.5, "why": why}
    if turn <= -RULES["turn_deg"]:
        return {"value": "좌회전", "confidence": 0.5, "why": why}
    if turn >= RULES["turn_deg"]:
        return {"value": "우회전", "confidence": 0.5, "why": why}
    return {"value": "직진", "confidence": 0.5, "why": why}
