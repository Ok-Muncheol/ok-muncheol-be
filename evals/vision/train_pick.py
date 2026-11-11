"""상대 차 고르기 모델(workers/vision/opponent_pick.py)을 학습·평가한다. 무료(CPU).

사용:
    .venv/Scripts/python evals/vision/train_pick.py

정답: AI Hub 이미지 라벨의 상대 차 박스(gt_tracks.load_gt_tracks의 'A', 블랙박스 차=B 기준)와
같은 프레임끼리 겹침 중앙값이 0.5 이상인 YOLO 추적 번호 = 상대 차.
평가: 영상 단위 5겹(같은 영상이 학습·평가에 섞이지 않게). 지금 규칙(pick_opponent)과 같은 영상에서 비교.
개선용 10건도 따로 비교(학습에 넣지 않음).
"""

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_boxes import MATCH_IOU, track_iou  # noqa: E402
from gt_tracks import load_gt_tracks  # noqa: E402
from silver import ego_cached  # noqa: E402
from workers.vision.detection import detect_and_track, pick_opponent  # noqa: E402
from workers.vision.opponent_pick import MODEL_PATH, pick_learned, track_features  # noqa: E402

RESULTS = ROOT / "evals/results/vision"


def video_data(row: dict):
    """(특징 {번호: dict}, 정답 번호 집합, det, anchor) 또는 None(정답 박스 없음)."""
    name = row["video_name"]
    det = detect_and_track("", cache_path=RESULTS / "yolo" / f"{name}.json")
    gt = load_gt_tracks(row, ROOT)[0].get("A", [])
    if not gt or not det["tracks"]:
        return None
    anchor = ego_cached(name).collision_frame or 75
    feats = track_features(det, anchor)
    pos = set()
    for tid, t in det["tracks"].items():
        m, k = track_iou(gt, [tuple(b[:5]) for b in t["boxes"]])
        if m is not None and m >= MATCH_IOU and k >= 1:
            pos.add(tid)
    return feats, pos, det, anchor, gt


def right(track, gt) -> bool:
    m, _ = track_iou(gt, track) if track else (None, 0)
    return m is not None and m >= MATCH_IOU


def main():
    import joblib
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.model_selection import GroupKFold

    with (RESULTS / "train_set.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    data = {}
    for r in rows:
        d = video_data(r)
        if d:
            data[r["video_name"]] = d
    columns = sorted({k for d in data.values() for v in d[0].values() for k in v})
    has_pos = [n for n, d in data.items() if d[1]]
    print(f"정답 박스 있는 영상 {len(data)}건 중 YOLO가 상대 차를 잡은 영상 {len(has_pos)}건 "
          f"({len(has_pos) / len(data) * 100:.0f}%)")

    X, y, g, tids = [], [], [], []
    names = list(data)
    for gi, n in enumerate(names):
        feats, pos = data[n][0], data[n][1]
        for tid, v in feats.items():
            X.append([v.get(k, np.nan) for k in columns])
            y.append(int(tid in pos))
            g.append(gi)
            tids.append(tid)
    X, y, g = np.array(X, float), np.array(y), np.array(g)

    prob = np.zeros(len(y))
    for tr, te in GroupKFold(5).split(X, y, g):
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                           l2_regularization=1.0, random_state=0).fit(X[tr], y[tr])
        prob[te] = m.predict_proba(X[te])[:, 1]

    learned_ok = rule_ok = 0
    oof = {}  # 교차 검증 예측으로 고른 번호(그 영상을 안 본 모델). 움직임 분류기 학습에 쓴다(점수 부풀림 방지)
    for gi, n in enumerate(names):
        idx = np.where(g == gi)[0]
        best = idx[prob[idx].argmax()]
        oof[n] = tids[best]
        learned_ok += y[best] == 1
        det, anchor, gt = data[n][2], data[n][3], data[n][4]
        track, _ = pick_opponent(det)
        rule_ok += right(track, gt)
    N = len(names)
    print(f"상대 차를 맞게 고름 (영상 {N}건, 영상 단위 교차 검증):")
    print(f"  지금 규칙(가장 크게 보인 차): {rule_ok}/{N} ({rule_ok / N * 100:.0f}%)")
    print(f"  학습한 모델:                 {learned_ok}/{N} ({learned_ok / N * 100:.0f}%)")
    print(f"  (YOLO가 상대 차를 아예 못 잡은 영상 {N - len(has_pos)}건은 둘 다 못 맞힘)")

    model = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                           l2_regularization=1.0, random_state=0).fit(X, y)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "columns": columns, "n_videos": N}, MODEL_PATH)
    print(f"→ {MODEL_PATH}")
    import json

    (RESULTS / "pick_oof.json").write_text(json.dumps(oof), encoding="utf-8")

    with (RESULTS / "eval_set.csv").open(encoding="utf-8-sig") as f:
        dev = [r for r in csv.DictReader(f) if r["split"] == "dev"]
    lo = ro = n = 0
    for r in dev:
        d = video_data(r)
        if not d:
            continue
        det, anchor, gt = d[2], d[3], d[4]
        n += 1
        lt = pick_learned(det, anchor, stitch=False)
        lo += right(lt[0] if lt else [], gt)
        ro += right(pick_opponent(det)[0], gt)
    print(f"개선용 {n}건: 지금 규칙 {ro}/{n} | 학습한 모델 {lo}/{n}")


if __name__ == "__main__":
    main()
