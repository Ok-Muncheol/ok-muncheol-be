"""시험(한 번만 돌림): 사람 라벨을 섞어 학습하면 움직임 분류기가 사람 정답에서 나아지나. 무료.

사용:
    .venv/Scripts/python evals/vision/exp_human_mix.py

미리 정한 방식(결과를 보고 바꾸지 않는다, 2026-11-09):
- 사람 정답: 구준휘님 라벨 64건(움직임 A·B, 영상만 보고 붙임) + 우리 정답 카드 30건. 겹치면 구준휘님
  라벨
- 학습: AI Hub 라벨(사람 라벨 영상은 모두 뺌, 무게 1) + 사람 라벨 학습 묶음(무게 W). W는 0과 20 두
  가지만
- 평가: 사람 라벨 영상 5묶음 교차 검증(영상 단위). 기준: 전부 직진, 카메라 규칙(B), VLM(우리 30건만)
- 특징: 움직임 특징 + CLIP 장면(PCA 20) + 장소(AI Hub 라벨, 담당자 확인 장소로 봄). 분류기가 기타면
  B는 규칙, A는 직진
"""

import csv
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_answer_sheet import prefill  # noqa: E402
from score import load_answers  # noqa: E402
from silver import ego_cached  # noqa: E402
from train_motion import video_features  # noqa: E402

from workers.vision.detection import detect_and_track  # noqa: E402
from workers.vision.motion_model import place_onehot  # noqa: E402
from workers.vision.scene_embed import embed_video, scene_vector  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
VIDEOS = ROOT / "data/private/aihub/videos"
EN = {"straight": "직진", "left_turn": "좌회전", "right_turn": "우회전", "u_turn": "유턴"}
FOUR = ("직진", "좌회전", "우회전")
RUNS = ("1109_1042_s1_ego_dev_openai_v0.2", "1109_1717_s1_ego_yolo_motion_test_openai_v0.2")


def four(v):
    return v if v in FOUR else "기타"


def ensure(row: dict) -> None:
    """영상·YOLO·카메라 흐름·CLIP이 없으면 만든다."""
    name = row["video_name"]
    dest = VIDEOS / f"{name}.mp4"
    if not dest.exists():
        zpath, inner = row["mp4_path"].split("::", 1)
        with zipfile.ZipFile(ROOT / zpath) as zf, zf.open(inner) as fin, dest.open("wb") as fout:
            shutil.copyfileobj(fin, fout)
    detect_and_track(str(dest), cache_path=RESULTS / "yolo" / f"{name}.json")
    ego_cached(name)
    embed_video(str(dest), name)


def main():
    import numpy as np
    from sklearn.decomposition import PCA
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.model_selection import KFold

    with (RESULTS / "labels.csv").open(encoding="utf-8-sig") as f:
        labels = {r["video_name"]: r for r in csv.DictReader(f)}
    human = {}
    gold = load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx")
    for n, g in gold.items():
        human[n] = {"movement_A": g["movement_A"], "movement_B": g["movement_B"], "src": "우리 30"}
    jh = [
        json.loads(x) for x in (RESULTS / "human_labels_jh_2026-11-09.jsonl").open(encoding="utf-8")
    ]
    jh_all = {r["example_id"] for r in jh}
    for r in jh:
        if r["status"] == "scored":
            human[r["example_id"]] = {
                "movement_A": EN[r["movement_other"]],
                "movement_B": EN[r["movement_ego"]],
                "src": "구준휘 64",
            }
    for n in human:
        ensure(labels[n])
    place = {n: prefill(labels[n]).get("location_type") for n in labels}

    # VLM 답(우리 30건만): 개선용 아침 실행, 최종용 실행
    vlm = {}
    for run in RUNS:
        for p in (RESULTS / "runs" / run).glob("*.json"):
            if p.name != "run.json":
                c = json.loads(p.read_text(encoding="utf-8"))
                vlm[c["case_id"]] = {
                    k: (c["fields"][k]["vision_detail"].get("vlm") or {}).get("value")
                    for k in ("movement_A", "movement_B")
                }

    with (RESULTS / "train_answers.csv").open(encoding="utf-8-sig") as f:
        silver_ans = {r["video_name"]: r for r in csv.DictReader(f)}
    excluded = set(human) | jh_all
    silver = [n for n in silver_ans if n not in excluded]
    feats = {n: video_features(n) for n in silver + list(human)}
    silver = [n for n in silver if feats[n]]
    hum = sorted(n for n in human if feats[n])
    cols = sorted({k for n in silver for k in feats[n]})
    places = sorted({place[n] for n in silver if place[n]})
    pca = PCA(20, random_state=0).fit(np.array([scene_vector(embed_video("", n)) for n in silver]))

    def X(names):
        base = np.array([[feats[n].get(k, np.nan) for k in cols] for n in names], dtype=float)
        z = pca.transform(np.array([scene_vector(embed_video("", n)) for n in names]))
        return np.hstack([base, z, np.array([place_onehot(place[n], places) for n in names])])

    Xs, Xh = X(silver), X(hum)
    print(
        f"사람 정답 {len(hum)}건 (구준휘 {sum(human[n]['src'] == '구준휘 64' for n in hum)}, "
        f"우리 {sum(human[n]['src'] == '우리 30' for n in hum)}), AI Hub 학습 {len(silver)}건"
    )
    folds = list(KFold(5, shuffle=True, random_state=0).split(hum))
    results = {}  # 다른 스크립트(eval_video_human.py)가 영상별 예측을 쓰도록 돌려준다
    for field in ("movement_B", "movement_A"):
        ys = np.array([four(silver_ans[n][field]) if silver_ans[n][field] else "" for n in silver])
        m = ys != ""
        yh4 = np.array([four(human[n][field]) for n in hum])
        truth = np.array([human[n][field] for n in hum])
        rule = np.array(
            [ego_cached(n).field_opinions(place[n])["movement_B"]["value"] for n in hum]
        )
        preds = {}
        for w in (0, 20):
            out = np.empty(len(hum), dtype=object)
            for tr, te in folds:
                Xt = np.vstack([Xs[m], Xh[tr]]) if w else Xs[m]
                yt = np.concatenate([ys[m], yh4[tr]]) if w else ys[m]
                sw = np.concatenate([np.ones(m.sum()), np.full(len(tr), w)]) if w else None
                clf = HistGradientBoostingClassifier(
                    max_iter=300,
                    learning_rate=0.05,
                    max_leaf_nodes=15,
                    l2_regularization=1.0,
                    random_state=0,
                ).fit(Xt, yt, sample_weight=sw)
                p = clf.predict(Xh[te])
                fallback = rule[te] if field == "movement_B" else np.array(["직진"] * len(te))
                out[te] = np.where(p == "기타", fallback, p)
            preds[w] = out
        results[field] = {
            n: {"classifier": preds[20][i], "rule": rule[i]} for i, n in enumerate(hum)
        }
        N = len(hum)
        print(f"\n== {field} (사람 정답 {N}건)")
        print(
            f"  분류기 AI Hub만(무게 0):   {np.sum(preds[0] == truth)}/{N} "
            f"({np.mean(preds[0] == truth) * 100:.0f}%)"
        )
        print(
            f"  분류기 + 사람 라벨(무게 20): {np.sum(preds[20] == truth)}/{N} "
            f"({np.mean(preds[20] == truth) * 100:.0f}%)"
        )
        print(
            f"  전부 직진:                  {np.sum(truth == '직진')}/{N} "
            f"({np.mean(truth == '직진') * 100:.0f}%)"
        )
        if field == "movement_B":
            print(
                f"  카메라 규칙:                {np.sum(rule == truth)}/{N} "
                f"({np.mean(rule == truth) * 100:.0f}%)"
            )
        has_vlm = np.array([n in vlm for n in hum])
        if has_vlm.any():
            v = np.array([vlm[n][field] if n in vlm else None for n in hum])
            k = has_vlm.sum()
            print(
                f"  (우리 30건 중 VLM 답 있는 {k}건) VLM "
                f"{np.sum(v[has_vlm] == truth[has_vlm])}/{k} | "
                f"분류기+사람 {np.sum(preds[20][has_vlm] == truth[has_vlm])}/{k}"
            )
        for c in ("좌회전", "우회전"):
            sel = truth == c
            print(
                f"  {c} {sel.sum()}건 맞힘: 무게0 {np.sum(preds[0][sel] == c)} | 무게20 "
                f"{np.sum(preds[20][sel] == c)}"
            )
    return results


if __name__ == "__main__":
    main()
