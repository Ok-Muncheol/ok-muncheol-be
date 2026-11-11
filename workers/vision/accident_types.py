"""A: 사고 유형표(accident_type_map.csv)로 사실 카드 칸을 채우고, VLM이 고를 유형 후보를 만든다.

- type_fields: 유형 한 줄(장소·장소특징·A/B 진행·신호) → 카드 칸 값. 정답 입력표 미리 채우기와 같은
  규칙.
- candidates: 장소(와 1인칭이면 카메라로 본 B 움직임)에 맞는 유형 후보 목록. 블랙박스 차가 유형의
  A일 수도
  B일 수도 있으므로(AI Hub 1인칭의 61%가 A/B 반대) 두 방향을 모두 후보로 만든다.
카드 기준은 늘 '블랙박스 차 = B'다.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

TYPE_MAP = Path(__file__).resolve().parents[2] / "evals/vision/accident_type_map.csv"
PLACE_MAP = {
    "직선 도로": "직선도로",
    "사거리교차로(신호등있음)": "사거리_신호있음",
    "사거리교차로(신호등없음)": "사거리_신호없음",
    "T자형교차로": "T자형교차로",
    "회전교차로": "회전교차로",
    "고속도로(자동차전용도로)포함": "고속도로",
    "주차장(또는차도가아닌장소)": "주차장",
    "차도와차도가아닌장소": "차도와비차도",
}
SIGNAL_MAP = {
    "녹색신호": "녹색",
    "황색신호": "황색",
    "적색신호": "적색",
    "녹색 좌회전 신호": "녹색좌회전",
    "녹색좌회전신호": "녹색좌회전",
    "녹색 직진·좌회전 신호": "녹색직진좌회전",
    "무신호": "신호없음",
}
# 앞에 있을수록 먼저 본다 (예: "후진 출자"는 출차)
MOVE_WORDS = [
    ("출자", "출차"),
    ("유턴", "유턴"),
    ("회전교차로 진입", "회전교차로진입"),
    ("교차로 내 회전", "회전교차로회전"),
    ("회전(회전", "회전교차로회전"),
    ("좌회전", "좌회전"),
    ("우회전", "우회전"),
    ("진로변경", "진로변경"),
    ("차로로 변경", "진로변경"),
    ("합류", "합류"),
    ("추월", "추월"),
    ("출발", "정차후출발"),
    ("주(정)차", "주정차"),
    ("주 (정)차", "주정차"),
    ("후진", "후진"),
    ("추돌", "직진"),
    ("직진", "직진"),
]
INTERSECTIONS = {"사거리_신호있음", "사거리_신호없음", "T자형교차로", "회전교차로"}
# 유형 고르기로 정하는 칸(유형에서 값이 나오는 칸만 실제로 덮어쓴다)
TYPE_FIELDS = (
    "location_type",
    "road_priority",
    "movement_A",
    "movement_B",
    "entry_order",
    "signal_A",
    "signal_B",
    "relative_position",
    "centerline_cross_A",
    "centerline_cross_B",
    "lane_change_A",
    "lane_change_B",
)


def movement(text: str) -> str:
    text = text.replace("(선진입)", "").replace("(후진입)", "")  # "후진입"을 "후진"으로 읽지 않게
    for word, value in MOVE_WORDS:
        if word in text:
            return value
    return ""


def signal(map_value: str, text: str) -> str:
    if map_value:
        return SIGNAL_MAP.get(map_value, "")
    for k, v in SIGNAL_MAP.items():
        if f"[{k}]" in text:
            return v
    return ""


def _leading(text: str) -> bool | None:
    """이 차가 앞(선행)이면 True, 뒤(후행)면 False, 모르면 None.

    '선행 차량 추돌'은 '앞차를 들이받음'이라 이 차가 뒤다(2026-11-09 버그 수정: 전에는 앞으로 읽음).
    """
    if text.startswith("후행"):
        return False
    if text.startswith("선행"):
        return False if "추돌" in text else True
    return None


def type_fields(
    place_raw: str, feat: str, a: str, b: str, sig_a: str = "", sig_b: str = ""
) -> dict:
    """유형표 한 줄(이미 '블랙박스 차=B' 방향으로 맞춘 A/B) → 카드 칸 값. 알 수 없는 칸은 넣지
    않는다."""
    place = PLACE_MAP.get(place_raw, "")
    v = {"accident_object": "차대차", "party_count": 2, "location_type": place}
    if place and place not in INTERSECTIONS:
        v["road_priority"] = "해당없음"
    elif "동일폭" in feat:
        v["road_priority"] = "동일폭"
    elif "대로와 소로" in feat:
        v["road_priority"] = "A대로" if "대로" in a else "B대로" if "대로" in b else ""
    v["signal_A"] = signal(sig_a, a)
    v["signal_B"] = signal(sig_b, b)
    if place == "사거리_신호없음":
        v["signal_A"] = v["signal_A"] or "신호없음"
        v["signal_B"] = v["signal_B"] or "신호없음"
    v["movement_A"], v["movement_B"] = movement(a), movement(b)
    if "마주보며" in a or "마주보며" in b:
        v["relative_position"] = "맞은편"
    elif a.startswith(("후행", "선행")) or b.startswith(("후행", "선행")):
        la, lb = _leading(a), _leading(b)
        a_behind = la is False or lb is True
        v["relative_position"] = "같은방향_뒤" if a_behind else "같은방향_앞"
    if "(선진입)" in a or "(후진입)" in b:
        v["entry_order"] = "A먼저"
    elif "(후진입)" in a or "(선진입)" in b:
        v["entry_order"] = "B먼저"
    elif place in ("직선도로", "고속도로") and "합류" not in a + b:
        v["entry_order"] = "해당없음"
    for side, text in (("A", a), ("B", b)):
        if "중앙선" in text:
            v[f"centerline_cross_{side}"] = "있음"
        if v[f"movement_{side}"] == "진로변경":
            v[f"lane_change_{side}"] = "있음"
    return {k: x for k, x in v.items() if x != ""}


@lru_cache(maxsize=1)
def load_types(path: str = str(TYPE_MAP)) -> tuple[dict, ...]:
    with open(path, encoding="utf-8-sig") as f:
        return tuple(r for r in csv.DictReader(f) if r["사고대상"] in ("차대차", "고속도로"))


def candidates(
    location_type: str | None, ego_movement: str | None = None, all_places: bool = False
) -> list[dict]:
    """장소에 맞는 유형 후보. 각 후보 = 유형 1개 × 방향(블랙박스 차가 유형의 B / A).

    ego_movement(카메라로 본 B 움직임)가 있으면 B 움직임이 같은 후보만 남긴다(남는 게 없으면 거르지
    않음).
    location_type이 없거나 모르는 값이면 교차로 전체에서 고른다. all_places=True면 모든 장소.
    """
    if all_places:
        places = set(PLACE_MAP.values())
    else:
        places = {location_type} if location_type in PLACE_MAP.values() else INTERSECTIONS
    out, seen = [], set()
    for t in load_types():
        place = PLACE_MAP.get(t["사고장소"], "")
        if place not in places:
            continue
        for swap in (False, True):
            a, b = (t["B진행"], t["A진행"]) if swap else (t["A진행"], t["B진행"])
            sa, sb = (t["B신호"], t["A신호"]) if swap else (t["A신호"], t["B신호"])
            key = (place, t["장소특징"], a, b)
            if key in seen or not a or not b:
                continue
            seen.add(key)
            fields = type_fields(t["사고장소"], t["장소특징"], a, b, sa, sb)
            out.append(
                {
                    "type": t["accident_type"],
                    "swap": swap,
                    "place": place,
                    "feature": t["장소특징"],
                    "A": a,
                    "B": b,
                    "signal_A": sa,
                    "signal_B": sb,
                    "fields": fields,
                }
            )
    if ego_movement:
        kept = [c for c in out if c["fields"].get("movement_B") == ego_movement]
        if kept:
            out = kept
    for i, c in enumerate(out, 1):
        c["id"] = i
    return out


# ---------- 점수로 유형 고르기 (VLM에게 후보 목록을 읽히지 않음) ----------

PRIORS = Path(__file__).resolve().parent / "type_priors.json"
#  칸이 맞을 때 더하는 점수. 장소·카메라로 본 B 움직임은 믿을 만해서 크게 준다(2026-11-06 개선용
# 기준)
WEIGHTS = {
    "location_type": 3.0,
    "movement_B_ego": 3.0,
    "movement_B": 1.0,
    "movement_A": 1.5,
    "relative_position": 1.0,
    "entry_order": 1.0,
    "road_priority": 1.0,
    "signal_A": 0.7,
    "signal_B": 0.7,
    "centerline_cross_A": 0.5,
    "centerline_cross_B": 0.5,
    "lane_change_A": 0.5,
    "lane_change_B": 0.5,
}
#  흔한 유형 가산점(log 빈도)에 곱하는 값. 1.0이면 1인칭 유형 11번(319건)으로 쏠려 0/10(2026-11-06)
# → 낮춤
PRIOR_WEIGHT = 0.2


@lru_cache(maxsize=1)
def load_priors(path: str = str(PRIORS)) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    import json

    return json.loads(p.read_text(encoding="utf-8")).get("counts", {})


def score_candidates(
    observed: dict,
    viewpoint: int,
    ego_movement: str | None = None,
    prior_weight: float | None = None,
) -> list[dict]:
    """모든 장소의 유형 후보에 점수를 매겨 높은 순으로 돌려준다.

    observed: 1차 VLM 답 {칸: {"value", "confidence"}}. 값이 '불명'이거나 없으면 그 칸은 점수에 쓰지
    않는다.
    점수 = Σ(칸이 맞으면 가중치 × VLM 확신도) + 카메라 B 움직임 일치 + PRIOR_WEIGHT × log(빈도+1)
    """
    import math

    counts = load_priors().get(str(viewpoint), {})
    out = []
    for c in candidates(None, all_places=True):
        f, s, why = c["fields"], 0.0, []
        for name, w in WEIGHTS.items():
            if name == "movement_B_ego":
                if viewpoint == 1 and ego_movement and f.get("movement_B") == ego_movement:
                    s += w
                    why.append("카메라B")
                continue
            obs = observed.get(name) or {}
            val, conf = obs.get("value"), obs.get("confidence")
            if val in (None, "불명") or name not in f:
                continue
            if f[name] == val:
                s += w * (conf if isinstance(conf, (int, float)) else 0.7)
                why.append(name)
        prior = counts.get(f"{c['type']}|{c['swap']}", 0)
        s += (PRIOR_WEIGHT if prior_weight is None else prior_weight) * math.log(prior + 1)
        out.append({**c, "score": round(s, 3), "matched": why, "prior": prior})
    out.sort(key=lambda c: -c["score"])
    return out


def describe(c: dict) -> str:
    sig = ""
    if c["signal_A"] or c["signal_B"]:
        sig = f" / 신호: A {c['signal_A'] or '-'}, B {c['signal_B'] or '-'}"
    return (
        f"[{c['id']}] {c['place']}, {c['feature']} / A(상대 차): {c['A']}"
        f" / B(블랙박스 차): {c['B']}{sig}"
    )
