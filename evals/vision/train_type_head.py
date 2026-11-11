"""사고 유형 × 방향을 직접 맞히는 판단부. 고른 유형에서 칸 값을 유형표 규칙으로 한 번에 채운다.
무료(GPU, 약 10~15분).

사용:
    .venv/Scripts/python evals/vision/train_type_head.py

왜(2026-11-10): 지금은 칸 8개를 따로 맞혀서 칸끼리 안 맞고(장소가 틀리면 도로 관계·진입 순서도
틀림),
VLM 자신감은 늘 0.8~0.9라 쓸 수 없다. 유형을 고르면 칸이 항상 서로 맞고, '그 값을 가진 유형들의 확률
합'이
칸마다 진짜 자신감이 된다. 최종 목표(과실 도표)와도 바로 이어진다.
- 정답: AI Hub 라벨의 accident_type × label_swapped(블랙박스 차가 유형표의 A인가) → 예: '115|True'
  예시 20건 이상인 조합 82개(학습 영상의 88%) + '기타'. 조합의 칸 값 =
  make_answer_sheet.prefill(같은 조합이면 늘 같음)
- 입력: 우리 영상으로 학습한 VideoMAE v2·v3의 영상 요약(768 + 768). 본체는 다시 학습하지 않는다
  (v2·v3를 학습한 영상으로 판단부를 학습하므로 학습 영상 점수는 부풀려짐 → 회차는 v2·v3와 같은 검증
  10%로 고름)
- 판단부: 1536 → 512 → 83, 답 종류별 가중치(train_video.py --balance와 같은 식), 씨앗 0으로 한 번만

미리 정한 판정(결과 보기 전):
- 사람 92건(장소·움직임 A·B): train_vjepa_probe.py와 같은 기준(장소·B는 p < 0.10, A는 지금(v2+v3
  섞기)보다 많이)
- 사람 라벨이 없는 칸(도로 관계·진입 순서·상대 위치·신호): 30건 개발 세트에서 지금 카드보다 2칸 이상
  더 맞히면
  그 칸은 유형 값으로(개발 세트라 부풀려질 수 있음을 기록). 유형이 그 칸을 정하지 않으면 지금 카드
  값 그대로
- 자신감: 92건에서 자신감 높은 쪽이 낮은 쪽보다 정답률이 높아야 확인 표시에 쓴다(구간별 표를 본다)
결과: data/private/models/type_head.pt, evals/results/vision/type_head_preds.json
"""

import csv
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_video_human import load_human, mcnemar  # noqa: E402
from make_answer_sheet import label_swapped, prefill  # noqa: E402
from silver import ego_cached  # noqa: E402

from workers.vision.video_model import (
    CACHE,
    MODEL_FILES,
    build_model,
    load_clip,
    predict,
    to_tensor,
)  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
MODELS = ROOT / "data/private/models"
FEAT = RESULTS / "type_feat"
OUT = MODELS / "type_head.pt"
MIN_COUNT = 20
EPOCHS, BATCH, LR = 30, 128, 1e-3
SPECIAL = ("진로변경", "추월")
CORE = [
    "location_type",
    "road_priority",
    "signal_A",
    "signal_B",
    "movement_A",
    "movement_B",
    "relative_position",
    "entry_order",
]
OTHER = "기타"


def video_features(spec, names):
    """VideoMAE(spec) 영상 요약(norm 뒤 768개). evals/results/vision/type_feat/<spec>.npz에 저장해
    두고 다시 쓴다."""
    import numpy as np
    import torch

    path = FEAT / f"{spec}.npz"
    have = {}
    if path.exists():
        d = np.load(path)
        have = dict(zip(d["names"].tolist(), d["feat"]))
    todo = [n for n in names if n not in have]
    if todo:
        net = build_model(spec)
        net.load_state_dict(torch.load(MODELS / MODEL_FILES[spec], map_location="cpu"))
        net = net.cuda().eval()
        t0 = time.time()
        with torch.no_grad():
            for i in range(0, len(todo), 16):
                chunk = todo[i : i + 16]
                x = to_tensor(np.stack([load_clip("", n) for n in chunk])).cuda()
                with torch.autocast("cuda", dtype=torch.float16):
                    h = (
                        net.norm(net.backbone(pixel_values=x).last_hidden_state.mean(1))
                        .float()
                        .cpu()
                        .numpy()
                    )
                have.update(zip(chunk, h))
                if (i // 16) % 50 == 0:
                    print(
                        f"  {spec} 요약 {i + len(chunk)}/{len(todo)} | {(time.time() - t0) / 60:.1f}분",
                        flush=True,
                    )
        del net
        torch.cuda.empty_cache()
        FEAT.mkdir(parents=True, exist_ok=True)
        keys = sorted(have)
        np.savez(
            path, names=np.array(keys), feat=np.stack([have[k] for k in keys]).astype(np.float32)
        )
    return np.stack([have[n] for n in names])


def marginal(probs, classes, fields_of, field):
    """칸 값별 확률 = 그 값을 가진 유형들의 확률 합 / 그 칸을 정하는 유형들의 확률 합. (값, 자신감)
    또는 (None, 0)."""
    mass, total = Counter(), 0.0
    for p, c in zip(probs, classes):
        v = fields_of.get(c, {}).get(field)
        if v:
            mass[v] += p
            total += p
    if not mass:
        return None, 0.0
    v, m = mass.most_common(1)[0]
    return v, m / total


def main():
    import numpy as np
    import torch
    from torch import nn

    torch.manual_seed(0)
    random.seed(0)
    lab = {}
    for f in ("labels.csv", "labels_train.csv"):
        with (RESULTS / f).open(encoding="utf-8-sig") as fh:
            lab.update({r["video_name"]: r for r in csv.DictReader(fh)})
    silver = {}
    for f in ("train_answers.csv", "train_answers_tr.csv"):
        with (RESULTS / f).open(encoding="utf-8-sig") as fh:
            silver.update({r["video_name"]: r for r in csv.DictReader(fh)})
    human = load_human()
    jh = {
        json.loads(x)["example_id"]
        for x in (RESULTS / "human_labels_jh_2026-11-09.jsonl").open(encoding="utf-8")
    }
    # train_video.py와 같은 순서로 섞어 같은 검증 10%를 쓴다(v2·v3가 보지 않은 영상)
    pool = sorted(n for n in silver if n not in set(human) | jh and (CACHE / f"{n}.npy").exists())
    random.shuffle(pool)
    n_val = len(pool) // 10
    val, train = pool[:n_val], pool[n_val:]

    key = {n: f"{lab[n]['accident_type']}|{label_swapped(lab[n])}" for n in pool}
    cnt = Counter(key[n] for n in train)
    classes = sorted(k for k, v in cnt.items() if v >= MIN_COUNT) + [OTHER]
    idx = {c: i for i, c in enumerate(classes)}
    fields_of = {}
    for n in pool:
        if key[n] in idx and key[n] not in fields_of:
            fields_of[key[n]] = prefill(lab[n])
    print(
        f"학습 {len(train)}건, 검증 {len(val)}건, 유형×방향 {len(classes) - 1}개 + 기타", flush=True
    )

    hnames = sorted(n for n in human if (CACHE / f"{n}.npy").exists())
    allnames = train + val + hnames
    X = np.concatenate([video_features("v2", allnames), video_features("v3", allnames)], 1)
    X = torch.tensor(X, dtype=torch.float32)
    Xtr, Xva, Xh = (
        X[: len(train)],
        X[len(train) : len(train) + len(val)],
        X[len(train) + len(val) :],
    )
    ytr = torch.tensor([idx.get(key[n], idx[OTHER]) for n in train])
    yva = torch.tensor([idx.get(key[n], idx[OTHER]) for n in val])

    k = len(classes)
    c = np.bincount(ytr.numpy(), minlength=k)
    w = np.sqrt(c.sum() / (k * np.maximum(c, 1)))
    ce = nn.CrossEntropyLoss(weight=torch.tensor(w, dtype=torch.float32).cuda())
    net = nn.Sequential(
        nn.Dropout(0.2), nn.Linear(X.shape[1], 512), nn.GELU(), nn.Dropout(0.3), nn.Linear(512, k)
    ).cuda()
    opt = torch.optim.AdamW(net.parameters(), lr=LR, weight_decay=0.05)
    best = -1.0
    for ep in range(EPOCHS):
        net.train()
        perm = torch.randperm(len(train))
        for i in range(0, len(train), BATCH):
            b = perm[i : i + BATCH]
            loss = ce(net(Xtr[b].cuda()), ytr[b].cuda())
            opt.zero_grad()
            loss.backward()
            opt.step()
        net.eval()
        with torch.no_grad():
            pva = net(Xva.cuda()).argmax(-1).cpu()
        top1 = (pva == yva).float().mean().item()
        if top1 > best:
            best = top1
            OUT.parent.mkdir(parents=True, exist_ok=True)
            torch.save({"state": net.state_dict(), "classes": classes, "fields_of": fields_of}, OUT)
        if (ep + 1) % 5 == 0:
            print(
                f"회차 {ep + 1}: 검증(AI Hub) 유형×방향 맞힘 {top1:.0%} | 최고 {best:.0%}",
                flush=True,
            )

    net.load_state_dict(torch.load(OUT)["state"])
    net.eval()
    with torch.no_grad():
        pva = net(Xva.cuda()).softmax(-1).cpu().numpy()
        ph = net(Xh.cuda()).softmax(-1).cpu().numpy()
    top5 = np.mean(
        [idx.get(key[n], idx[OTHER]) in np.argsort(-pva[i])[:5] for i, n in enumerate(val)]
    )
    print(f"검증(AI Hub) 상위 5개 안에 정답 유형: {top5:.0%}")

    # ---- 사람 92건 ----
    typed = {
        n: {f: marginal(ph[i], classes, fields_of, f) for f in CORE} for i, n in enumerate(hnames)
    }
    old = json.loads((RESULTS / "video_human_preds.json").read_text(encoding="utf-8"))
    v2 = {n: predict("", n, spec="v2") for n in hnames}
    v3 = {n: predict("", n, spec="v3") for n in hnames}

    def fixA(v):
        return "직진" if v == "기타" else v

    def fixB(n, v):
        return (
            ego_cached(n).field_opinions(human[n]["location_type"])["movement_B"]["value"]
            if v == "기타"
            else v
        )

    a2 = {n: fixA(v2[n]["movement_A"]["value"]) for n in hnames}
    a3 = {n: fixA(v3[n]["movement_A"]["value"]) for n in hnames}
    current = {
        "location_type": {n: v2[n]["location_type"]["value"] for n in hnames},
        "movement_B": {
            n: old.get(n, {}).get("classifier_B") or fixB(n, v2[n]["movement_B"]["value"])
            for n in hnames
        },
        "movement_A": {n: a3[n] if a3[n] in SPECIAL else a2[n] for n in hnames},
    }
    names_cur = {"location_type": "v2", "movement_B": "분류기", "movement_A": "v2+v3 섞기"}
    print(
        f"\n== 사람 정답 {len(hnames)}건: 유형에서 나온 값 vs 지금 값 (유형이 칸을 안 정하면 지금 값을 씀)"
    )
    verdict = []
    for f in ("location_type", "movement_B", "movement_A"):
        sel = [n for n in hnames if human[n].get(f)]
        t = {n: typed[n][f][0] or current[f][n] for n in sel}
        a = sum(t[n] == human[n][f] for n in sel)
        cc = sum(current[f][n] == human[n][f] for n in sel)
        b_ = sum(t[n] == human[n][f] and current[f][n] != human[n][f] for n in sel)
        c_ = sum(t[n] != human[n][f] and current[f][n] == human[n][f] for n in sel)
        p = mcnemar(b_, c_)
        print(
            f"  {f:16} 유형 {a}/{len(sel)} vs 지금({names_cur[f]}) {cc}/{len(sel)} (유형만 맞힘 {b_}, 반대 {c_}, p={p:.2f})"
        )
        ok = a > cc and (p < 0.10 if f != "movement_A" else True)
        if f == "movement_A" and ok:
            sw = [n for n in sel if t[n] in SPECIAL]
            prec = sum(t[n] == human[n][f] for n in sw) / max(len(sw), 1)
            print(f"    진로변경·추월이라고 한 {len(sw)}건 중 맞음 {prec:.0%}")
            ok = prec >= 0.5
        verdict.append(f"{f}: {'유형 값으로' if ok else '지금 값 유지'}")
        # 자신감 구간별 정답률(유형이 정한 칸만)
        rows = [(typed[n][f][1], typed[n][f][0] == human[n][f]) for n in sel if typed[n][f][0]]
        bins = [(0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]
        print(
            "    자신감 구간별 정답률: "
            + " | ".join(
                f"{lo:.1f}~{min(hi, 1):.1f}: {sum(r for cf, r in rows if lo <= cf < hi)}/{sum(lo <= cf < hi for cf, _ in rows)}"
                for lo, hi in bins
            )
        )

    # ---- 30건 개발 세트(핵심 칸) ----
    score = list(
        csv.DictReader((RESULTS / "runs/dev30_1110_mix/score.csv").open(encoding="utf-8-sig"))
    )
    card = {(r["video"], r["field"]): r for r in score if r["field"] in CORE}
    cards = sorted({v for v, _ in card})
    miss = [n for n in cards if n not in typed]
    if miss:
        print(f"(30건 중 {len(miss)}건은 16장 저장분이 없어 뺌)")
    print("\n== 30건 개발 세트(핵심 칸): 유형 값(유형이 칸을 정하면) vs 지금 카드")
    tot_t = tot_c = 0
    for f in CORE:
        rows = [card[(n, f)] for n in cards if (n, f) in card and n in typed]
        ct = sum((typed[r["video"]][f][0] or r["got"]) == r["truth"] for r in rows)
        cc = sum(r["got"] == r["truth"] for r in rows)
        changed = sum(
            bool(typed[r["video"]][f][0]) and typed[r["video"]][f][0] != r["got"] for r in rows
        )
        tot_t += ct
        tot_c += cc
        print(f"  {f:18} 유형 {ct}/{len(rows)} vs 카드 {cc}/{len(rows)} (값이 바뀐 칸 {changed})")
        if f not in ("location_type", "movement_A", "movement_B"):
            verdict.append(
                f"{f}: {'유형 값으로(30건 개발 세트 기준)' if ct >= cc + 2 else '지금 값 유지'}"
            )
    print(f"  핵심 칸 합계: 유형 {tot_t} vs 카드 {tot_c}")

    print("\n판정(미리 정한 기준):")
    for v in verdict:
        print("  " + v)
    out = {
        n: {
            f: {"value": typed[n][f][0], "confidence": round(float(typed[n][f][1]), 3)}
            for f in CORE
        }
        | {"top5": [(classes[j], round(float(ph[i][j]), 3)) for j in np.argsort(-ph[i])[:5]]}
        for i, n in enumerate(hnames)
    }
    (RESULTS / "type_head_preds.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\n저장: {OUT}, evals/results/vision/type_head_preds.json")


if __name__ == "__main__":
    main()
