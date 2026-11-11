"""A: 상대 차(A)만 잘라 확대한 사진으로 A의 움직임을 따로 묻는다(시험, 2026-11-09).

배경: 전체 사진으로 물으면 VLM이 A의 움직임을 대부분 '직진'으로 답했다(개선용 틀린 5건 모두 직진).
사진에 박스만 그린 D안은 효과가 없었다(5/10→3/10). 여기서는 YOLO가 고른 상대 차 주변만 원본 해상도로
잘라
시간 순서대로 6칸 한 장에 붙여 보낸다(다른 차·배경 제거, 해상도 높임). 사진 1장이라 1건 약 5원.
"""

from __future__ import annotations

import base64

from workers.vision.fields import MOVES

OFFSETS = (-40, -32, -24, -16, -8, 0)  # 충돌 순간 기준 프레임(약 2.7초 전 ~ 충돌)
TILE = 384
CONTEXT = 2.0  # 박스 크기의 몇 배를 잘라낼지(주변 도로가 조금 보이게)


def crop_grid(video_path: str, track_a: list, hit_frame: int) -> tuple[str | None, list[int]]:
    """상대 차 주변을 잘라 3×2 한 장(base64 JPG)으로. 박스가 3칸 미만이면 (None, [])."""
    import cv2

    by_frame = {p[0]: p for p in track_a}
    cap = cv2.VideoCapture(video_path)
    tiles, used = [], []
    for off in OFFSETS:
        f = hit_frame + off
        near = [by_frame[g] for g in (f, f - 1, f + 1, f - 2, f + 2) if g in by_frame]
        if not near:
            continue
        _, x, y, w, h = near[0][:5]
        cap.set(cv2.CAP_PROP_POS_FRAMES, f - 1)
        ok, img = cap.read()
        if not ok:
            continue
        H, W = img.shape[:2]
        side = max(w, h) * CONTEXT
        side = max(side, 200)
        cx, cy = x + w / 2, y + h / 2
        x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        cv2.rectangle(
            img, (int(x), int(y)), (int(x + w), int(y + h)), (0, 0, 255), 2
        )  # 가는 빨간 테두리: 어느 차인지
        tile = cv2.resize(img[y0:y1, x0:x1], (TILE, TILE))
        cv2.rectangle(tile, (0, 0), (120, 30), (0, 0, 0), -1)
        cv2.putText(tile, f"#{f}", (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        tiles.append(tile)
        used.append(f)
    if len(tiles) < 3:
        return None, []
    while len(tiles) < 6:
        tiles.append(tiles[0] * 0)
    grid = cv2.vconcat([cv2.hconcat(tiles[:3]), cv2.hconcat(tiles[3:6])])
    return base64.b64encode(
        cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 90])[1]
    ).decode(), used


def build_prompt(frames: list[int], ego_summary: str | None) -> str:
    lines = [
        "블랙박스 차(B)의 카메라 영상에서 상대 차(A) 부분만 시간 순서대로 잘라 확대한 사진이다.",
        f"6칸을 왼쪽 위 → 오른쪽 아래 순서로 본다. 각 칸 왼쪽 위 숫자는 프레임 번호(초당 15장): "
        f"{frames}.",
        "가는 빨간 테두리가 A다. 마지막 칸 무렵이 충돌 순간이다.",
        "A의 충돌 직전 움직임을 고른다. 좌회전·우회전은 A 운전자 기준이다.",
        "판단 방법: (1) 칸마다 A의 어느 면(앞, 옆, 뒤)이 보이는지 (2) 그 면이 칸을 지나며 어떻게 "
        "바뀌는지"
        "(차체가 돌면 회전) (3) 주변 차선·도로와 비교해 A가 어느 쪽으로 가는지. 이 순서로 관찰을 "
        "먼저 적는다.",
        "카메라(B)도 움직이므로, 화면에서 A가 옮겨 간 것만으로 판단하지 말고 A 차체의 방향 변화를 "
        "본다.",
        "사진으로 알 수 없으면 '불명'.",
    ]
    if ego_summary:
        lines += ["", "참고:", ego_summary]
    return "\n".join(lines)


SCHEMA = {
    "type": "object",
    "required": ["observations", "movement_A"],
    "properties": {
        "observations": {"type": "string"},
        "movement_A": {
            "type": "object",
            "required": ["value", "confidence"],
            "properties": {"value": {"enum": MOVES}, "confidence": {"type": "number"}},
        },
    },
}


def ask_movement_a(
    vlm, video_path: str, track_a: list, hit_frame: int, ego_summary: str | None = None
) -> dict | None:
    """{"value", "confidence", "observations", "frames", "tokens"} 또는 None(잘라낼 박스 부족)."""
    img, frames = crop_grid(video_path, track_a, hit_frame)
    if img is None:
        return None
    raw, meta = vlm.generate(build_prompt(frames, ego_summary), [img], SCHEMA)
    mv = raw.get("movement_A") or {}
    return {
        "value": mv.get("value"),
        "confidence": mv.get("confidence"),
        "observations": raw.get("observations"),
        "frames": frames,
        "tokens": (meta.get("prompt_tokens"), meta.get("output_tokens")),
        "image": img,
    }
