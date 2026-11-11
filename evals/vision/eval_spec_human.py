"""학습한 영상 모델 하나(--spec)를 사람 정답으로 지금 카드 설정과 비교하고, 미리 정한 기준으로
판정한다. 무료(GPU, 1~2분).

사용:
    .venv/Scripts/python evals/vision/eval_spec_human.py --spec j3

판정 기준(train_vjepa_probe.py와 같음, 2026-11-10 결과 보기 전에 정함). 정답 = 사람 92건(다시 본
라벨 반영):
- 장소: 새 모델이 v2보다 많이 맞히고 정확 맥니마 p < 0.10이면 교체
- 움직임 B: 새 모델이 지금 값(분류기)보다 많이 맞히고 p < 0.10이면 교체
- 움직임 A: '새 모델 단독'과 'v2 + 새 모델 섞기'(진로변경·추월일 때만) 중 나은 것이 지금(v2 + v3
  섞기)보다
  많이 맞히고, 진로변경·추월이라고 한 것 중 맞은 비율 50% 이상이면 교체
- 상대 위치·도로 관계(정답 카드): v3보다 많이 맞히면 확인 표시를 새 모델 기준으로(30건 재조립으로
  다시 확인)
'기타'면 A는 직진, B는 카메라 규칙
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_video_human import load_human, mcnemar  # noqa: E402
from score import load_answers  # noqa: E402
from silver import ego_cached  # noqa: E402

from workers.vision.video_model import CACHE, SPECS, label_class, predict  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
SPECIAL = ("진로변경", "추월")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, choices=[s for s in SPECS if s not in ("v2",)])
    args = parser.parse_args()
    human = load_human()
    names = sorted(n for n in human if (CACHE / f"{n}.npy").exists())
    truth = human
    new = {n: predict("", n, spec=args.spec) for n in names}
    v2 = {n: predict("", n, spec="v2") for n in names}
    v3 = {n: predict("", n, spec="v3") for n in names}
    old = json.loads((RESULTS / "video_human_preds.json").read_text(encoding="utf-8"))
    gold = load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx")
    fields = SPECS[args.spec]

    def fixA(v):
        return "직진" if v == "기타" else v

    def fixB(n, v):
        return (
            ego_cached(n).field_opinions(human[n]["location_type"])["movement_B"]["value"]
            if v == "기타"
            else v
        )

    print(f"\n== {args.spec} vs 지금 설정, 사람 정답 {len(names)}건 (다시 본 라벨 반영)")

    def compare(title, f, a_pred, c_pred, c_name):
        sel = [n for n in names if truth[n].get(f)]
        a = sum(a_pred[n] == truth[n][f] for n in sel)
        c = sum(c_pred[n] == truth[n][f] for n in sel)
        b_ = sum(a_pred[n] == truth[n][f] and c_pred[n] != truth[n][f] for n in sel)
        c_ = sum(a_pred[n] != truth[n][f] and c_pred[n] == truth[n][f] for n in sel)
        p = mcnemar(b_, c_)
        print(
            f"  {title:26} {args.spec} {a}/{len(sel)} vs {c_name} {c}/{len(sel)} ({args.spec}만 "
            f"맞힘 {b_}, 반대 {c_}, p={p:.2f})"
        )
        return a, c, p

    verdict = []
    a, c, p = compare(
        "장소",
        "location_type",
        {n: new[n]["location_type"]["value"] for n in names},
        {n: v2[n]["location_type"]["value"] for n in names},
        "v2",
    )
    verdict.append(f"장소: {'교체' if a > c and p < 0.10 else 'v2 유지'}")
    nb = {n: fixB(n, new[n]["movement_B"]["value"]) for n in names}
    clf = {
        n: old.get(n, {}).get("classifier_B") or fixB(n, v2[n]["movement_B"]["value"])
        for n in names
    }
    a, c, p = compare("움직임 B", "movement_B", nb, clf, "분류기(지금 값)")
    compare(
        "  (참고) B vs 영상 모델 v2",
        "movement_B",
        nb,
        {n: fixB(n, v2[n]["movement_B"]["value"]) for n in names},
        "v2",
    )
    verdict.append(f"움직임 B: {'교체' if a > c and p < 0.10 else '분류기 유지'}")

    a2 = {n: fixA(v2[n]["movement_A"]["value"]) for n in names}
    a3 = {n: fixA(v3[n]["movement_A"]["value"]) for n in names}
    cur = {n: a3[n] if a3[n] in SPECIAL else a2[n] for n in names}
    alone = {n: fixA(new[n]["movement_A"]["value"]) for n in names}
    mixn = {n: alone[n] if alone[n] in SPECIAL else a2[n] for n in names}
    best = None
    for title, pred in (("움직임 A: 단독", alone), ("움직임 A: v2+새 모델 섞기", mixn)):
        a, c, p = compare(title, "movement_A", pred, cur, "지금(v2+v3 섞기)")
        sw = [n for n in names if pred[n] in SPECIAL]
        prec = sum(pred[n] == truth[n]["movement_A"] for n in sw) / max(len(sw), 1)
        print(f"    진로변경·추월이라고 한 {len(sw)}건 중 맞음 {prec:.0%}")
        if a > c and prec >= 0.5 and (best is None or a > best[1]):
            best = (title, a)
    for c_ in ("좌회전", "우회전", "진로변경", "추월", "주정차", "출차"):
        sel = [n for n in names if truth[n]["movement_A"] == c_]
        if sel:
            print(
                f"    A {c_} {len(sel)}건: {args.spec} {sum(alone[n] == c_ for n in sel)} | v2 "
                f"{sum(a2[n] == c_ for n in sel)}"
                f" | v3 {sum(a3[n] == c_ for n in sel)}"
            )
    verdict.append(
        f"움직임 A: {best[0].split(': ')[1] + '로 교체' if best else '지금(v2+v3 섞기) 유지'}"
    )
    for f in ("relative_position", "road_priority"):
        if f not in fields:
            continue
        sel = [n for n in names if n in gold and gold[n].get(f)]
        t = {n: label_class("v3", f, gold[n][f]) for n in sel}
        a = sum(new[n][f]["value"] == t[n] for n in sel)
        c = sum(v3[n][f]["value"] == t[n] for n in sel)
        print(
            f"  {f:26} {args.spec} {a}/{len(sel)} vs v3 {c}/{len(sel)} (정답 카드, 왼쪽·오른쪽은 "
            f"'옆')"
        )
        verdict.append(
            f"{f} 확인 표시: {'새 모델 기준으로(30건 재조립 확인)' if a > c else 'v3 유지'}"
        )
    print("\n판정(미리 정한 기준):")
    for v in verdict:
        print("  " + v)


if __name__ == "__main__":
    main()
