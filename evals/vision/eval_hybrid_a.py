"""움직임 A 섞기 시험: v3가 진로변경·추월이라고 할 때만 v3, 나머지는 v2. 무료(노트북 GPU, 1~2분).

사용:
    .venv/Scripts/python evals/vision/eval_hybrid_a.py

결과를 본 뒤 나온 아이디어라는 점을 기록해 둔다(2026-11-10: v3가 진로변경 3/3, 추월 2/4를 맞혔지만
회전을 잃음).
미리 정한 판정(이 스크립트 결과를 보기 전):
- 섞기를 채택: 92건에서 v2보다 1건 넘게 떨어지지 않고(회전 손해 없음), 30건(정답 카드)에서 v2보다
  많이 맞힐 때
- v3가 진로변경·추월이라고 한 영상 중 실제로 맞은 비율도 본다(잘못 말한 경우가 많으면 채택하지 않음:
  맞은 비율 50% 미만)
v4 비교(2026-11-10, v4 학습 전에 정함): --special v4로 돌려 v3 섞기와 견준다.
- v4로 바꿈: 92건에서 섞기(v2+v4)가 섞기(v2+v3)보다 많이 맞히고(같으면 v3 유지), 진로변경·추월 맞은
  비율 50% 이상
- 상대 위치·도로 관계 확인 표시도 v4로 바꾸는 건 30건 재조립에서 '표시 없이 틀린 칸'이 늘지 않을
  때만
주의: 구준휘님 라벨(62건)은 직진·좌·우·유턴으로만 붙어 있어, 실제 진로변경이 '직진'으로 적혀 있을 수
있다
'기타'면 직진(train_video.py·eval_video_human.py와 같음). 정답은 사람 라벨 그대로
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_video_human import load_human  # noqa: E402
from exp_human_mix import RUNS  # noqa: E402

from workers.vision.video_model import CACHE, predict  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
SPECIAL = ("진로변경", "추월")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--special", default="v3", choices=["v3", "v4", "j3"], help="진로변경·추월을 맡길 모델"
    )
    args = parser.parse_args()
    human = load_human()
    names = sorted(n for n in human if (CACHE / f"{n}.npy").exists())
    vlm = {}
    for run in RUNS:
        for p in (RESULTS / "runs" / run).glob("*.json"):
            if p.name != "run.json":
                c = json.loads(p.read_text(encoding="utf-8"))
                vlm[c["case_id"]] = (
                    c["fields"]["movement_A"]["vision_detail"].get("vlm") or {}
                ).get("value")

    def fix(v):
        return "직진" if v == "기타" else v

    v2 = {n: fix(predict("", n, spec="v2")["movement_A"]["value"]) for n in names}
    v3 = {n: fix(predict("", n, spec=args.special)["movement_A"]["value"]) for n in names}
    print(f"섞기에 쓰는 모델: {args.special} (아래 'v3' 자리에 이 모델 결과가 나옴)")
    mix = {n: v3[n] if v3[n] in SPECIAL else v2[n] for n in names}
    truth = {n: human[n]["movement_A"] for n in names}

    for title, sel in (
        ("사람 정답 92건", names),
        ("정답 카드 30건(이미 쓴 세트)", [n for n in names if n in vlm]),
    ):
        N = len(sel)
        ok = lambda pred: sum(pred[n] == truth[n] for n in sel)  # noqa: E731, B023
        line = f"\n== {title} ({N}건): v2 {ok(v2)} | v3 {ok(v3)} | 섞기 {ok(mix)}"
        if all(n in vlm for n in sel):
            line += f" | VLM {sum(vlm[n] == truth[n] for n in sel)}"
        print(line)
        # 참고 숫자(판정에는 안 씀, 2026-11-10 결과를 본 뒤 추가): 진로변경·추월을 한 묶음으로 볼 때
        m = lambda v: "진로변경" if v == "추월" else v  # noqa: E731
        okm = lambda pred: sum(m(pred[n]) == m(truth[n]) for n in sel)  # noqa: E731, B023
        print(f"  (참고) 진로변경·추월 한 묶음: v2 {okm(v2)} | v3 {okm(v3)} | 섞기 {okm(mix)}")
        switched = [n for n in sel if v3[n] in SPECIAL]
        print(
            f"  v3가 진로변경·추월이라고 한 영상 {len(switched)}건 → 실제 맞음 "
            f"{sum(mix[n] == truth[n] for n in switched)}, "
            f"그중 v2가 맞혔던 것 {sum(v2[n] == truth[n] for n in switched)}"
        )
        for n in switched:
            print(f"    {n}: 정답 {truth[n]} | v3 {v3[n]} | v2 {v2[n]}")

    b = sum(mix[n] == truth[n] for n in names) - sum(v2[n] == truth[n] for n in names)
    sel30 = [n for n in names if n in vlm]
    b30 = sum(mix[n] == truth[n] for n in sel30) - sum(v2[n] == truth[n] for n in sel30)
    sw = [n for n in names if v3[n] in SPECIAL]
    prec = sum(mix[n] == truth[n] for n in sw) / max(len(sw), 1)
    adopt = b >= -1 and b30 > 0 and prec >= 0.5
    print(
        f"\n판정: 92건 차이 {b:+d}, 30건 차이 {b30:+d}, 진로변경·추월 맞은 비율 {prec:.0%} → "
        f"{'섞기 채택' if adopt else '섞기 안 함(v2 유지)'}"
    )


if __name__ == "__main__":
    main()
