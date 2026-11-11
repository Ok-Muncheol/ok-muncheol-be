"""A: 차량 궤적(박스 목록)을 VLM이 읽을 수 있게 바꾼다. 2단계(정답 박스)·3단계(YOLO)가 같은 형식을
쓴다.

궤적 형식: {"A": [(frame, x, y, w, h), ...], "B": [...]}  — 픽셀 좌표, 입력표 기준(블랙박스 차=B)
"""

from __future__ import annotations

Track = list[tuple[int, float, float, float, float]]
COLORS = {"A": (0, 0, 255), "B": (255, 0, 0)}  # OpenCV BGR: A 빨강, B 파랑


def _where(x: float) -> str:
    return "왼쪽" if x < 0.35 else "오른쪽" if x > 0.65 else "가운데"


def _size(area: float) -> str:
    return "아주 큼" if area > 0.15 else "큼" if area > 0.04 else "보통" if area > 0.008 else "작음"


def summarize_tracks(
    tracks: dict[str, Track], width: int, height: int, viewpoint: int, source: str = "정답 박스"
) -> str:
    """궤적을 짧은 글로. 위치는 화면 비율(0~1), 크기는 박스 넓이/화면 넓이."""
    lines = [
        f"[{source}] 화면 좌표 x: 0=왼쪽~1=오른쪽, y: 0=위~1=아래. 크기는 화면 대비 박스 "
        f"넓이(클수록 가까움)."
    ]
    for party in ("A", "B"):
        pts = sorted(tracks.get(party, []))
        if not pts:
            if viewpoint == 1 and party == "B":
                lines.append("B(촬영 차량): 블랙박스가 달린 차라 화면에 보이지 않음.")
            else:
                lines.append(f"{party}: 박스 없음(보이지 않거나 표시되지 않음).")
            continue
        steps = []
        for f, x, y, w, h in pts:
            cx, cy, area = (x + w / 2) / width, (y + h / 2) / height, (w * h) / (width * height)
            steps.append((f, cx, cy, area))
        who = "상대 차량" if viewpoint == 1 and party == "A" else "사고 차량"
        path = " → ".join(f"#{f} x{cx:.2f} y{cy:.2f} {_size(a)}({a:.3f})" for f, cx, cy, a in steps)
        lines.append(f"{party}({who}): {path}")
        (f0, x0, _, a0), (f1, x1, _, a1) = steps[0], steps[-1]
        if len(steps) > 1:
            dx = x1 - x0
            move = (
                "거의 그대로"
                if abs(dx) < 0.08
                else ("오른쪽으로" if dx > 0 else "왼쪽으로") + f" {abs(dx):.2f} 이동"
            )
            grow = a1 / a0 if a0 > 0 else 0
            near = "다가옴" if grow > 1.3 else "멀어짐" if grow < 0.77 else "거리 비슷"
            lines.append(
                f"  요약: #{f0}~#{f1} 사이 화면 {_where(x0)}→{_where(x1)}, {move}, 크기 "
                f"{grow:.1f}배({near})."
            )
    if all(tracks.get(p) for p in ("A", "B")):
        common = sorted({p[0] for p in tracks["A"]} & {p[0] for p in tracks["B"]})
        if common:
            f = common[-1]
            a = next(p for p in tracks["A"] if p[0] == f)
            b = next(p for p in tracks["B"] if p[0] == f)
            gap = abs((a[1] + a[3] / 2) - (b[1] + b[3] / 2)) / width
            lines.append(f"  #{f}에서 A·B 가로 간격 {gap:.2f} (화면 비율).")
    return "\n".join(lines)


def boxes_near(tracks: dict[str, Track], frame: int, tolerance: int = 2) -> dict[str, tuple]:
    """frame에서 ±tolerance 안에 있는 가장 가까운 박스."""
    out = {}
    for party, pts in tracks.items():
        near = [p for p in pts if abs(p[0] - frame) <= tolerance]
        if near:
            out[party] = min(near, key=lambda p: abs(p[0] - frame))
    return out


def draw_boxes(img, tracks: dict[str, Track], frame: int) -> None:
    """원본 해상도 이미지에 A·B 박스를 그린다(제자리 수정)."""
    import cv2

    for party, (_, x, y, w, h) in boxes_near(tracks, frame).items():
        color = COLORS[party]
        cv2.rectangle(img, (int(x), int(y)), (int(x + w), int(y + h)), color, 6)
        cv2.putText(
            img, party, (int(x), max(50, int(y) - 12)), cv2.FONT_HERSHEY_SIMPLEX, 2, color, 6
        )
