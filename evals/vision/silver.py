"""라벨 기반 개선용 세트(make_silver_set.py) 공통 도구: 정답 읽기, 카메라 계산 결과 저장·재사용.

    .venv/Scripts/python evals/vision/silver.py      # 카메라 계산으로 움직임 B를 라벨 정답과 비교(무료)
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
RESULTS = ROOT / "evals/results/vision"
VIDEOS = ROOT / "data/private/aihub/videos"


def load_silver() -> tuple[list[dict], dict]:
    """(silver_set 줄들, {영상: 라벨 정답 칸})."""
    with (RESULTS / "silver_set.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with (RESULTS / "silver_answers.csv").open(encoding="utf-8-sig") as f:
        answers = {r["video_name"]: r for r in csv.DictReader(f)}
    return rows, answers


def ego_cached(name: str):
    """카메라 계산(ego_motion.analyze_video) 결과를 evals/results/vision/ego_cache/에 저장해 두고 다시 쓴다."""
    from workers.vision.ego_motion import _decide, analyze_video

    path = RESULTS / "ego_cache" / f"{name}.json"
    if path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
        return _decide(d["series"], d["collision"], d["conf"], d["collision"])
    ego = analyze_video(str(VIDEOS / f"{name}.mp4"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"series": ego.series, "collision": ego.collision_frame,
                                "conf": ego.collision_confidence}), encoding="utf-8")
    return ego


def main():
    rows, answers = load_silver()
    ok = n = 0
    confusion = Counter()
    for r in rows:
        name = r["video_name"]
        truth = answers[name]["movement_B"]
        if not truth:
            continue
        ego = ego_cached(name)
        got = ego.field_opinions(answers[name]["location_type"])["movement_B"]["value"]
        n += 1
        ok += got == truth
        confusion[(truth, got)] += 1
    print(f"움직임 B (카메라 계산, 카메라 충돌 순간 기준): {ok}/{n} ({ok / max(n, 1) * 100:.0f}%)")
    print("정답 → 계산 (많은 순):")
    for (t, g), c in confusion.most_common(15):
        print(f"  {t} → {g}: {c}{'' if t == g else '  ✗'}")


if __name__ == "__main__":
    main()
