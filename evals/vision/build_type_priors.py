"""사고 유형 빈도표를 만든다(유형 고르기에서 '흔한 유형' 가산점으로 쓴다). 학습이 아니라 개수
세기다.

사용:
    .venv/Scripts/python evals/vision/build_type_priors.py
입력: evals/results/vision/labels.csv (scan_labels.py 결과), evals/results/vision/eval_set.csv
결과: workers/vision/type_priors.json  — 평가 영상 30건은 빼고 센다(평가 누수 방지)

세는 단위: 시점(1인칭/3인칭) × 사고 유형 × 방향(블랙박스 차가 유형의 A 역할이면 swap=True).
1인칭 방향은 라벨 박스로 정한다(라벨 박스가 B에만 있으면 블랙박스 차가 유형의 A,
make_answer_sheet.label_swapped).
3인칭은 블랙박스 차가 사고 차가 아니므로 두 방향에 반씩 센다.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evals.vision.make_answer_sheet import label_swapped  # noqa: E402


def main():
    labels = ROOT / "evals/results/vision/labels.csv"
    eval_set = ROOT / "evals/results/vision/eval_set.csv"
    out = ROOT / "workers/vision/type_priors.json"
    with eval_set.open(encoding="utf-8-sig") as f:
        excluded = {r["video_name"] for r in csv.DictReader(f)}
    counts = {"1": Counter(), "3": Counter()}
    used = 0
    with labels.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["video_name"] in excluded or r["map_사고대상"] not in ("차대차", "고속도로"):
                continue
            view, t = r["video_point_of_view"], r["accident_type"]
            if view == "1":
                counts["1"][f"{t}|{label_swapped(r)}"] += 1
            else:
                counts["3"][f"{t}|False"] += 0.5
                counts["3"][f"{t}|True"] += 0.5
            used += 1
    out.write_text(
        json.dumps(
            {
                "note": "평가 30건 제외, AI Hub Validation 라벨 개수",
                "videos": used,
                "counts": {k: dict(v) for k, v in counts.items()},
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"영상 {used}건으로 빈도표 생성 → {out}")
    for view, c in counts.items():
        print(f"  {view}인칭: 유형·방향 {len(c)}종, 상위 5: {c.most_common(5)}")


if __name__ == "__main__":
    main()
