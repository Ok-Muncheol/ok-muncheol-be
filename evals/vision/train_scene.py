"""장면 분류기(workers/vision/scene_embed.py)를 학습·평가한다. 무료(CPU, 몇 분).

사용:
    .venv/Scripts/python evals/vision/train_scene.py --dev-runs evals/results/vision/runs/<실행1>
    <실행2>

- location_type: 학습 영상 전체(라벨 장소, 데이터 폴더 기준이라 정확)
- road_priority: 신호 없는 교차로(사거리_신호없음·T자)만. 다른 곳은 정리 규칙이 '해당없음'으로
  채운다
평가: 5겹 교차 검증(정확도, 찍기, 확신도별), 개선용 10건(사람 정답)에서 VLM 실행들과 비교.
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
#  noqa: E402
from workers.vision.scene_embed import MODEL_DIR, embed_video, predict_scene, scene_vector  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
VIDEOS = ROOT / "data/private/aihub/videos"
JUNCTIONS = {"사거리_신호없음", "T자형교차로"}


def main():
    import joblib
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-runs", type=Path, nargs="*", default=[])
    args = parser.parse_args()

    with (RESULTS / "train_answers.csv").open(encoding="utf-8-sig") as f:
        answers = {r["video_name"]: r for r in csv.DictReader(f)}
    vec = {n: scene_vector(embed_video(str(VIDEOS / f"{n}.mp4"), n)) for n in answers}

    from score import load_answers

    gold = load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx")
    with (RESULTS / "eval_set.csv").open(encoding="utf-8-sig") as f:
        dev = [r["video_name"] for r in csv.DictReader(f) if r["split"] == "dev"]

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for field in ("location_type", "road_priority"):
        names = [
            n
            for n in answers
            if answers[n][field]
            and (field != "road_priority" or answers[n]["location_type"] in JUNCTIONS)
        ]
        X = np.array([vec[n] for n in names])
        y = np.array([answers[n][field] for n in names])
        counts = Counter(y)
        keep = np.array([counts[v] >= 10 for v in y])  # 10건 미만 값은 배울 수 없어 뺀다
        X, y = X[keep], y[keep]
        best = None
        for c in (0.01, 0.03, 0.1, 0.3):
            model = make_pipeline(StandardScaler(), LogisticRegression(C=c, max_iter=3000))
            proba = cross_val_predict(
                model,
                X,
                y,
                cv=StratifiedKFold(5, shuffle=True, random_state=0),
                method="predict_proba",
            )
            cl = np.unique(y)
            acc = float(np.mean(cl[proba.argmax(1)] == y))
            if best is None or acc > best[0]:
                best = (acc, c, proba, cl)
        acc, c, proba, cl = best
        pred, conf = cl[proba.argmax(1)], proba.max(1)
        top, cnt = Counter(y).most_common(1)[0]
        print(f"\n== {field} ({len(y)}건, 교차 검증, C={c})")
        print(f"  정확도 {acc * 100:.0f}% | 찍기(전부 '{top}') {cnt / len(y) * 100:.0f}%")
        for t in (0.6, 0.8, 0.9):
            m = conf >= t
            if m.any():
                print(
                    f"  확신도 ≥{t}: {m.mean() * 100:.0f}%의 영상, 그 정확도 "
                    f"{np.mean(pred[m] == y[m]) * 100:.0f}%"
                )
        for v in cl:
            m = y == v
            print(f"    {v:14} {m.sum():4}건 → 맞힘 {np.mean(pred[m] == v) * 100:3.0f}%")
        model = make_pipeline(StandardScaler(), LogisticRegression(C=c, max_iter=3000)).fit(X, y)
        joblib.dump(
            {"model": model, "field": field, "n": len(y)}, MODEL_DIR / f"scene_{field}.joblib"
        )

        ok = n = 0
        vlm_ok = Counter()
        for name in dev:
            truth = gold.get(name, {}).get(field)
            if not truth or (
                field == "road_priority" and gold[name]["location_type"] not in JUNCTIONS
            ):
                continue
            p = predict_scene(field, embed_video(str(VIDEOS / f"{name}.mp4"), name))
            n += 1
            ok += p["value"] == truth
            vl = []
            for run in args.dev_runs:
                v = json.loads((run / f"{name}.json").read_text(encoding="utf-8"))["fields"][field][
                    "value"
                ]
                vlm_ok[run.name] += v == truth
                vl.append(v)
            print(
                f"    dev {name[5:22]} 정답={truth} 분류기={p['value']}({p['confidence']}) VLM={vl}"
            )
        print(
            f"  개선용: 분류기 {ok}/{n}"
            + "".join(f" | {k[:9]} VLM {v}/{n}" for k, v in vlm_ok.items())
        )


if __name__ == "__main__":
    main()
