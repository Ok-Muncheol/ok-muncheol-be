"""A: YOLO로 차·신호등을 찾고 추적한다. 1인칭 영상에서 상대 차(A) 한 대를 고른다. 학습 없음.

- detect_and_track: 영상 → 차 궤적(번호별)과 신호등 박스. 결과를 JSON으로 저장해 두고 다시 쓴다.
- pick_opponent: 차 궤적 중 상대 차 하나를 규칙으로 고른다(충돌 순간 근처에서 가장 크게 보인 차).
박스 형식은 정답 박스와 같다: (frame, x, y, w, h) 픽셀, (x, y)는 왼쪽 위, frame은 1부터.
→ box_features.box_opinions에 그대로 넣을 수 있다.

픽셀 이동으로 실제 속도·과속을 확정하지 않는다. 모델은 COCO로 학습된 기본 가중치를 그대로 쓴다.
ultralytics·torch는 비전 환경(.venv)에만 있으므로 함수 안에서 불러온다.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "data/private/models"
DEFAULT_MODEL = "yolo11s.pt"
VEHICLES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}  # COCO 번호
TRAFFIC_LIGHT = 9

PICK_RULES = {
    "min_boxes": 5,          # 이보다 적게 잡힌 차는 후보에서 뺀다(잠깐 스친 차, 잘못 잡힌 것)
    "hood_bottom": 0.97,     # 박스 아래가 화면 이 비율 아래에 붙고
    "hood_width": 0.5,       # 폭이 화면 절반 이상인 박스가 대부분이면 내 차 보닛으로 본다
    "search": (40, 110),     # 충돌 순간을 모를 때 찾는 프레임 범위(box_features와 같음)
    "around_hit": (-15, 5),  # 카메라 계산으로 충돌 순간을 알면 그 앞 15 ~ 뒤 5 프레임에서 찾는다
}


def _load_model(name: str):
    from ultralytics import YOLO
    from ultralytics.utils.downloads import attempt_download_asset

    path = MODEL_DIR / name
    if not path.exists():
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        attempt_download_asset(str(path))  # 처음 한 번 내려받음(data/private/models, git 제외)
    return YOLO(str(path))


def detect_and_track(video_path: str, cache_path: str | Path | None = None, model: str = DEFAULT_MODEL,
                     conf: float = 0.25) -> dict:
    """영상 → {"model", "size", "frames", "tracks": {번호: {"cls", "boxes"}}, "lights": [박스...]}.

    cache_path가 있고 파일이 있으면 다시 돌리지 않고 읽는다. 없으면 돌린 뒤 저장한다.
    tracks의 boxes: [(frame, x, y, w, h, 신뢰도)]. lights: [(frame, x, y, w, h, 신뢰도)] (추적 번호 없음).
    """
    if cache_path and Path(cache_path).exists():
        return json.loads(Path(cache_path).read_text(encoding="utf-8"))

    yolo = _load_model(model)
    tracks: dict[str, dict] = {}
    lights: list = []
    size, n = None, 0
    results = yolo.track(source=str(video_path), stream=True, persist=True, tracker="bytetrack.yaml",
                         classes=[*VEHICLES, TRAFFIC_LIGHT], conf=conf, verbose=False)
    for n, r in enumerate(results, start=1):
        size = size or (r.orig_shape[1], r.orig_shape[0])
        b = r.boxes
        if b is None or len(b) == 0:
            continue
        ids = b.id.int().tolist() if b.id is not None else [None] * len(b)
        for (x1, y1, x2, y2), cls, c, tid in zip(b.xyxy.tolist(), b.cls.int().tolist(), b.conf.tolist(), ids):
            box = [n, round(x1, 1), round(y1, 1), round(x2 - x1, 1), round(y2 - y1, 1), round(c, 3)]
            if cls == TRAFFIC_LIGHT:
                lights.append(box)
            elif tid is not None:
                t = tracks.setdefault(str(tid), {"cls": VEHICLES[cls], "boxes": []})
                t["boxes"].append(box)
    out = {"model": model, "conf": conf, "size": list(size or (0, 0)), "frames": n,
           "tracks": tracks, "lights": lights}
    if cache_path:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def _is_hood(boxes: list, width: int, height: int) -> bool:
    hood = [b for b in boxes if (b[2] + b[4]) / height > PICK_RULES["hood_bottom"]
            and b[3] / width > PICK_RULES["hood_width"]]
    return len(hood) > len(boxes) / 2


STITCH = {"max_gap": 12, "max_dist": 0.15}  # 끊긴 추적 잇기: 프레임 간격, 화면 폭 대비 중심 거리


def stitch_back(det: dict, tid: str) -> list:
    """고른 차 궤적의 시작 직전에 근처에서 끝난 다른 번호를 같은 차로 보고 앞에 이어 붙인다(여러 번 반복).

    추적기가 충돌 순간(가림·흔들림)에 차를 놓쳤다가 새 번호로 다시 잡는 경우가 있다(개선용 3건).
    """
    width = det["size"][0]
    boxes = sorted(tuple(b[:5]) for b in det["tracks"][tid]["boxes"])
    used = {tid}
    while True:
        f0, x0, y0, w0, h0 = boxes[0]
        cx0 = (x0 + w0 / 2) / width
        best = None
        for other, t in det["tracks"].items():
            if other in used or not t["boxes"]:
                continue
            last = max(t["boxes"], key=lambda b: b[0])
            gap = f0 - last[0]
            dist = abs((last[1] + last[3] / 2) / width - cx0)
            if 0 < gap <= STITCH["max_gap"] and dist <= STITCH["max_dist"]:
                if best is None or gap < best[0]:
                    best = (gap, other)
        if best is None:
            return boxes
        used.add(best[1])
        boxes = sorted([tuple(b[:5]) for b in det["tracks"][best[1]]["boxes"] if b[0] < f0] + boxes)


def pick_opponent(det: dict, hit_frame: int | None = None, stitch: bool = False) -> tuple[list, dict]:
    """상대 차 궤적 [(frame, x, y, w, h)]와 고른 근거. 후보가 없으면 ([], 근거).

    규칙: min_boxes 이상 잡힌 차 중 보닛이 아닌 것, 찾는 범위 안에서 박스가 가장 컸던 차.
    hit_frame(카메라 계산의 충돌 순간)이 있으면 그 근처(around_hit)에서 찾는다.
    """
    width, height = det["size"]
    if hit_frame:
        lo, hi = hit_frame + PICK_RULES["around_hit"][0], hit_frame + PICK_RULES["around_hit"][1]
    else:
        lo, hi = PICK_RULES["search"]
    scored = []
    for tid, t in det["tracks"].items():
        boxes = t["boxes"]
        if len(boxes) < PICK_RULES["min_boxes"] or _is_hood(boxes, width, height):
            continue
        inside = [b for b in boxes if lo <= b[0] <= hi]
        if not inside:
            continue
        peak = max(inside, key=lambda b: b[3] * b[4])
        scored.append((peak[3] * peak[4] / (width * height), tid, peak[0]))
    why = {"window": [lo, hi], "candidates": len(scored)}
    if not scored:
        return [], why
    scored.sort(reverse=True)
    area, tid, frame = scored[0]
    t = det["tracks"][tid]
    why.update({"track": tid, "cls": t["cls"], "peak_area": round(area, 3), "peak_frame": frame,
                "n_boxes": len(t["boxes"]),
                "runner_up_area": round(scored[1][0], 3) if len(scored) > 1 else None})
    if stitch:
        boxes = stitch_back(det, tid)
        why["stitched_from"] = boxes[0][0]
        return boxes, why
    return [tuple(b[:5]) for b in t["boxes"]], why


# 카드에 넘기는 박스 판단 칸. 2026-11-09 개선용 10건(YOLO 상대 차): 충돌 부위 B 9/10(VLM 5/10).
# 상대 위치(4/10 vs VLM 8/10)·충돌 순간(2/10 vs 6/10)은 VLM이 나아서 넘기지 않는다.
BOX_FIELDS = ("collision_part_B",)


def box_detections(video_path: str, cache_path: str | Path | None = None, anchor: int | None = None) -> dict:
    """1인칭 영상 → 카드 조립(extract_fact_card_sync)에 넘길 {"box_opinions", "yolo_info"}.

    상대 차를 못 찾으면 box_opinions가 비어 VLM 값만 쓰인다(그 칸은 확인 표시).
    anchor(카메라 계산의 충돌 순간)를 주면 학습한 상대 차 고르기(opponent_pick)를 쓴다. 이어 붙이기는 하지 않는다
    (충돌 부위는 박스가 가장 커지는 순간만 보면 되고, 9/10을 낸 방식과 같게).
    """
    from workers.vision.box_features import box_opinions
    from workers.vision.opponent_pick import pick

    det = detect_and_track(video_path, cache_path=cache_path)
    track_a, why = pick(det, anchor, stitch=False) if anchor is not None else pick_opponent(det)
    width, height = det["size"]
    ops = box_opinions(track_a, width, height).get("opinions", {}) if track_a else {}
    return {"box_opinions": {k: v for k, v in ops.items() if k in BOX_FIELDS},
            "yolo_info": {**why, "model": det.get("model")}}


SURE_RULES = {"min_area": 0.03, "over_runner_up": 1.5}


def pick_is_sure(why: dict) -> bool:
    """고른 상대 차가 확실한가: 화면의 3% 이상으로 보였고, 2등 후보보다 1.5배 이상 컸다.

    D안(사진에 상대 차 표시)에서 애매하면 그리지 않으려고 쓴다. 2026-11-09 개선용 10건 기준으로 정함:
    그리는 7건 중 1건(211010)이 틀린 차, 맞게 고르고도 애매해서 안 그리는 2건(150729, 160225).
    """
    area, runner = why.get("peak_area"), why.get("runner_up_area")
    if not area or area < SURE_RULES["min_area"]:
        return False
    return runner is None or area >= runner * SURE_RULES["over_runner_up"]


def light_fact(det: dict, until_frame: int = 90) -> str:
    """VLM 질문에 붙일 신호등 탐지 사실(iFinder처럼 해석된 사실, 결론은 주지 않음). 2026-11-09 시험.

    배경: VLM이 신호 없는 곳(T자 등)을 '사거리_신호있음'으로 본 영상 2건에서 YOLO는 신호등을 1~2프레임만 잡았다.
    충돌 순간을 아직 모르므로 영상 앞쪽 until_frame까지 본다(충돌은 보통 70~80번).
    """
    s = light_summary(det, until_frame=until_frame)
    head = f"[자동 탐지(YOLO), 틀릴 수 있음] 신호등: #1~#{until_frame} 중 "
    if not s["visible"]:
        return head + "잡히지 않음"
    # 크기는 주지 않는다: 화면 높이의 30% 넘는 '신호등'은 표지판 등을 잘못 잡은 것으로 보여 오히려 헷갈리게 함
    return head + f"{s['frames']}프레임에서 잡힘"


def light_summary(det: dict, until_frame: int | None = None, min_height: float = 0.01) -> dict:
    """신호등이 보였나: until_frame(충돌 순간)까지, 화면 위쪽 절반에서, 높이가 화면의 min_height 이상인 신호등.

    신호 B 판단 보조(C안): 하나도 없으면 VLM이 말한 신호 색을 믿지 않고 '불명'으로 둔다.
    """
    width, height = det["size"]
    seen = [b for b in det["lights"]
            if (until_frame is None or b[0] <= until_frame) and (b[2] + b[4] / 2) < height / 2
            and b[4] / height >= min_height]
    biggest = max(seen, key=lambda b: b[3] * b[4]) if seen else None
    return {"visible": bool(seen), "n_boxes": len(seen),
            "frames": len({b[0] for b in seen}), "biggest": biggest}
