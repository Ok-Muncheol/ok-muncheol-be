"""A: 상대 차(A) 박스 위치로 칸을 코드가 판단한다. 1인칭 영상 전용(카메라 = 블랙박스 차 B의 시점).

입력 박스는 정답 박스(실험 2단계)든 YOLO 박스(3단계)든 같은 형식: [(frame, x, y, w, h), ...] 픽셀
좌표.
- 충돌 순간: A 박스가 가장 커지는 프레임(가장 가까워진 때)
- 충돌 부위 B: 그 순간 A 박스가 화면 왼쪽이면 B의 앞왼쪽, 오른쪽이면 앞오른쪽, 가운데면 앞
- 상대 위치(B 기준 A): A가 처음 보인 곳과 다가오는 모양
학습 없음. 기준값(BOX_THRESHOLDS)은 사람이 정한 숫자.
"""

from __future__ import annotations

BOX_THRESHOLDS = {
    "side": 0.38,  # 박스 가운데 x가 이보다 작으면 왼쪽, 1-이 값보다 크면 오른쪽
    "head_on_grow": 3.0,  # 가운데에서 크기가 이 배수 이상 커지면 정면으로 다가옴(맞은편)
    "search": (40, 110),  # 충돌 순간을 찾는 프레임 범위
}


def _center(box, width, height):
    _, x, y, w, h = box
    return (x + w / 2) / width, (y + h / 2) / height, (w * h) / (width * height)


def _side(cx: float) -> str:
    t = BOX_THRESHOLDS["side"]
    return "왼쪽" if cx < t else "오른쪽" if cx > 1 - t else "가운데"


BEFORE_HIT = (-20, -8)  # 충돌 순간 기준 이 범위(약 1.3~0.5초 전)의 위치로 상대 위치를 본다


def relative_before_hit(track_a: list, width: int, height: int, hit_frame: int) -> dict | None:
    """상대 위치(B 기준 A)를 충돌 약 1초 전 위치로 판단한다(시험 규칙, 2026-11-09).

    기존 규칙(box_opinions)은 '처음 보인 곳'을 써서, 영상 시작부터 가운데에 있던 차를 맞은편으로
    봤다.
    충돌 직전 구간에서 박스 가운데 x 평균으로 왼쪽/오른쪽, 가운데면 그 구간에서 커진 정도로
    맞은편/같은방향_앞.
    """
    lo, hi = hit_frame + BEFORE_HIT[0], hit_frame + BEFORE_HIT[1]
    win = sorted(p for p in track_a if lo <= p[0] <= hi)
    if len(win) < 2:
        return None
    cxs = [_center(p, width, height)[0] for p in win]
    cx = sum(cxs) / len(cxs)
    side = _side(cx)
    a0, a1 = _center(win[0], width, height)[2], _center(win[-1], width, height)[2]
    grow = a1 / a0 if a0 > 0 else 1.0
    if side != "가운데":
        value = side
    else:
        value = "맞은편" if grow >= 1.5 else "같은방향_앞"
    return {
        "value": value,
        "confidence": 0.6,
        "why": {"window": [lo, hi], "cx": round(cx, 2), "grow": round(grow, 2)},
    }


def box_opinions(track_a: list, width: int, height: int) -> dict:
    """A 박스 목록 → {칸: {"value", "confidence"}}와 판단 근거. 박스가 2개 미만이면 빈 dict."""
    pts = sorted(track_a)
    if len(pts) < 2:
        return {}
    from workers.vision.fields import frame_to_ms

    lo, hi = BOX_THRESHOLDS["search"]
    window = [p for p in pts if lo <= p[0] <= hi] or pts
    hit = max(window, key=lambda p: p[3] * p[4])  # 가장 큰 박스 = 가장 가까운 순간
    cx_hit, _, area_hit = _center(hit, width, height)
    first = next((p for p in pts if p[0] < hit[0]), pts[0])
    cx0, _, area0 = _center(first, width, height)

    part = {"왼쪽": "앞왼쪽", "오른쪽": "앞오른쪽", "가운데": "앞"}[_side(cx_hit)]
    side0 = _side(cx0)
    grow = area_hit / area0 if area0 > 0 else 1.0
    if side0 != "가운데":
        rel, rel_conf = side0, 0.7
    elif grow >= BOX_THRESHOLDS["head_on_grow"]:
        rel, rel_conf = "맞은편", 0.5
    else:
        rel, rel_conf = "같은방향_앞", 0.5
    ops = {
        "collision_part_B": {"value": part, "confidence": 0.6},
        "relative_position": {"value": rel, "confidence": rel_conf},
        "collision_time_ms": {"value": frame_to_ms(hit[0]), "confidence": 0.5},
    }
    why = {
        "hit_frame": hit[0],
        "hit_cx": round(cx_hit, 2),
        "first_frame": first[0],
        "first_cx": round(cx0, 2),
        "grow": round(grow, 1),
        "n_boxes": len(pts),
    }
    return {"opinions": ops, "why": why}
