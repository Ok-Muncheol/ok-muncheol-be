"""시험: 상대 차 모습(crop_embed) 특징을 더하면 움직임 분류기가 나아지나. 무료(로컬 GPU·CPU).

사용:
    .venv/Scripts/python evals/vision/experiments/exp_crop_motion.py

상대 차: 학습 영상은 교차 검증으로 고른 번호(pick_oof.json, 그 영상을 안 본 고르기 모델 → 점수
부풀림 방지),
개선용은 학습한 고르기(opponent_pick.pick). 기준 시점은 움직임 분류기와 같음(카메라 충돌 순간,
없으면 75).
비교: 기존 특징만 vs 기존 + 앞·옆·뒷면 점수 vs 기존 + 앞·옆·뒷면 + 모습 숫자(PCA).
"""

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # evals/vision 공통 도구
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 이 폴더의 실험 모듈
from crop_embed import crop_embeddings, view_scores  # noqa: E402
from silver import ego_cached  # noqa: E402
from train_motion import video_features  # noqa: E402

from workers.vision.detection import detect_and_track, stitch_back  # noqa: E402
from workers.vision.opponent_pick import pick  # noqa: E402
from workers.vision.scene_embed import embed_video, scene_vector  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
VIDEOS = ROOT / "data/private/aihub/videos"


def crops_for(name: str, oof: dict):
    det = detect_and_track("", cache_path=RESULTS / "yolo" / f"{name}.json")
    anchor = ego_cached(name).collision_frame or 75
    if name in oof and oof[name] in det["tracks"]:
        track = stitch_back(det, oof[name])
    else:
        track = pick(det, anchor)[0]
    return crop_embeddings(str(VIDEOS / f"{name}.mp4"), track, anchor, name, tag="__learned")


def main():
    import numpy as np
    from sklearn.decomposition import PCA
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    oof = json.loads((RESULTS / "pick_oof.json").read_text(encoding="utf-8"))
    with (RESULTS / "train_answers.csv").open(encoding="utf-8-sig") as f:
        answers = {r["video_name"]: r for r in csv.DictReader(f)}
    names = list(answers)
    crops = {}
    for i, n in enumerate(names, 1):
        crops[n] = crops_for(n, oof)
        if i % 300 == 0:
            print(f"잘라내기·숫자화 {i}/{len(names)}", flush=True)
    feats = {n: video_features(n) for n in names}
    cols = sorted({k for x in feats.values() if x for k in x})
    views = {n: view_scores(crops[n]) for n in names}
    seen = np.mean([np.isfinite(crops[n][:, 0]).mean() for n in names])
    print(f"상대 차를 잘라낸 비율(6장 중): {seen * 100:.0f}%")

    for field in ("movement_A", "movement_B"):
        ns = [n for n in names if answers[n][field] and feats[n]]
        y = np.array(
            [
                v if v in ("직진", "좌회전", "우회전") else "기타"
                for v in (answers[n][field] for n in ns)
            ]
        )
        base = np.array([[feats[n].get(k, np.nan) for k in cols] for n in ns])
        Z = np.array([scene_vector(embed_video("", n)) for n in ns])
        base = np.hstack([base, PCA(20, random_state=0).fit_transform(Z)])
        V = np.array([views[n] for n in ns])  # (N, 6, 3)
        v_feat = np.hstack([V.reshape(len(ns), -1), (V[:, -1] - V[:, 0])])
        E = np.array([crops[n] for n in ns])  # (N, 6, 512)
        flat = E.reshape(-1, 512)
        ok = np.isfinite(flat[:, 0])
        pca = PCA(8, random_state=0).fit(flat[ok])
        P = np.full((flat.shape[0], 8), np.nan)
        P[ok] = pca.transform(flat[ok])
        e_feat = P.reshape(len(ns), -1)
        cv = StratifiedKFold(5, shuffle=True, random_state=0)
        print(f"\n== {field} ({len(y)}건)")
        for label, X in (
            ("기존 특징", base),
            ("+ 앞·옆·뒷면 점수", np.hstack([base, v_feat])),
            ("+ 앞·옆·뒷면 + 모습 숫자", np.hstack([base, v_feat, e_feat])),
        ):
            m = HistGradientBoostingClassifier(
                max_iter=300,
                learning_rate=0.05,
                max_leaf_nodes=15,
                l2_regularization=1.0,
                random_state=0,
            )
            p = cross_val_predict(m, X, y, cv=cv)
            cls_acc = {c: np.mean(p[y == c] == c) for c in ("좌회전", "우회전")}
            print(
                f"  {label:22} 정확도 {np.mean(p == y) * 100:.1f}% | 좌회전 "
                f"{cls_acc['좌회전'] * 100:.0f}%"
                f" 우회전 {cls_acc['우회전'] * 100:.0f}%"
            )


if __name__ == "__main__":
    main()
