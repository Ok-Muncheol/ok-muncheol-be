"""A: 움직임 분류기(작은 학습 모델). YOLO 상대 차 궤적 + 카메라 흐름 특징으로 움직임 A·B를
맞힌다(2026-11-09).

VLM은 학습하지 않는다. 이 모델만 라벨 기반 정답(AI Hub 사고 유형 → 칸, 사람 정답과 움직임 90%
일치)으로 학습한다.
왜: 사람이 규칙으로 엮은 방법(꼬리표, 깊이 3D)은 잡음 때문에 실패했다. 이런 숫자 조합은 데이터로
배우는 편이 낫다.

특징 기준 시점(anchor): 카메라 계산이 찾은 충돌 순간, 없으면 75. 학습과 서비스가 같은 기준을 써야
한다.
학습: evals/vision/train_motion.py. 모델 파일: data/private/models/motion_<칸>.joblib (git 제외)
"""

from __future__ import annotations

import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "data/private/models"
EGO_WIDTH = 320
WINDOWS = ((-45, -30), (-30, -15), (-15, 0), (0, 15))  # 충돌 순간 기준 구간(프레임)
CLASSES = ("car", "truck", "bus", "motorcycle")


def anchor_frame(ego) -> int:
    return ego.collision_frame or 75


def features(det: dict, ego, track_a: list, why: dict) -> dict:
    """영상 하나 → {특징 이름: 숫자(없으면 nan)}."""
    import numpy as np

    nan = float("nan")
    a = anchor_frame(ego)
    width, height = det["size"]
    dx = np.array(ego.series["dx"])
    mag = np.array(ego.series["mag"])
    radial = np.array(ego.series["radial"])
    by_frame = {p[0]: p for p in track_a}
    # 고른 근거(크기 비율 등)는 고르는 방식(규칙/학습)마다 뜻이 달라 넣지 않는다
    f = {"anchor": a, "n_tracks": len(det["tracks"]), "has_track": float(bool(track_a))}
    for c in CLASSES:
        f[f"cls_{c}"] = float(why.get("cls") == c)
    #  영상 전체를 일정 구간(15프레임)으로 나눈 카메라·상대 차 숫자(충돌 순간 기준이 어긋나도 모델이
    # 스스로 찾게)
    for j, s in enumerate(range(1, 150, 15)):
        seg = slice(s - 1, s + 14)
        f[f"g_ego_turn_{j}"] = float(dx[seg].sum() / EGO_WIDTH) if len(dx[seg]) else nan
        f[f"g_ego_mag_{j}"] = float(np.median(mag[seg])) if len(mag[seg]) else nan
        pts = [by_frame[k] for k in range(s, s + 15) if k in by_frame]
        f[f"g_a_cx_{j}"] = float(np.mean([(p[1] + p[3] / 2) / width for p in pts])) if pts else nan
        f[f"g_a_area_{j}"] = (
            float(np.mean([math.log(max(p[3] * p[4], 1) / (width * height)) for p in pts]))
            if pts
            else nan
        )
    for i, (s, e) in enumerate(WINDOWS):
        lo, hi = max(0, a + s - 1), max(0, a + e - 1)  # dx[k]: 프레임 k+1 → k+2
        seg = slice(lo, hi)
        cam = float(dx[seg].sum() / EGO_WIDTH) if hi > lo else nan
        f[f"ego_turn_{i}"] = cam
        f[f"ego_mag_{i}"] = float(np.median(mag[seg])) if hi > lo else nan
        f[f"ego_fwd_{i}"] = float(np.median(radial[seg])) if hi > lo else nan
        pts = [by_frame[k] for k in range(a + s, a + e + 1) if k in by_frame]
        f[f"a_seen_{i}"] = len(pts) / max(1, e - s + 1)
        if len(pts) >= 2:
            cx = [(p[1] + p[3] / 2) / width for p in pts]
            cy = [(p[2] + p[4]) / height for p in pts]  # 박스 아래(바닥 닿는 곳)
            area = [math.log(max(p[3] * p[4], 1) / (width * height)) for p in pts]
            asp = [p[3] / max(p[4], 1) for p in pts]
            f[f"a_cx_{i}"], f[f"a_cy_{i}"] = float(np.mean(cx)), float(np.mean(cy))
            f[f"a_area_{i}"], f[f"a_asp_{i}"] = float(np.mean(area)), float(np.mean(asp))
            f[f"a_dcx_{i}"] = cx[-1] - cx[0]
            f[f"a_dcx_own_{i}"] = (
                cx[-1] - cx[0] - (cam if cam == cam else 0.0)
            )  # 카메라 회전 뺀 이동
            f[f"a_dcy_{i}"] = cy[-1] - cy[0]
            f[f"a_darea_{i}"] = area[-1] - area[0]
            f[f"a_dasp_{i}"] = asp[-1] - asp[0]
        else:
            for k in ("cx", "cy", "area", "asp", "dcx", "dcx_own", "dcy", "darea", "dasp"):
                f[f"a_{k}_{i}"] = nan
    return f


def motion_opinions(det: dict, ego, track_a: list, why: dict, emb=None) -> dict:
    """카드 조립용: {"movement_A": 예측, "movement_B": 예측}. '기타'이거나 모델이 없으면 그 칸은
    빠진다.

    2026-11-09 라벨 1,230건 교차 검증: 움직임 B 분류기(기타면 카메라 규칙) 72% vs 규칙만 62%.
    움직임 A 57%(찍기 52%), VLM은 개선용 10건에서 실행마다 5·3·3/10.
    """
    feats = features(det, ego, track_a, why)
    bundle_a, bundle_b = _bundle("movement_A"), _bundle("movement_B")
    if bundle_b is None or bundle_b.get("places") is None:
        return {}
    # 장소에 따라 달라진다: 장소 8가지 각각의 예측을 미리 계산해 둔다.
    #  카드는 VLM이 본 장소의 예측을 쓰고, 담당자가 장소를 고치면 그 장소의 예측으로 바꾼다(다시
    # 계산 없음).
    #  2026-11-09 최종: 움직임 B만 쓴다(사람 정답 92건 75% vs 규칙 67%). 움직임 A 분류기는 사람
    # 정답에서
    #  전부 직진과 같아 모델 파일을 두지 않는다(있으면 A도 계산: A는 같은 장소의 B 확률을 입력으로
    # 씀).
    by_a, by_b = {}, {}
    for place in bundle_b["places"]:
        pb = predict("movement_B", feats, emb, place_onehot(place, bundle_b["places"]))
        by_b[place] = {k: v for k, v in pb.items() if k != "all"}
        if bundle_a is not None and bundle_a.get("places") is not None:
            b_all = [pb["all"].get(c, 0.0) for c in bundle_a["b_classes"]]
            pa = predict("movement_A", feats, emb, place_onehot(place, bundle_a["places"]) + b_all)
            by_a[place] = {k: v for k, v in pa.items() if k != "all"}
    out = {"movement_B": {"by_place": by_b}}
    if by_a:
        out["movement_A"] = {"by_place": by_a}
    return out


_BUNDLES: dict = {}


def _bundle(field: str):
    import joblib

    if field not in _BUNDLES:
        path = MODEL_DIR / f"motion_{field}.joblib"
        _BUNDLES[field] = joblib.load(path) if path.exists() else None
    return _BUNDLES[field]


def place_onehot(place: str | None, places: list) -> list[float]:
    return [float(place == p) for p in places]


def predict(field: str, feats: dict, emb=None, extra: list | None = None) -> dict | None:
    """학습한 모델로 {"value", "confidence", "probs"(상위 3), "all"(전체)}. 모델 파일이 없으면 None.

    emb: scene_embed.embed_video 결과(CLIP). 모델이 CLIP 숫자로 학습됐으면(bundle['pca']) 줄여서
    붙인다.
    extra: 움직임 A 모델의 맥락 입력(장소 원핫 + 움직임 B 확률, train_motion.py와 같은 순서).
    """
    import numpy as np

    bundle = _bundle(field)
    if bundle is None:
        return None
    x = [feats.get(k, float("nan")) for k in bundle["columns"]]
    if bundle.get("pca") is not None:
        from workers.vision.scene_embed import scene_vector

        z = (
            bundle["pca"].transform([scene_vector(emb)])[0]
            if emb is not None
            else np.full(bundle["pca"].n_components_, np.nan)
        )
        x += list(z)
    if bundle.get("places") is not None:
        n_extra = len(bundle["places"]) + len(bundle.get("b_classes") or [])
        x += list(extra) if extra is not None else [float("nan")] * n_extra
    probs = bundle["model"].predict_proba([x])[0]
    order = probs.argsort()[::-1]
    classes = bundle["model"].classes_
    return {
        "value": str(classes[order[0]]),
        "confidence": round(float(probs[order[0]]), 2),
        "probs": {str(classes[i]): round(float(probs[i]), 2) for i in order[:3]},
        "all": {str(c): float(p) for c, p in zip(classes, probs, strict=False)},
    }
