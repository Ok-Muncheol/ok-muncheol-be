"""영상 모델(workers/vision/video_model.py) 학습·평가. 무료(노트북 GPU, 약 20~40분).

사용:
    .venv/Scripts/python evals/vision/train_video.py

- 학습: AI Hub 라벨(train_answers.csv). 사람 정답 영상(구준휘님 78건 + 우리 정답 카드 30건)은 완전히 뺀다
  학습 영상의 10%를 검증으로 떼어 가장 좋은 회차를 고른다(검증도 AI Hub 라벨이라 회차 고르기에만 씀)
- 평가: 사람 정답으로만
  움직임 A·B: 구준휘님 64건 + 우리 30건(겹치면 구준휘님). 장소: 우리 30건(검수) + 구준휘님 영상의 AI Hub 장소(폴더 기준)
  비교: 전부 직진, 카메라 규칙(B), VLM(우리 30건)
"""

import csv
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from exp_human_mix import EN, RUNS, four  # noqa: E402
from make_answer_sheet import prefill  # noqa: E402
from score import load_answers  # noqa: E402
from silver import ego_cached  # noqa: E402
from workers.vision.video_model import (  # noqa: E402
    MODEL_FILES, SPECS, build_model, label_class, load_clip, to_tensor,
)

RESULTS = ROOT / "evals/results/vision"
VIDEOS = ROOT / "data/private/aihub/videos"
MODELS = ROOT / "data/private/models"
# 학습할 칸과 답 목록(video_model.SPECS). main에서 --spec으로 정한다. v2: 장소·움직임 A·B(직진·좌·우·기타)
SPEC = "v2"
FIELDS = tuple(SPECS[SPEC])
EPOCHS, BATCH, ACCUM = 6, 4, 2


def target(field, value):
    cls = label_class(SPEC, field, value)
    return SPECS[SPEC][field].index(cls) if cls is not None else -100


def side(field, value):
    """상대 위치는 왼쪽·오른쪽을 '옆'으로 묶어 비교한다(모델이 왼쪽·오른쪽을 배우지 않음)."""
    return "옆" if field == "relative_position" and value in ("왼쪽", "오른쪽") else value


def main():
    import argparse

    import numpy as np
    import torch

    parser = argparse.ArgumentParser()
    # Training 추가(2026-11-10): --answers train_answers.csv train_answers_tr.csv
    parser.add_argument("--answers", nargs="+", default=["train_answers.csv"], help="evals/results/vision/ 안의 정답 파일들")
    parser.add_argument("--epochs", default=EPOCHS, type=int)
    parser.add_argument("--model-out", type=Path, help="기본: data/private/models/<spec의 모델 파일>")
    parser.add_argument("--balance", action="store_true", help="답 종류별 개수 차이를 벌점 가중치로 맞춤(2026-11-10)")
    parser.add_argument("--spec", default="v2", choices=list(SPECS),
                        help="v3: 움직임에 진로변경·추월·주정차·출차, 상대 위치·도로 관계 추가(2026-11-10)")
    args = parser.parse_args()
    global SPEC, FIELDS
    SPEC, FIELDS = args.spec, tuple(SPECS[args.spec])
    model_path = args.model_out or MODELS / MODEL_FILES[SPEC]
    print(f"모델 {SPEC}: 칸 {FIELDS} → {model_path}", flush=True)

    torch.manual_seed(0)
    random.seed(0)
    with (RESULTS / "labels.csv").open(encoding="utf-8-sig") as f:
        labels = {r["video_name"]: r for r in csv.DictReader(f)}
    silver = {}
    for name in args.answers:
        with (RESULTS / name).open(encoding="utf-8-sig") as f:
            silver.update({r["video_name"]: r for r in csv.DictReader(f)})
    gold = load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx")
    jh = [json.loads(x) for x in (RESULTS / "human_labels_jh_2026-11-09.jsonl").open(encoding="utf-8")]
    human = {n: {f: g.get(f) for f in FIELDS} for n, g in gold.items()}
    for r in jh:
        if r["status"] == "scored":
            human[r["example_id"]] = {"location_type": prefill(labels[r["example_id"]]).get("location_type"),
                                      "movement_A": EN[r["movement_other"]], "movement_B": EN[r["movement_ego"]]}
    excluded = set(human) | {r["example_id"] for r in jh}
    pool = sorted(n for n in silver if n not in excluded and (ROOT / "evals/results/vision/clip16" / f"{n}.npy").exists())
    random.shuffle(pool)
    n_val = len(pool) // 10
    val, train = pool[:n_val], pool[n_val:]
    print(f"학습 {len(train)}건, 검증 {len(val)}건, 사람 정답 {len(human)}건(학습에서 뺌)", flush=True)

    def batch_tensor(names, augment=False):
        clips = np.stack([load_clip("", n) for n in names])
        x = to_tensor(clips)
        if augment:  # 밝기·대비만 살짝(좌우 뒤집기 금지)
            x = x * (1 + 0.2 * (torch.rand(len(names), 1, 1, 1, 1) - 0.5)) + 0.2 * (torch.rand(len(names), 1, 1, 1, 1) - 0.5)
        return x

    def ys(names, src):
        return {f: torch.tensor([target(f, src[n][f]) for n in names]) for f in FIELDS}

    net = build_model(SPEC).cuda()
    head_params = [p for n, p in net.named_parameters() if not n.startswith("backbone")]
    opt = torch.optim.AdamW([{"params": [p for p in net.backbone.parameters() if p.requires_grad], "lr": 3e-5},
                             {"params": head_params, "lr": 1e-3}], weight_decay=0.05)
    steps = args.epochs * (len(train) // (BATCH * ACCUM))
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=[3e-5, 1e-3], total_steps=steps + 5, pct_start=0.1)
    scaler = torch.amp.GradScaler("cuda")
    ce = torch.nn.CrossEntropyLoss(ignore_index=-100)
    ces = {f: ce for f in FIELDS}
    if args.balance:  # 적은 답(좌·우회전 등)을 틀리면 더 크게 벌점: 가중치 = (전체/(종류 수×그 답 수))^0.5
        for f in FIELDS:
            k = len(SPECS[SPEC][f])
            cnt = np.bincount([t for t in (target(f, silver[n][f]) for n in train) if t != -100], minlength=k)
            w = np.where(cnt > 0, np.sqrt(cnt.sum() / (k * np.maximum(cnt, 1))), 0.0)
            ces[f] = torch.nn.CrossEntropyLoss(weight=torch.tensor(w, dtype=torch.float32).cuda(), ignore_index=-100)
            print(f"  {f} 답 수 {cnt.tolist()} → 가중치 {[round(x, 2) for x in w]}", flush=True)

    def evaluate(names, src):
        net.eval()
        preds = {f: [] for f in FIELDS}
        with torch.no_grad():
            for i in range(0, len(names), 8):
                chunk = names[i:i + 8]
                with torch.autocast("cuda", dtype=torch.float16):
                    out = net(batch_tensor(chunk).cuda())
                for f in FIELDS:
                    preds[f] += out[f].argmax(-1).cpu().tolist()
        net.train()
        acc = {}
        for f in FIELDS:
            t = [target(f, src[n].get(f)) if src[n].get(f) else -100 for n in names]  # 정답 없는 칸은 뺌
            pairs = [(p, y) for p, y in zip(preds[f], t) if y != -100]
            acc[f] = sum(p == y for p, y in pairs) / max(len(pairs), 1)
        return acc, preds

    best, t0 = -1.0, time.time()
    net.train()
    for ep in range(args.epochs):
        random.shuffle(train)
        opt.zero_grad()
        for i in range(0, len(train) - BATCH + 1, BATCH):
            chunk = train[i:i + BATCH]
            x = batch_tensor(chunk, augment=True).cuda()
            y = {f: v.cuda() for f, v in ys(chunk, silver).items()}
            with torch.autocast("cuda", dtype=torch.float16):
                out = net(x)
                loss = sum(ces[f](out[f].float(), y[f]) for f in FIELDS) / ACCUM
            scaler.scale(loss).backward()
            if (i // BATCH + 1) % ACCUM == 0:
                scaler.step(opt)
                scaler.update()
                opt.zero_grad()
                sched.step()
        acc, _ = evaluate(val, silver)
        score = sum(acc.values()) / len(acc)
        print(f"회차 {ep + 1}: 검증(AI Hub 라벨) " + " ".join(f"{f} {acc[f]:.0%}" for f in FIELDS)
              + f" | {(time.time() - t0) / 60:.0f}분", flush=True)
        if score > best:
            best = score
            model_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save(net.state_dict(), model_path)

    # 사람 정답으로 평가(가장 좋은 회차)
    net.load_state_dict(torch.load(model_path))
    names = sorted(n for n in human if (ROOT / "evals/results/vision/clip16" / f"{n}.npy").exists())
    _, preds = evaluate(names, human)
    vlm = {}
    for run in RUNS:
        for p in (RESULTS / "runs" / run).glob("*.json"):
            if p.name != "run.json":
                c = json.loads(p.read_text(encoding="utf-8"))
                vlm[c["case_id"]] = {k: (c["fields"][k]["vision_detail"].get("vlm") or {}).get("value") for k in FIELDS}
    print(f"\n== 사람 정답 평가 ({len(names)}건)")
    print("  (상대 위치는 왼쪽·오른쪽을 '옆'으로 묶어 비교. 움직임은 '기타'면 B는 카메라 규칙, A는 직진)")
    for f in FIELDS:
        classes = SPECS[SPEC][f]
        rows = [(n, preds[f][i]) for i, n in enumerate(names) if human[n].get(f)]
        got = []
        for n, p in rows:  # 영상 모델이 '기타'면 B는 카메라 규칙, A는 직진(카드에서 VLM 대신 쓰는 값 기준)
            c = classes[p]
            if f.startswith("movement_") and c == "기타":
                c = (ego_cached(n).field_opinions(human[n]["location_type"])["movement_B"]["value"]
                     if f == "movement_B" else "직진")
            got.append(c)
        truth = [side(f, human[n][f]) for n, _ in rows]
        N = len(rows)
        ok = sum(g == t for g, t in zip(got, truth))
        line = f"  {f:18} 영상 모델 {ok}/{N} ({ok / N:.0%})"
        if f.startswith("movement_"):
            line += f" | 전부 직진 {sum(t == '직진' for t in truth)}/{N}"
        else:
            top = max(set(truth), key=truth.count)
            line += f" | 찍기('{top}') {truth.count(top)}/{N}"
        if f == "movement_B":
            rule = sum(ego_cached(n).field_opinions(human[n]["location_type"])["movement_B"]["value"] == human[n][f] for n, _ in rows)
            line += f" | 카메라 규칙 {rule}/{N}"
        sub = [(g, t, side(f, vlm[n].get(f))) for (n, _), g, t in zip(rows, got, truth) if n in vlm]
        if sub:
            line += f" | (VLM 답 있는 {len(sub)}건) VLM {sum(v == t for _, t, v in sub)} vs 영상 모델 {sum(g == t for g, t, _ in sub)}"
        print(line)
        if f.startswith("movement_"):
            for c in ("좌회전", "우회전", "진로변경", "추월", "주정차", "출차"):
                sel = [g for g, t in zip(got, truth) if t == c]
                if sel:
                    print(f"    {c} {len(sel)}건 중 맞힘 {sum(g == c for g in sel)}")


if __name__ == "__main__":
    main()
