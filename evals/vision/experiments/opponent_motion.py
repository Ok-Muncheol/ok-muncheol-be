"""A: 상대 차(A)의 구간별 움직임 꼬리표를 코드로 만든다(시험, 2026-11-09). 학습 없음.

근거: TCogMap(arXiv 2512.05277)은 내 차 움직임을 구간마다 해석된 꼬리표('#40~#55 좌회전')로 VLM에
줘서
학습 없이 +9~18%p를 얻었다. 우리 9번 실험(카메라 계산)도 같은 효과였다. 여기서는 같은 방식을 상대
차에 쓴다.
숫자 궤적(2·4·7번 실험, 효과 없음)이 아니라 해석된 말로 준다. 움직임 결론('A는 좌회전')은 주지
않는다
(결론을 주면 VLM이 그대로 따라 함, 2026-11-06).

입력: YOLO 상대 차 궤적 [(frame, x, y, w, h)], 카메라 흐름(ego_motion series['dx'], 축소 화면 320폭
기준 픽셀/프레임).
카메라 보정: 멀리 있는 배경은 카메라가 돈 만큼 화면에서 옮겨 간다. 상대 차가 화면에서 옮겨 간 양에서
그만큼을 빼
상대 차 자체의 옆 이동을 어림한다(가까운 차는 시차 때문에 오차가 있음).
"""

from __future__ import annotations

SEG = 10  # 구간 길이(프레임)
EGO_WIDTH = 320  # ego_motion 계산 화면 폭
TAG_RULES = {
    "lateral": 0.04,  # 구간 동안 카메라 보정한 옆 이동(화면 폭 비율)이 이보다 크면 가로지름
    "grow": 1.25,  # 박스 넓이가 이 배 이상 커지면 가까워짐, 1/이 값 이하면 멀어짐
    "aspect": 1.2,  # 가로세로비(가로/세로)가 이 배 이상 커지면 가로형이 됨(옆면이 보이기 시작)
}


def _side(cx: float) -> str:
    return "화면 왼쪽" if cx < 0.38 else "화면 오른쪽" if cx > 0.62 else "화면 가운데"


def opponent_tags(
    track_a: list, size: tuple, ego_dx: list | None, start: int, end: int
) -> list[dict]:
    """start~end 프레임을 SEG 단위로 나눠 구간마다 꼬리표. 박스가 2개 미만인 구간은 '안 보임'."""
    width, height = size
    by_frame = {p[0]: p for p in track_a}
    out = []
    for s in range(start, end, SEG):
        e = min(s + SEG, end)
        pts = [by_frame[f] for f in range(s, e + 1) if f in by_frame]
        if len(pts) < 2:
            out.append({"frames": (s, e), "seen": False})
            continue
        a, b = pts[0], pts[-1]
        cx = lambda p: (p[1] + p[3] / 2) / width
        move = cx(b) - cx(a)
        cam = 0.0
        if ego_dx:
            # dx[k]는 프레임 k+1 → k+2 흐름. 같은 구간의 배경 이동(화면 폭 비율)
            cam = (
                sum(ego_dx[f - 1] for f in range(a[0], b[0]) if 0 <= f - 1 < len(ego_dx))
                / EGO_WIDTH
            )
        own = move - cam
        area_a, area_b = a[3] * a[4], b[3] * b[4]
        grow = area_b / area_a if area_a > 0 else 1.0
        asp_a, asp_b = a[3] / max(a[4], 1), b[3] / max(b[4], 1)
        out.append(
            {
                "frames": (a[0], b[0]),
                "seen": True,
                "side": _side(cx(b)),
                "own_move": round(own, 3),
                "cam_move": round(cam, 3),
                "grow": round(grow, 2),
                "aspect": (round(asp_a, 2), round(asp_b, 2)),
            }
        )
    return out


def describe(tags: list[dict]) -> str:
    """꼬리표를 VLM 질문에 붙일 글로. 결론(움직임 이름)은 쓰지 않는다."""
    lines = [
        "[상대 차(A) 움직임 기록: 코드 계산(자동 탐지·추적). 카메라(B)가 돈 만큼은 빼고 어림한 값이며 틀릴 수 있다]"
    ]
    for t in tags:
        s, e = t["frames"]
        if not t["seen"]:
            lines.append(f"- #{s}~#{e}: A가 잡히지 않음")
            continue
        parts = [t["side"]]
        own = t["own_move"]
        if own > TAG_RULES["lateral"]:
            parts.append("오른쪽으로 옮겨 감")
        elif own < -TAG_RULES["lateral"]:
            parts.append("왼쪽으로 옮겨 감")
        else:
            parts.append("옆 이동 거의 없음")
        g = t["grow"]
        parts.append(
            "가까워짐"
            if g >= TAG_RULES["grow"]
            else "멀어짐"
            if g <= 1 / TAG_RULES["grow"]
            else "거리 비슷"
        )
        a0, a1 = t["aspect"]
        if a1 >= a0 * TAG_RULES["aspect"]:
            parts.append("박스가 가로로 넓어짐(옆면이 더 보임)")
        elif a1 <= a0 / TAG_RULES["aspect"]:
            parts.append("박스가 좁아짐(앞이나 뒤가 더 보임)")
        lines.append(f"- #{s}~#{e}: " + ", ".join(parts))
    return "\n".join(lines)
