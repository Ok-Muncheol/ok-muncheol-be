"""깊이 기반 상대 차 궤적(evals/vision/experiments/depth3d.py)으로 판단한 움직임 A를 정답과
비교한다. 무료(로컬 GPU).

사용:
    .venv/Scripts/python evals/vision/experiments/eval_depth_a.py --set silver # 라벨 기반 100건
    .venv/Scripts/python evals/vision/experiments/eval_depth_a.py --set dev --vlm-run
    evals/results/vision/runs/<실행>  # 사람 정답 10건

충돌 순간: dev는 VLM이 찾은 값(서비스와 같은 조건), silver는 VLM 실행이 없어 카메라 계산 값(없으면
YOLO 박스가 가장 컸던 프레임).
궤적은 evals/results/vision/depth_traj/<영상>.json에 저장해 다시 쓴다(규칙만 바꿔 재채점할 때 빠름).
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # evals/vision 공통 도구
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 이 폴더의 실험 모듈
from depth3d import classify_movement, opponent_world_track  # noqa: E402
from silver import ego_cached, load_silver  # noqa: E402

from workers.vision.detection import detect_and_track, pick_opponent  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
VIDEOS = ROOT / "data/private/aihub/videos"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", default="silver", choices=["silver", "dev"])
    parser.add_argument("--vlm-run", type=Path, help="dev: VLM 충돌 순간을 가져올 실행 폴더")
    parser.add_argument("--fresh", action="store_true", help="저장된 궤적을 무시하고 다시 계산")
    args = parser.parse_args()

    if args.set == "silver":
        rows, answers = load_silver()
        truth_of = {k: v["movement_A"] for k, v in answers.items()}
    else:
        from score import load_answers

        with (RESULTS / "eval_set.csv").open(encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r["split"] == "dev"]
        gold = load_answers(ROOT / "evals/vision/answers/answer_cards.xlsx")
        truth_of = {k: v["movement_A"] for k, v in gold.items()}

    cache = RESULTS / "depth_traj"
    cache.mkdir(parents=True, exist_ok=True)
    ok = n = none = 0
    conf = Counter()
    for r in rows:
        name = r["video_name"]
        truth = truth_of.get(name)
        if not truth:
            continue
        det = detect_and_track("", cache_path=RESULTS / "yolo" / f"{name}.json")
        ego = ego_cached(name)
        hit = None
        if args.vlm_run:
            ms = json.loads((args.vlm_run / f"{name}.json").read_text(encoding="utf-8"))["fields"][
                "collision_time_ms"
            ]["value"]
            hit = round(ms * 15 / 1000) if isinstance(ms, (int, float)) else None
        track_a, why = pick_opponent(det, hit_frame=hit, stitch=True)
        hit = hit or ego.collision_frame or why.get("peak_frame")
        path = cache / f"{name}__{args.set}.json"
        if path.exists() and not args.fresh:
            traj = json.loads(path.read_text(encoding="utf-8"))
        else:
            all_boxes = {}
            for t in det["tracks"].values():
                for b in t["boxes"]:
                    all_boxes.setdefault(b[0], []).append(tuple(b[:5]))
            traj = (
                opponent_world_track(
                    str(VIDEOS / f"{name}.mp4"), track_a, ego.series["dx"], hit, all_boxes
                )
                if track_a and hit
                else []
            )
            path.write_text(json.dumps(traj), encoding="utf-8")
        res = classify_movement(traj)
        got = res["value"]
        n += 1
        none += got is None
        ok += got == truth
        conf[(truth, got)] += 1
        if args.set == "dev":
            print(f"{name[:24]} 정답={truth} 계산={got} {res['why']}")
    print(
        f"\n움직임 A (깊이 궤적, {args.set}): {ok}/{n} ({ok / max(n, 1) * 100:.0f}%), 판단 못 함 "
        f"{none}건"
    )
    base = Counter(truth_of[r["video_name"]] for r in rows if truth_of.get(r["video_name"]))
    top, cnt = base.most_common(1)[0]
    print(f"찍기 기준(전부 '{top}'): {cnt}/{n} ({cnt / max(n, 1) * 100:.0f}%)")
    print("정답 → 계산 (많은 순):")
    for (t, g), c in conf.most_common(15):
        print(f"  {t} → {g}: {c}{'' if t == g else '  ✗'}")


if __name__ == "__main__":
    main()
