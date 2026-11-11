"""영상 모델(video_model_tr.pt) 사람 정답 평가 2가지. 무료(노트북 GPU, 수 분 + 분류기 비교에 수 분).

사용:
    .venv/Scripts/python evals/vision/eval_video_human.py

미리 정한 방식(결과를 보고 바꾸지 않는다, 2026-11-10):
[1] 움직임 B 짝 비교: 영상 모델 vs 움직임 분류기(exp_human_mix의 '분류기 + 사람 라벨(무게 20)',
5묶음 교차 검증 예측)
    같은 영상에서 '영상 모델만 맞힘'(b)과 '분류기만 맞힘'(c)을 세고 정확 맥니마 검정(둘 중 하나만
    맞힌 건끼리 50:50인지)
    판정: 영상 모델이 더 맞히고 p < 0.10 → 영상 모델로 바꿈 / 그 밖 → 그대로 두고, 둘이 다르면 확인
    표시
[2] 사람 라벨 보정(움직임 A·B): 영상 모델 본체는 고정. 사람 정답을 4묶음으로 나눠 3묶음으로 작은
판단부
    (로지스틱 회귀, 입력 = 영상 모델 답 확률 4개 + 본체 요약 768개를 PCA 16개로 줄인 값)를 만들고
    남은 묶음을 평가, 4번 돌림
    판정: 보정 후가 보정 전보다 더 맞히고 p < 0.10 → 보정 채택 / 아니면 보정 안 함
    장소는 하지 않는다(92건 중 62건 정답이 AI Hub 라벨이라 사람 보정이 아님)
공통: '기타'로 답하면 B는 카메라 규칙, A는 직진(train_video.py와 같음). 정답은 사람 라벨 그대로
결과: evals/results/vision/video_human_preds.json(영상별 예측)
"""

import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from exp_human_mix import EN, RUNS, four  # noqa: E402
from make_answer_sheet import prefill  # noqa: E402
from score import load_answers  # noqa: E402
from silver import ego_cached  # noqa: E402

from workers.vision.video_model import (  # noqa: E402
    CACHE,
    MODEL_PATH,
    MOVES,
    PLACES,
    build_model,
    load_clip,
    to_tensor,
)  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
MOVE_FIELDS = ("movement_B", "movement_A")


def mcnemar(b: int, c: int) -> float:
    """정확 맥니마 검정 양측 p값: 한쪽만 맞힌 b+c건이 50:50에서 이만큼 치우칠 확률."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n)


def load_human():
    """train_video.py와 같은 사람 정답: 우리 30건 + 구준휘님 64건(겹치면 구준휘님)."""
    with (RESULTS / "labels.csv").open(encoding="utf-8-sig") as f:
        labels = {r["video_name"]: r for r in csv.DictReader(f)}
    gold = load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx")
    human = {
        n: {
            "location_type": g["location_type"],
            "movement_A": g["movement_A"],
            "movement_B": g["movement_B"],
        }
        for n, g in gold.items()
    }
    for x in (RESULTS / "human_labels_jh_2026-11-09.jsonl").open(encoding="utf-8"):
        r = json.loads(x)
        if r["status"] == "scored":
            human[r["example_id"]] = {
                "location_type": prefill(labels[r["example_id"]]).get("location_type"),
                "movement_A": EN[r["movement_other"]],
                "movement_B": EN[r["movement_ego"]],
            }
    #  2026-11-10 정지원 재확인(8건, 가짜 4건 섞어 순서 섞음): 빈칸이면 원래 라벨 유지. 원본 jsonl은
    # 안 바꿈
    recheck = RESULTS / "recheck_a_1110.csv"
    if recheck.exists():
        with recheck.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["my_movement_A"].strip() and r["video_name"] in human:
                    human[r["video_name"]]["movement_A"] = r["my_movement_A"].strip()
    return human


def model_outputs(names):
    """{이름: {"feat": 요약 768개, 칸: 확률}} — 본체는 고정(학습 안 함)."""
    import numpy as np
    import torch

    net = build_model()
    net.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"))
    net = net.cuda().eval()
    out = {}
    with torch.no_grad():
        for i in range(0, len(names), 8):
            chunk = names[i : i + 8]
            x = to_tensor(np.stack([load_clip("", n) for n in chunk])).cuda()
            with torch.autocast("cuda", dtype=torch.float16):
                h = net.norm(net.backbone(pixel_values=x).last_hidden_state.mean(1)).float()
                probs = {
                    k: head(h).float().softmax(-1).cpu().numpy() for k, head in net.heads.items()
                }
            h = h.cpu().numpy()
            for j, n in enumerate(chunk):
                out[n] = {"feat": h[j], **{k: v[j] for k, v in probs.items()}}
    return out


def with_fallback(field, cls, name, human):
    """영상 모델·보정 답이 '기타'면 B는 카메라 규칙, A는 직진."""
    if cls != "기타":
        return cls
    if field == "movement_B":
        return ego_cached(name).field_opinions(human[name]["location_type"])["movement_B"]["value"]
    return "직진"


def main():
    import numpy as np
    from sklearn.decomposition import PCA
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import KFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    human = load_human()
    names = sorted(n for n in human if (CACHE / f"{n}.npy").exists())
    print(f"사람 정답 {len(names)}건, 모델 {MODEL_PATH.name}", flush=True)
    outs = model_outputs(names)
    vlm = {}
    for run in RUNS:
        for p in (RESULTS / "runs" / run).glob("*.json"):
            if p.name != "run.json":
                c = json.loads(p.read_text(encoding="utf-8"))
                vlm[c["case_id"]] = {
                    k: (c["fields"][k]["vision_detail"].get("vlm") or {}).get("value")
                    for k in (*MOVE_FIELDS, "location_type")
                }

    video = {
        f: {n: with_fallback(f, MOVES[int(outs[n][f].argmax())], n, human) for n in names}
        for f in MOVE_FIELDS
    }
    place = {n: PLACES[int(outs[n]["location_type"].argmax())] for n in names}

    # [2] 사람 라벨 보정(4묶음 돌려 가며)
    calib = {f: {} for f in MOVE_FIELDS}
    folds = list(KFold(4, shuffle=True, random_state=0).split(names))
    feats = np.stack([outs[n]["feat"] for n in names])
    for f in MOVE_FIELDS:
        probs = np.stack([outs[n][f] for n in names])
        y = np.array([four(human[n][f]) for n in names])
        for tr, te in folds:
            pca = PCA(16, random_state=0).fit(feats[tr])  # 평가 묶음은 PCA에도 안 씀
            X = lambda idx: np.hstack([np.log(probs[idx] + 1e-6), pca.transform(feats[idx])])  # noqa: E731, B023
            clf = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=5000)).fit(
                X(tr), y[tr]
            )
            for i, cls in zip(te, clf.predict(X(te)), strict=False):
                calib[f][names[i]] = with_fallback(f, cls, names[i], human)

    # [1] 움직임 분류기 예측(exp_human_mix와 같은 5묶음 교차 검증). 출력이 길어서 앞에 구분선
    print("\n---- (참고) exp_human_mix 출력: 분류기 다시 계산 ----", flush=True)
    from exp_human_mix import main as classifier_cv

    clf_preds = classifier_cv()
    print("---- 여기까지 ----\n")

    print("== [1] 움직임 B 짝 비교: 영상 모델 vs 분류기(+사람 라벨)")
    both = [n for n in names if n in clf_preds["movement_B"]]
    truth = {n: human[n]["movement_B"] for n in both}
    v_ok = {n: video["movement_B"][n] == truth[n] for n in both}
    c_ok = {n: clf_preds["movement_B"][n]["classifier"] == truth[n] for n in both}
    b = sum(v_ok[n] and not c_ok[n] for n in both)
    c = sum(c_ok[n] and not v_ok[n] for n in both)
    p = mcnemar(b, c)
    print(f"  같은 {len(both)}건: 영상 모델 {sum(v_ok.values())}, 분류기 {sum(c_ok.values())}")
    print(
        f"  둘 다 맞힘 {sum(v_ok[n] and c_ok[n] for n in both)} | 영상 모델만 {b} | 분류기만 {c} | "
        f"둘 다 틀림 {sum(not v_ok[n] and not c_ok[n] for n in both)} | p = {p:.3f}"
    )
    print("  판정:", "영상 모델로 바꿈" if b > c and p < 0.10 else "그대로(둘이 다르면 확인 표시)")
    agree = [n for n in both if video["movement_B"][n] == clf_preds["movement_B"][n]["classifier"]]
    dis = [n for n in both if n not in agree]
    print(
        f"  (확인 표시 참고) 둘이 같은 {len(agree)}건 정답률 "
        f"{sum(v_ok[n] for n in agree)}/{len(agree)} | "
        f"다른 {len(dis)}건: 영상 모델 {sum(v_ok[n] for n in dis)}, 분류기 "
        f"{sum(c_ok[n] for n in dis)}"
    )

    print("\n== [2] 사람 라벨 보정(4묶음 돌려 가며): 보정 전 vs 보정 후")
    for f in MOVE_FIELDS:
        pre = {n: video[f][n] == human[n][f] for n in names}
        post = {n: calib[f][n] == human[n][f] for n in names}
        b = sum(post[n] and not pre[n] for n in names)
        c = sum(pre[n] and not post[n] for n in names)
        p = mcnemar(b, c)
        print(
            f"  {f}: 보정 전 {sum(pre.values())}/{len(names)} → 보정 후 "
            f"{sum(post.values())}/{len(names)} "
            f"| 보정만 맞힘 {b}, 보정 전만 맞힘 {c}, p = {p:.3f} → "
            f"{'보정 채택' if b > c and p < 0.10 else '보정 안 함'}"
        )
        for cl in ("직진", "좌회전", "우회전"):
            sel = [n for n in names if human[n][f] == cl]
            print(
                f"    {cl} {len(sel)}건 맞힘: 보정 전 {sum(video[f][n] == cl for n in sel)}, "
                f"보정 후 {sum(calib[f][n] == cl for n in sel)}"
            )
        sub = [n for n in names if n in vlm and vlm[n].get(f)]
        if sub:
            print(
                f"    (VLM 답 있는 {len(sub)}건) VLM {sum(vlm[n][f] == human[n][f] for n in sub)}, "
                f"보정 전 {sum(pre[n] for n in sub)}, 보정 후 {sum(post[n] for n in sub)}"
            )

    rows = {
        n: {
            "truth": human[n],
            "video": {f: video[f][n] for f in MOVE_FIELDS} | {"location_type": place[n]},
            "calibrated": {f: calib[f][n] for f in MOVE_FIELDS},
            "classifier_B": clf_preds["movement_B"].get(n, {}).get("classifier"),
            "vlm": vlm.get(n),
        }
        for n in names
    }
    (RESULTS / "video_human_preds.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("\n저장: evals/results/vision/video_human_preds.json")


if __name__ == "__main__":
    main()
