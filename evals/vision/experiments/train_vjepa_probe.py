"""V-JEPA 2 특징(본체 고정) 위에 작은 판단부(어텐션 프로브)를 학습하고 사람 정답으로 평가한다.
무료(GPU, 몇 분).

사용:
    .venv/Scripts/python evals/vision/experiments/extract_vjepa.py        # 먼저 특징 뽑기
    .venv/Scripts/python evals/vision/experiments/train_vjepa_probe.py

같게 맞춘 것(VideoMAE v3와 공정 비교): 학습 영상(train_answers.csv + train_answers_tr.csv, 6,438 +
Validation),
칸과 답 목록(SPECS["v3"]), 검증 10% 떼는 방법(seed 0), 답 종류별 가중치(--balance와 같은 식). 씨앗은
한 번만(0) 돌린다.
판단부: 칸마다 질문 벡터 1개가 화면 128칸(시간 8 × 4×4)을 어텐션으로 훑고 + SSv2 요약 → 선형.
검증(AI Hub)으로 회차를 고른다.

미리 정한 판정(2026-11-10, 결과 보기 전). 정답 = 사람 92건(다시 본 라벨 반영,
eval_video_human.load_human):
- 장소: V-JEPA가 v2보다 많이 맞히고 정확 맥니마 p < 0.10이면 장소 값을 V-JEPA로
- 움직임 B: V-JEPA가 지금 값(분류기)보다 많이 맞히고 p < 0.10이면 B 값을 V-JEPA로
- 움직임 A: 'V-JEPA 단독'과 'v2 + V-JEPA 섞기'(V-JEPA가 진로변경·추월일 때만) 중 나은 것이
  지금(v2 + v3 섞기)보다 많이 맞히고, 진로변경·추월이라고 한 것 중 맞은 비율 50% 이상이면 교체
- 상대 위치·도로 관계(정답 카드 28건): 확인 표시용 v3보다 많이 맞히면 표시를 V-JEPA 기준으로(30건
  재조립으로 다시 확인)
'기타'면 A는 직진, B는 카메라 규칙(train_video.py와 같음)
결과: data/private/models/vjepa_probe.pt, evals/results/vision/vjepa_human_preds.json
"""

import csv
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # evals/vision 공통 도구
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 이 폴더의 실험 모듈
from eval_video_human import load_human, mcnemar  # noqa: E402
from score import load_answers  # noqa: E402
from silver import ego_cached  # noqa: E402
from vjepa_feat import load_feat  # noqa: E402

from workers.vision.video_model import SPECS, label_class, predict  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
# 구준휘님 사람 라벨(scored 64건)
HUMAN_LABELS = ROOT / "evals/vision/labels/human_labels_2026-11-09.jsonl"
MODEL_OUT = ROOT / "data/private/models/vjepa_probe.pt"
SPEC = "v3"
FIELDS = tuple(SPECS[SPEC])
SPECIAL = ("진로변경", "추월")
EPOCHS, BATCH, LR = 40, 64, 3e-4


def target(field, value):
    cls = label_class(SPEC, field, value)
    return SPECS[SPEC][field].index(cls) if cls is not None else -100


def build_probe():
    import torch
    from torch import nn

    class Probe(nn.Module):
        def __init__(self):
            super().__init__()
            d = 1024
            self.pos = nn.Parameter(torch.randn(1, 128, d) * 0.02)
            self.norm = nn.LayerNorm(d)
            self.query = nn.Parameter(torch.randn(len(FIELDS), d) * 0.02)
            self.attn = nn.MultiheadAttention(d, 8, dropout=0.1, batch_first=True)
            self.snorm = nn.LayerNorm(d)
            self.drop = nn.Dropout(0.3)
            self.heads = nn.ModuleDict({f: nn.Linear(2 * d, len(SPECS[SPEC][f])) for f in FIELDS})

        def forward(self, grid, ssv2):  # grid (B, 128, 1024), ssv2 (B, 1024)
            x = self.norm(grid + self.pos)
            o, _ = self.attn(self.query.expand(len(x), -1, -1), x, x)
            s = self.snorm(ssv2)
            return {
                f: self.heads[f](self.drop(torch.cat([o[:, i], s], -1)))
                for i, f in enumerate(FIELDS)
            }

    return Probe()


def main():
    import numpy as np
    import torch

    torch.manual_seed(0)
    random.seed(0)
    silver = {}
    for name in ("train_answers.csv", "train_answers_tr.csv"):
        with (RESULTS / name).open(encoding="utf-8-sig") as f:
            silver.update({r["video_name"]: r for r in csv.DictReader(f)})
    human = load_human()
    jh = {
        json.loads(x)["example_id"]
        for x in HUMAN_LABELS.open(encoding="utf-8")
    }
    excluded = set(human) | jh
    pool = sorted(n for n in silver if n not in excluded and load_feat(n) is not None)
    random.shuffle(pool)
    n_val = len(pool) // 10
    val, train = pool[:n_val], pool[n_val:]
    print(f"학습 {len(train)}건, 검증 {len(val)}건 (특징 있는 영상만)", flush=True)

    def stack(names):
        feats = [load_feat(n) for n in names]
        g = torch.from_numpy(np.stack([f[0] for f in feats]).reshape(len(names), 128, 1024))
        s = torch.from_numpy(np.stack([f[1] for f in feats]))
        return g, s

    t0 = time.time()
    Gtr, Str = stack(train)
    Gva, Sva = stack(val)
    Ytr = {f: torch.tensor([target(f, silver[n][f]) for n in train]) for f in FIELDS}
    Yva = {f: torch.tensor([target(f, silver[n][f]) for n in val]) for f in FIELDS}
    print(f"특징 불러오기 {time.time() - t0:.0f}초", flush=True)

    net = build_probe().cuda()
    opt = torch.optim.AdamW(net.parameters(), lr=LR, weight_decay=0.05)
    steps = EPOCHS * (len(train) // BATCH)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=LR, total_steps=steps + 5, pct_start=0.1
    )
    ces = {}
    for f in (
        FIELDS
    ):  # 답 종류별 가중치 = (전체/(종류 수×그 답 수))^0.5 (train_video.py --balance와 같음)
        k = len(SPECS[SPEC][f])
        y = Ytr[f][Ytr[f] != -100].numpy()
        cnt = np.bincount(y, minlength=k)
        w = np.where(cnt > 0, np.sqrt(cnt.sum() / (k * np.maximum(cnt, 1))), 0.0)
        ces[f] = torch.nn.CrossEntropyLoss(
            weight=torch.tensor(w, dtype=torch.float32).cuda(), ignore_index=-100
        )

    def run(G, S):
        net.eval()
        out = {f: [] for f in FIELDS}
        with torch.no_grad():
            for i in range(0, len(G), 256):
                o = net(G[i : i + 256].cuda().float(), S[i : i + 256].cuda())
                for f in FIELDS:
                    out[f].append(o[f].float().softmax(-1).cpu())
        net.train()
        return {f: torch.cat(v) for f, v in out.items()}

    best = -1.0
    for ep in range(EPOCHS):
        perm = torch.randperm(len(train))
        for i in range(0, len(train) - BATCH + 1, BATCH):
            idx = perm[i : i + BATCH]
            out = net(Gtr[idx].cuda().float(), Str[idx].cuda())
            loss = sum(ces[f](out[f], Ytr[f][idx].cuda()) for f in FIELDS)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
        probs = run(Gva, Sva)
        acc = {}
        for f in FIELDS:
            m = Yva[f] != -100
            acc[f] = (probs[f].argmax(-1)[m] == Yva[f][m]).float().mean().item()
        score = sum(acc.values()) / len(acc)
        if score > best:
            best = score
            MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
            torch.save(net.state_dict(), MODEL_OUT)
        if (ep + 1) % 5 == 0 or ep == 0:
            print(
                f"회차 {ep + 1}: 검증(AI Hub 라벨) "
                + " ".join(f"{f} {acc[f]:.0%}" for f in FIELDS)
                + f" | 최고 {best:.3f} | {(time.time() - t0) / 60:.1f}분",
                flush=True,
            )

    # ---- 사람 정답 평가(가장 좋은 회차) ----
    net.load_state_dict(torch.load(MODEL_OUT))
    names = sorted(n for n in human if load_feat(n) is not None)
    G, S = stack(names)
    probs = run(G, S)
    vj = {
        f: {n: SPECS[SPEC][f][probs[f][i].argmax().item()] for i, n in enumerate(names)}
        for f in FIELDS
    }
    old = json.loads((RESULTS / "video_human_preds.json").read_text(encoding="utf-8"))
    gold = load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx")

    def fixA(v):
        return "직진" if v == "기타" else v

    def fixB(n, v):
        return (
            ego_cached(n).field_opinions(human[n]["location_type"])["movement_B"]["value"]
            if v == "기타"
            else v
        )

    v2 = {n: predict("", n, spec="v2") for n in names}
    v3 = {n: predict("", n, spec="v3") for n in names}
    truth = {n: human[n] for n in names}
    print(f"\n== 사람 정답 {len(names)}건 (다시 본 라벨 반영)")

    def compare(title, f, new, cur, cur_name):
        sel = [n for n in names if truth[n].get(f)]
        a = sum(new[n] == truth[n][f] for n in sel)
        c = sum(cur[n] == truth[n][f] for n in sel)
        b_ = sum(new[n] == truth[n][f] and cur[n] != truth[n][f] for n in sel)
        c_ = sum(new[n] != truth[n][f] and cur[n] == truth[n][f] for n in sel)
        p = mcnemar(b_, c_)
        print(
            f"  {title:24} V-JEPA {a}/{len(sel)} vs {cur_name} {c}/{len(sel)} (V-JEPA만 맞힘 {b_}, "
            f"반대 {c_}, p={p:.2f})"
        )
        return a, c, p

    verdict = []
    # 장소
    a, c, p = compare(
        "장소",
        "location_type",
        vj["location_type"],
        {n: v2[n]["location_type"]["value"] for n in names},
        "v2",
    )
    verdict.append(f"장소: {'V-JEPA로 교체' if a > c and p < 0.10 else 'v2 유지'}")
    # 움직임 B
    vjB = {n: fixB(n, vj["movement_B"][n]) for n in names}
    clfB = {
        n: old.get(n, {}).get("classifier_B") or fixB(n, v2[n]["movement_B"]["value"])
        for n in names
    }
    a, c, p = compare("움직임 B", "movement_B", vjB, clfB, "분류기(지금 값)")
    compare(
        "  (참고) B vs 영상 모델 v2",
        "movement_B",
        vjB,
        {n: fixB(n, v2[n]["movement_B"]["value"]) for n in names},
        "v2",
    )
    verdict.append(f"움직임 B: {'V-JEPA로 교체' if a > c and p < 0.10 else '분류기 유지'}")
    # 움직임 A
    a2 = {n: fixA(v2[n]["movement_A"]["value"]) for n in names}
    a3 = {n: fixA(v3[n]["movement_A"]["value"]) for n in names}
    cur = {n: a3[n] if a3[n] in SPECIAL else a2[n] for n in names}
    alone = {n: fixA(vj["movement_A"][n]) for n in names}
    mixv = {n: alone[n] if alone[n] in SPECIAL else a2[n] for n in names}
    best_opt = None
    for title, pred in (("움직임 A: V-JEPA 단독", alone), ("움직임 A: v2+V-JEPA 섞기", mixv)):
        a, c, p = compare(title, "movement_A", pred, cur, "지금(v2+v3 섞기)")
        sw = [n for n in names if pred[n] in SPECIAL]
        prec = sum(pred[n] == truth[n]["movement_A"] for n in sw) / max(len(sw), 1)
        print(f"    진로변경·추월이라고 한 {len(sw)}건 중 맞음 {prec:.0%}")
        if a > c and prec >= 0.5 and (best_opt is None or a > best_opt[1]):
            best_opt = (title, a)
    for c_ in ("좌회전", "우회전", "진로변경", "추월", "주정차", "출차"):
        sel = [n for n in names if truth[n]["movement_A"] == c_]
        if sel:
            print(
                f"    A {c_} {len(sel)}건: V-JEPA {sum(alone[n] == c_ for n in sel)} | v2 "
                f"{sum(a2[n] == c_ for n in sel)}"
                f" | v3 {sum(a3[n] == c_ for n in sel)}"
            )
    verdict.append(
        f"움직임 A: {best_opt[0].split(': ')[1] + '로 교체' if best_opt else '지금(v2+v3 섞기) '
                                                                       '유지'}"
    )
    # 상대 위치·도로 관계(정답 카드)
    for f in ("relative_position", "road_priority"):
        sel = [n for n in names if n in gold and gold[n].get(f)]
        t = {n: label_class(SPEC, f, gold[n][f]) for n in sel}
        a = sum(vj[f][n] == t[n] for n in sel)
        c = sum(v3[n][f]["value"] == t[n] for n in sel)
        print(
            f"  {f:24} V-JEPA {a}/{len(sel)} vs v3 {c}/{len(sel)} (정답 카드, 왼쪽·오른쪽은 '옆')"
        )
        verdict.append(
            f"{f} 확인 표시: {'V-JEPA 기준으로(30건 재조립 확인)' if a > c else 'v3 유지'}"
        )

    print("\n판정(미리 정한 기준):")
    for v in verdict:
        print("  " + v)
    rows = {n: {"truth": truth[n], "vjepa": {f: vj[f][n] for f in FIELDS}} for n in names}
    (RESULTS / "vjepa_human_preds.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\n저장: {MODEL_OUT}, evals/results/vision/vjepa_human_preds.json")


if __name__ == "__main__":
    main()
