"""최종 움직임 B(촬영 차량) 분류기 학습: AI Hub 라벨 + 사람 라벨(무게 20). 무료.

사용:
    .venv/Scripts/python evals/vision/train_motion_b.py

근거(2026-11-09, exp_human_mix.py, 사람 정답 92건 5묶음 교차 검증, 한 번만 잼):
  분류기 + 사람 라벨 75% vs 카메라 규칙 67% vs 전부 직진 59%. 우리 30건에서 VLM 14/30 vs 20/30.
  움직임 A는 사람 라벨을 섞어도 전부 직진과 같아(48% vs 49%) 분류기를 쓰지 않는다(VLM + 항상 확인).
- 사람 라벨: 구준휘님 64건(evals/vision/labels/human_labels_2026-11-09.jsonl) + 우리 정답 카드
  30건
- 특징: 움직임 특징 + CLIP 장면(PCA 20) + 장소(AI Hub 라벨 = 담당자 확인 장소). 카드에서는 장소별
  예측을 미리 계산
- 저장: data/private/models/motion_movement_B.joblib (움직임 A 모델은 쓰지 않도록 이름을 바꿔 둔다)
"""

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from exp_human_mix import EN, ensure, four  # noqa: E402
from make_answer_sheet import prefill  # noqa: E402
from score import load_answers  # noqa: E402
from train_motion import video_features  # noqa: E402

from workers.vision.motion_model import MODEL_DIR, place_onehot  # noqa: E402
from workers.vision.scene_embed import embed_video, scene_vector  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
# 구준휘님 사람 라벨(scored 64건)
HUMAN_LABELS = ROOT / "evals/vision/labels/human_labels_2026-11-09.jsonl"
HUMAN_WEIGHT = 20


def main():
    import joblib
    import numpy as np
    from sklearn.decomposition import PCA
    from sklearn.ensemble import HistGradientBoostingClassifier

    with (RESULTS / "labels.csv").open(encoding="utf-8-sig") as f:
        labels = {r["video_name"]: r for r in csv.DictReader(f)}
    human = {
        n: g["movement_B"]
        for n, g in load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx").items()
    }
    jh = [
        json.loads(x) for x in HUMAN_LABELS.open(encoding="utf-8")
    ]
    jh_all = {r["example_id"] for r in jh}
    human.update({r["example_id"]: EN[r["movement_ego"]] for r in jh if r["status"] == "scored"})
    for n in human:
        ensure(labels[n])
    with (RESULTS / "train_answers.csv").open(encoding="utf-8-sig") as f:
        silver_ans = {r["video_name"]: r for r in csv.DictReader(f)}
    #  사람 라벨이 있는 영상은 AI Hub 라벨 대신 사람 라벨로(구준휘님 라벨 중 제외·중복 표시 영상은
    # 아예 뺀다)
    silver = [
        n for n in silver_ans if n not in human and n not in jh_all and silver_ans[n]["movement_B"]
    ]
    names = silver + sorted(human)
    feats = {n: video_features(n) for n in names}
    names = [n for n in names if feats[n]]
    place = {n: prefill(labels[n]).get("location_type") for n in names}
    y = np.array(
        [four(human[n]) if n in human else four(silver_ans[n]["movement_B"]) for n in names]
    )
    w = np.array([HUMAN_WEIGHT if n in human else 1.0 for n in names])
    columns = sorted({k for n in names for k in feats[n]})
    places = sorted({place[n] for n in names if place[n]})
    Z = np.array([scene_vector(embed_video("", n)) for n in names])
    pca = PCA(20, random_state=0).fit(Z)
    X = np.hstack(
        [
            np.array([[feats[n].get(k, np.nan) for k in columns] for n in names], dtype=float),
            pca.transform(Z),
            np.array([place_onehot(place[n], places) for n in names]),
        ]
    )
    model = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.05, max_leaf_nodes=15, l2_regularization=1.0, random_state=0
    ).fit(X, y, sample_weight=w)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "columns": columns,
            "field": "movement_B",
            "n": len(y),
            "pca": pca,
            "classes": list(model.classes_),
            "places": places,
            "b_classes": [],
            "human_labels": sum(n in human for n in names),
            "human_weight": HUMAN_WEIGHT,
        },
        MODEL_DIR / "motion_movement_B.joblib",
    )
    a = MODEL_DIR / "motion_movement_A.joblib"
    if (
        a.exists()
    ):  # 움직임 A 분류기는 쓰지 않는다(사람 정답에서 전부 직진과 같음). 지우지 않고 이름만 바꿈
        a.replace(MODEL_DIR / "motion_movement_A.unused_ai_hub_only.joblib")
    print(
        f"움직임 B 학습: {len(y)}건 (사람 라벨 {int((w > 1).sum())}건, 무게 {HUMAN_WEIGHT}) → "
        f"{MODEL_DIR / 'motion_movement_B.joblib'}"
    )


if __name__ == "__main__":
    main()
