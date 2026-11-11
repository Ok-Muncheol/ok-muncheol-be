"""라벨 기반 개선용 세트(silver)를 만든다. 사람 검토 없이 AI Hub 라벨의 사고 유형으로 정답 칸을 채운다. 무료.

왜: 개선용 10건으로는 실행마다의 흔들림(3~5칸)과 효과를 구분할 수 없다(2026-11-09). 코드로 판단하는 방법
(카메라 계산, YOLO, 깊이)은 무료라 많은 영상으로 재면 믿을 수 있다. 최종 평가는 사람이 검토한 20건으로 한다.

사용:
    .venv/Scripts/python evals/vision/make_silver_set.py            # 고르기 + 라벨 정답 + 품질 확인
    .venv/Scripts/python evals/vision/extract_videos.py --eval-set evals/results/vision/silver_set.csv

결과(evals/results/vision/):
- silver_set.csv      고른 영상(eval_set.csv와 같은 열, split=silver). 평가 30건과 겹치지 않음
- silver_answers.csv  라벨로 채운 정답(make_answer_sheet.prefill과 같은 규칙, 블랙박스 차=B). 모르는 칸은 빈칸
품질 확인: 사람이 검토한 정답 카드 30건에 같은 규칙을 적용해 칸별 일치율을 출력한다(라벨 기반 정답의 믿음 정도).
"""

import argparse
import csv
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_answer_sheet import prefill  # noqa: E402
from select_eval_set import pick_diverse, reject_reason  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
# 교차로(MVP) 위주. 나머지 장소도 조금씩
QUOTA = {"사거리교차로(신호등있음)": 25, "사거리교차로(신호등없음)": 25, "T자형교차로": 25, "직선 도로": 10,
         "고속도로(자동차전용도로)포함": 5, "주차장(또는차도가아닌장소)": 4, "회전교차로": 3, "차도와차도가아닌장소": 3}
FIELDS = ["location_type", "road_priority", "signal_A", "signal_B", "movement_A", "movement_B",
          "relative_position", "entry_order"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default=20261110, type=int)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    with (RESULTS / "labels.csv").open(encoding="utf-8-sig") as f:
        labels = list(csv.DictReader(f))
    with (RESULTS / "eval_set.csv").open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames
        eval_rows = list(reader)
    used = {r["video_name"] for r in eval_rows}
    pool = defaultdict(list)
    for r in labels:
        if r["video_point_of_view"] == "1" and r["video_name"] not in used and reject_reason(r) is None:
            pool[r["map_사고장소"]].append(r)
    picked = []
    for place, n in QUOTA.items():
        for r in pick_diverse(sorted(pool[place], key=lambda x: x["video_name"]), n, rng):
            picked.append({**r, "split": "silver"})
    picked.sort(key=lambda r: (r["map_사고장소"], r["video_name"]))

    with (RESULTS / "silver_set.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(picked)
    with (RESULTS / "silver_answers.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["video_name", "accident_type", *FIELDS])
        w.writeheader()
        for r in picked:
            pre = prefill(r)
            w.writerow({"video_name": r["video_name"], "accident_type": r["accident_type"],
                        **{k: pre.get(k, "") for k in FIELDS}})
    print(f"고름 {len(picked)}건: {dict(Counter(r['map_사고장소'] for r in picked))}")
    print(f"→ {RESULTS / 'silver_set.csv'}, silver_answers.csv")

    # 품질 확인: 사람이 검토한 30건에서 라벨 규칙 값과 사람 정답이 얼마나 같은가
    from score import load_answers

    human = load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx")
    full = {r["video_name"]: r for r in labels}
    agree, filled = Counter(), Counter()
    for r in eval_rows:
        pre = prefill({**full[r["video_name"]], **r})
        h = human.get(r["video_name"], {})
        for k in FIELDS:
            if pre.get(k) not in (None, "") and h.get(k) not in (None, ""):
                filled[k] += 1
                agree[k] += str(pre[k]) == str(h[k])
    print("\n라벨 기반 정답 품질(사람 검토 30건과 일치, 라벨로 값이 나오는 칸만):")
    for k in FIELDS:
        print(f"  {k:18} {agree[k]}/{filled[k]}" + (f" ({agree[k] / filled[k] * 100:.0f}%)" if filled[k] else ""))


if __name__ == "__main__":
    main()
