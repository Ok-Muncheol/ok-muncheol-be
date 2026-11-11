"""A: 1인칭 블랙박스의 카메라 움직임(= 촬영 차량 B의 움직임)을 광학 흐름으로 계산한다. 학습 없음.

원리: 블랙박스는 차에 붙어 있으므로 화면 전체가 흐르는 모양이 곧 B의 움직임이다.
- 풍경이 오른쪽으로 흐름 → 좌회전, 왼쪽으로 흐름 → 우회전
- 가운데에서 바깥으로 퍼짐 → 전진, 모임 → 후진, 거의 안 움직임 → 정지
- 갑자기 크게 흔들림 → 충돌 순간
3인칭 영상(카메라가 사고 차가 아님)에는 쓰지 않는다.

판단 기준값(THRESHOLDS)은 학습이 아니라 개선용 영상을 보고 사람이 정한 숫자다.
결과는 사실 카드의 '탐지 의견'(vision_detail.detection)과 VLM 질문의 힌트로 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass

FPS = 15
WIDTH, HEIGHT = 320, 180  # 계산용 축소 크기(속도)
KEEP_TOP = 0.65  # 화면 위 65%만 쓴다. 아래쪽은 보닛·대시보드·글자 표시라 함께 움직이지 않음

THRESHOLDS = {
    # 2026-11-06 개선용 1인칭 7건으로 조정: 실제 좌회전 영상의 회전량이 0.11~0.12, 직진은 0.01~0.03
    "turn": 0.10,        # 충돌 전 구간에서 화면이 가로로 흘러간 양(화면 폭 비율)이 이보다 크면 회전
    "lane_change": 0.08,  # 한쪽으로 이만큼 흘렀다가 되돌아오면 진로변경 후보
    "stop": 0.15,        # 흐름 크기(축소 화면 픽셀/프레임)가 이보다 작으면 정지
    "backward": -0.05,   # 퍼짐 지표가 이보다 작으면(모임) 후진
    "window": 35,        # 충돌 직전 몇 프레임을 보고 움직임을 판단할지(약 2.3초)
    "jolt": 1.8,         # 흔들림 점수가 구간 중앙값의 이 배수를 넘어야 충돌 순간으로 인정
}
SEARCH = (30, 135)  # 충돌 순간을 찾는 프레임 범위


@dataclass
class EgoMotion:
    collision_frame: int | None
    collision_confidence: float
    movement: str
    movement_confidence: float
    motion_trend: str
    yaw: float           # 충돌 전 구간에서 화면이 가로로 흘러간 양(+면 오른쪽 = 좌회전 쪽)
    yaw_peak: float      # 구간 안에서 가장 많이 흘렀을 때의 양(진로변경 판단용)
    forward: float       # 퍼짐 지표(+ 전진, - 후진)
    speed: float         # 흐름 크기 중앙값
    texture: float       # 흐름을 잡을 무늬가 충분했던 비율(낮으면 어둡거나 단조로움)
    series: dict          # 프레임별 값(분석·그림용)

    def summary(self) -> str:
        """VLM 질문에 붙일 짧은 글: 결론 없이 1초 단위 기록만 준다.

        결론('좌회전')을 주면 VLM이 그대로 따라 해서(2026-11-06 실험, 7/7 동일) 독립 의견이 사라진다.
        """
        dx, mag = self.series["dx"], self.series["mag"]
        lines = ["[카메라 움직임 기록: 코드 계산. 1인칭이라 촬영 차량 B 자체의 움직임이다]",
                 "1초 단위로 B가 돈 방향과 움직임 크기(작으면 정지에 가까움):"]
        for start in range(0, len(dx), FPS):
            seg = slice(start, start + FPS)
            turn = sum(dx[seg]) / WIDTH
            speed = sorted(mag[seg])[len(mag[seg]) // 2] if mag[seg] else 0
            if speed < THRESHOLDS["stop"]:
                act = "거의 정지"
            elif abs(turn) > THRESHOLDS["turn"] / 2:
                act = "왼쪽으로 돎" if turn > 0 else "오른쪽으로 돎"
            else:
                act = "곧게 움직임"
            lines.append(f"- {start / FPS:.0f}~{(start + FPS) / FPS:.0f}초(#{start + 1}~#{start + FPS}): {act}"
                         f" (회전량 {turn:+.2f}, 크기 {speed:.2f})")
        if self.collision_frame:
            lines.append(f"- 화면이 갑자기 크게 튀는 곳: #{self.collision_frame} 근처(충돌 후보)")
        if self.texture < 0.5:
            lines.append("- 주의: 화면이 어둡거나 무늬가 적어 계산이 부정확할 수 있음")
        lines.append("이 기록은 참고용이다. 사진과 맞지 않으면 사진을 따르라.")
        return "\n".join(lines)

    def decide_at(self, collision_frame: int | None) -> "EgoMotion":
        """충돌 순간을 바꿔(예: VLM이 찾은 값) 그 직전 구간으로 움직임을 다시 판단한다."""
        return _decide(self.series, self.collision_frame, self.collision_confidence,
                       collision_frame or self.collision_frame)

    def field_opinions(self, location_type: str | None = None) -> dict:
        """사실 카드 '탐지 의견' 형식. 칸 이름 → {"value", "confidence"}.

        location_type(VLM이 본 장소)이 회전교차로이면 회전을 '회전교차로진입'으로 바꿔 부른다.
        """
        from workers.vision.fields import frame_to_ms

        movement = self.movement
        if location_type == "회전교차로" and movement in ("좌회전", "우회전"):
            movement = "회전교차로진입"
        ops = {
            "movement_B": {"value": movement, "confidence": self.movement_confidence},
            "motion_trend_B": {"value": self.motion_trend, "confidence": 0.5},
            "lane_change_B": {"value": "있음" if movement == "진로변경" else "없음",
                              "confidence": round(self.movement_confidence * 0.8, 2)},
        }
        if self.collision_frame:
            ops["collision_time_ms"] = {"value": frame_to_ms(self.collision_frame),
                                        "confidence": self.collision_confidence}
        return ops


def flow_series(gray_frames: list) -> dict:
    """연속 프레임 사이 광학 흐름을 계산해 프레임별 지표를 낸다. gray_frames: 같은 크기의 흑백 이미지 목록."""
    import cv2
    import numpy as np

    h, w = gray_frames[0].shape
    rows = int(h * KEEP_TOP)
    ys, xs = np.mgrid[0:rows, 0:w].astype(np.float32)
    cx, cy = w / 2, h * 0.45
    rx, ry = xs - cx, ys - cy
    r = np.sqrt(rx ** 2 + ry ** 2)
    far = r > min(w, h) * 0.1  # 화면 한가운데(소실점 근처)는 퍼짐 계산에서 뺀다
    out = {"dx": [], "dy": [], "mag": [], "radial": [], "texture": [], "diff": []}
    for a, b in zip(gray_frames[:-1], gray_frames[1:]):
        # 앞뒤 프레임 밝기 차이: 충돌 때 화면이 크게 튀면 흐름 계산은 못 따라가도 이 값은 확 커진다
        out["diff"].append(float(np.mean(cv2.absdiff(a[:rows], b[:rows]))))
        flow = cv2.calcOpticalFlowFarneback(a[:rows], b[:rows], None, 0.5, 3, 15, 3, 5, 1.2, 0)
        fx, fy = flow[..., 0], flow[..., 1]
        mag = np.sqrt(fx ** 2 + fy ** 2)
        # 무늬가 있는 곳만(경계가 뚜렷한 곳) 믿는다
        edges = cv2.Laplacian(a[:rows], cv2.CV_32F)
        tex = np.abs(edges) > 4
        use = tex if tex.mean() > 0.02 else np.ones_like(tex)
        radial = (rx * fx + ry * fy) / np.maximum(r, 1)
        out["dx"].append(float(np.median(fx[use])))
        out["dy"].append(float(np.median(fy[use])))
        out["mag"].append(float(np.median(mag[use])))
        out["radial"].append(float(np.median(radial[use & far])) if (use & far).any() else 0.0)
        out["texture"].append(float(tex.mean()))
    return out


def analyze_frames(gray_frames: list) -> EgoMotion:
    import numpy as np

    s = flow_series(gray_frames)
    dx, dy, mag, radial = (np.array(s[k]) for k in ("dx", "dy", "mag", "radial"))
    n = len(dx)
    w = gray_frames[0].shape[1]

    # 충돌 순간: 앞뒤 프레임 차이가 '주변 1초'보다 갑자기 커지는 곳 + 흐름 방향이 갑자기 바뀐 정도
    diff = np.array(s["diff"])
    turn_shake = np.zeros(n)
    turn_shake[1:] = np.hypot(np.diff(dx), np.diff(dy))
    local = np.array([np.median(diff[max(0, i - 15):i + 16]) for i in range(n)])
    shake = diff / (local + 1.0) + turn_shake / (np.median(turn_shake) + 1.0)
    lo, hi = SEARCH[0] - 1, min(SEARCH[1] - 1, n - 1)
    collision, c_conf = None, 0.0
    if hi > lo:
        k = lo + int(np.argmax(shake[lo:hi]))
        ratio = shake[k] / (np.median(shake[lo:hi]) + 1e-6)
        if ratio > THRESHOLDS["jolt"]:
            collision = k + 1  # 흐름 k는 프레임 k+1 → k+2 사이(프레임 번호는 1부터)
            c_conf = float(min(0.9, 0.3 + ratio / 10))

    series = {k: [round(v, 4) for v in s[k]] for k in s} | {"shake": [round(v, 4) for v in shake]}
    return _decide(series, collision, round(c_conf, 2), collision)


def _decide(series: dict, collision: int | None, c_conf: float, window_end: int | None) -> EgoMotion:
    """window_end(충돌 순간) 직전 구간으로 B 움직임·속도 변화를 판단한다. 못 찾았으면 75번 기준."""
    import numpy as np

    dx, mag, radial = (np.array(series[k]) for k in ("dx", "mag", "radial"))
    w = WIDTH
    end = (window_end or 75) - 2
    start = max(0, end - THRESHOLDS["window"])
    win = slice(start, max(start + 1, end))
    cum = np.cumsum(dx[win]) / w
    yaw = float(cum[-1]) if len(cum) else 0.0
    peak = float(cum[np.argmax(np.abs(cum))]) if len(cum) else 0.0
    speed = float(np.median(mag[win]))
    forward = float(np.median(radial[win]))
    half = (win.stop - win.start) // 2
    early, late = float(np.median(mag[win][:max(1, half)])), float(np.median(mag[win][half:] if half else mag[win]))
    texture = float(np.mean(np.array(series["texture"])[win] > 0.02))

    t = THRESHOLDS
    if speed < t["stop"]:
        movement, conf = "주정차", 0.7
    elif early < t["stop"] <= late:
        movement, conf = "정차후출발", 0.6
    elif abs(yaw) > t["turn"]:
        movement, conf = ("좌회전" if yaw > 0 else "우회전"), float(min(0.9, 0.5 + abs(yaw)))
    elif forward < t["backward"]:
        movement, conf = "후진", 0.6
    elif abs(peak) > t["lane_change"] and abs(yaw) < abs(peak) * 0.6:
        movement, conf = "진로변경", 0.5
    else:
        movement, conf = "직진", 0.7
    if texture < 0.5:
        conf = round(conf * 0.6, 2)

    if speed < t["stop"]:
        trend = "정지"
    elif late < early * 0.7:
        trend = "감속"
    elif late > early * 1.3:
        trend = "가속"
    else:
        trend = "일정"

    return EgoMotion(collision, c_conf, movement, round(conf, 2), trend,
                     round(yaw, 3), round(peak, 3), round(forward, 3), round(speed, 3), round(texture, 2), series)


def analyze_video(video_path: str) -> EgoMotion:
    """영상 전체(150프레임)를 축소·흑백으로 읽어 분석한다. 노트북에서 수 초."""
    import cv2

    cap = cv2.VideoCapture(video_path)
    frames = []
    while True:
        ok, img = cap.read()
        if not ok:
            break
        frames.append(cv2.cvtColor(cv2.resize(img, (WIDTH, HEIGHT)), cv2.COLOR_BGR2GRAY))
    if len(frames) < 10:
        raise RuntimeError(f"프레임을 충분히 읽지 못함: {video_path}")
    return analyze_frames(frames)
