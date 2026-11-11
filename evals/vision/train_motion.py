"""움직임 분류기(workers/vision/motion_model.py)를 학습하고 평가한다. 무료(CPU, 몇 분).

사용 (먼저 prepare_train.py로 YOLO·카메라 흐름을 계산해 둔다):
    .venv/Scripts/python evals/vision/train_motion.py # 교차 검증 + 학습 + 저장
    .venv/Scripts/python evals/vision/train_motion.py --dev-run evals/results/vision/runs/<실행>   #
    개선용 10건에서 VLM과 비교

평가:
- 학습 데이터(라벨 정답) 5겹 교차 검증: 정확도, 찍기 기준, 확신도별 정확도(확인 표시 기준 정하기용)
- 개선용 10건(사람 정답): 모델 vs VLM (dev는 학습에 넣지 않음)
직진·좌회전·우회전 밖의 움직임은 '기타'로 묶는다(카드에서는 기타면 기존 값: A는 VLM, B는 카메라
규칙).
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from silver import ego_cached  # noqa: E402

from workers.vision.detection import detect_and_track, pick_opponent  # noqa: E402
from workers.vision.motion_model import MODEL_DIR, features, place_onehot, predict  # noqa: E402
from workers.vision.scene_embed import embed_video, scene_vector  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
VIDEOS = ROOT / "data/private/aihub/videos"
TARGETS = ("movement_B", "movement_A")
MAIN_CLASSES = ("직진", "좌회전", "우회전")


_OOF = None
#  상대 차 고르기 방식. 2026-11-09: 학습 고르기(교차 검증 예측)로 바꿔도 움직임 A 58→56%, B
# 75.4→75%로
#  나아지지 않아 규칙 고르기(detection.pick_opponent, 이어 붙이기)를 쓴다. "learned"로 바꾸면 다시
# 시험 가능.
PICK_MODE = "rule"


def video_features(name: str) -> dict | None:
    """상대 차: PICK_MODE='rule'이면 규칙 고르기. 'learned'면 학습 영상은 교차 검증으로 고른 번호
    (pick_oof.json, 그 영상을 안 본 고르기 모델), 그 밖은 학습한 고르기 모델(opponent_pick.pick)."""
    global _OOF
    yolo = RESULTS / "yolo" / f"{name}.json"
    if not yolo.exists() or not (RESULTS / "ego_cache" / f"{name}.json").exists():
        return None
    det = detect_and_track("", cache_path=yolo)
    ego = ego_cached(name)
    if PICK_MODE == "rule":
        track_a, why = pick_opponent(det, stitch=True)
        return features(det, ego, track_a, why)
    if _OOF is None:
        p = RESULTS / "pick_oof.json"
        _OOF = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if name in _OOF and _OOF[name] in det["tracks"]:
        from workers.vision.detection import stitch_back

        tid = _OOF[name]
        track_a = stitch_back(det, tid)
        why = {
            "track": tid,
            "cls": det["tracks"][tid]["cls"],
            "peak_area": None,
            "runner_up_area": None,
        }
    else:
        from workers.vision.opponent_pick import pick

        track_a, why = pick(det, ego.collision_frame)
    return features(det, ego, track_a, why)


def fit_motion(feats: dict, answers: dict, columns: list, verbose: bool = True) -> dict:
    """움직임 B·A 분류기를 학습해 {칸: bundle}을 돌려준다(저장은 하지 않음). feats에 있는 영상만
    쓴다."""
    import numpy as np
    from sklearn.decomposition import PCA
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    places = sorted({answers[n]["location_type"] for n in feats if answers[n]["location_type"]})
    b_oof, b_classes = {}, None  # 움직임 B 교차 검증 확률(움직임 A 입력, 부풀림 없음)
    bundles = {}
    for field in TARGETS:  # B 먼저(A가 B 확률을 입력으로 씀)
        names = [n for n in feats if answers[n][field]]
        raw = [answers[n][field] for n in names]
        # 직진·좌회전·우회전만 구분하고 나머지는 '기타'(카드에서는 기타면 기존 값을 쓴다).
        # 2026-11-09: 8갈래·클래스 균형보다 4갈래·균형 없음이 나았다(움직임 A 54→57%, B 72%)
        y = np.array([v if v in MAIN_CLASSES else "기타" for v in raw])
        X = np.array([[feats[n].get(k, np.nan) for k in columns] for n in names], dtype=float)
        #  CLIP 장면 숫자 20개(PCA로 줄임) 추가: 움직임 B 72.0→75.4%, A 57.1→58.0% (2026-11-09 교차
        # 검증)
        Z = np.array([scene_vector(embed_video(str(VIDEOS / f"{n}.mp4"), n)) for n in names])
        pca = PCA(20, random_state=0).fit(Z)
        X = np.hstack([X, pca.transform(Z)])
        if field == "movement_A":
            #  맥락: 장소(담당자가 확인한 값, 학습은 라벨) + 움직임 B 확률. 사고 유형은 장소·B
            # 움직임에 따라
            # A의 움직임이 정해지는 경우가 많다. 2026-11-09 교차 검증 56.8→65.2%(좌회전 18→46%)
            nan_b = [np.nan] * len(b_classes)
            extra = np.array(
                [
                    place_onehot(answers[n]["location_type"], places) + list(b_oof.get(n, nan_b))
                    for n in names
                ]
            )
            X = np.hstack([X, extra])
        else:
            # 움직임 B도 장소 맥락: 2026-11-09 교차 검증 75.6→78.1%(좌회전 27→51%)
            X = np.hstack(
                [X, np.array([place_onehot(answers[n]["location_type"], places) for n in names])]
            )
        model = HistGradientBoostingClassifier(
            max_iter=300,
            learning_rate=0.05,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            random_state=0,
        )
        proba = cross_val_predict(
            model,
            X,
            y,
            cv=StratifiedKFold(n_splits=5, shuffle=True, random_state=0),
            method="predict_proba",
        )
        classes = np.unique(y)
        if field == "movement_B":
            b_oof = {n: proba[i] for i, n in enumerate(names)}
            b_classes = list(classes)
        if verbose:
            pred, conf = classes[proba.argmax(1)], proba.max(1)
            top, cnt = Counter(y).most_common(1)[0]
            print(f"\n== {field} (라벨 정답 {len(y)}건, 교차 검증)")
            print(
                f"  정확도 {np.mean(pred == y) * 100:.0f}% | 찍기(전부 '{top}') "
                f"{cnt / len(y) * 100:.0f}%"
            )
            for t in (0.6, 0.8):
                m = conf >= t
                if m.any():
                    print(
                        f"  확신도 ≥{t}: {m.mean() * 100:.0f}%의 영상, 그 정확도 "
                        f"{np.mean(pred[m] == y[m]) * 100:.0f}%"
                    )
            for c in classes:
                m = y == c
                print(f"    {c:10} 정답 {m.sum():4}건 → 맞힘 {np.mean(pred[m] == c) * 100:3.0f}%")
        model.fit(X, y)
        bundles[field] = {
            "model": model,
            "columns": columns,
            "field": field,
            "n": len(y),
            "pca": pca,
            "classes": list(classes),
            "places": places,
            "b_classes": b_classes if field == "movement_A" else [],
        }
    return bundles


def main():
    import joblib

    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-run", type=Path, help="개선용 10건 VLM 실행 폴더(비교용)")
    args = parser.parse_args()

    with (RESULTS / "train_answers.csv").open(encoding="utf-8-sig") as f:
        answers = {r["video_name"]: r for r in csv.DictReader(f)}
    feats = {}
    for name in answers:
        x = video_features(name)
        if x is not None:
            feats[name] = x
    columns = sorted({k for x in feats.values() for k in x})
    print(f"특징을 만든 영상 {len(feats)}/{len(answers)}건, 특징 {len(columns)}개")

    from score import load_answers

    gold = load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx")
    with (RESULTS / "eval_set.csv").open(encoding="utf-8-sig") as f:
        dev = [r["video_name"] for r in csv.DictReader(f) if r["split"] == "dev"]

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    bundles = fit_motion(feats, answers, columns)
    for field, bundle in bundles.items():
        joblib.dump(bundle, MODEL_DIR / f"motion_{field}.joblib")
    places, b_classes = bundles["movement_A"]["places"], bundles["movement_A"]["b_classes"]
    for field in TARGETS:
        # 개선용 10건(사람 정답, 학습에 없음). 움직임 A는 담당자가 확인한 장소(정답 장소)로
        ok_m = ok_v = n = 0
        for name in dev:
            x = video_features(name)
            truth = gold.get(name, {}).get(field)
            if x is None or not truth:
                continue
            emb = embed_video(str(VIDEOS / f"{name}.mp4"), name)
            place_x = place_onehot(gold[name]["location_type"], places)
            extra = place_x
            if field == "movement_A":
                pb = predict("movement_B", x, emb, place_x)
                extra = place_x + [pb["all"].get(c, 0.0) for c in b_classes]
            p = predict(field, x, emb, extra)
            vlm = None
            if args.dev_run and (args.dev_run / f"{name}.json").exists():
                vlm = json.loads((args.dev_run / f"{name}.json").read_text(encoding="utf-8"))[
                    "fields"
                ][field]["value"]
            n += 1
            ok_m += p["value"] == truth
            ok_v += vlm == truth
            print(
                f"    dev {name[5:22]} 정답={truth} 모델={p['value']}({p['confidence']}) "
                f"VLM/카드={vlm}"
            )
        print(
            f"  개선용 10건: 모델 {ok_m}/{n}" + (f" | 기존 카드 {ok_v}/{n}" if args.dev_run else "")
        )


if __name__ == "__main__":
    main()
