"""카메라 움직임 계산(ego_motion)만 정답 카드와 비교한다. API를 쓰지 않아 무료, 노트북에서 계산.

사용:
    .venv/Scripts/python evals/vision/eval_ego.py               # 개선용(dev) 1인칭 영상
    .venv/Scripts/python evals/vision/eval_ego.py --split test --final # 최종 평가(마지막에 한 번만)

출력: 영상별 계산값(기준값 정하는 데 씀)과 B 움직임·충돌 순간 정답률.
결과: evals/results/vision/ego/<split>.csv
"""

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evals.vision.score import load_answers  # noqa: E402
from workers.vision.ego_motion import THRESHOLDS, analyze_video  # noqa: E402
from workers.vision.fields import frame_to_ms, same_value  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--eval-set", default=ROOT / "evals/results/vision/eval_set.csv", type=Path)
    parser.add_argument("--videos", default=ROOT / "data/private/aihub/videos", type=Path)
    parser.add_argument(
        "--answers", default=ROOT / "evals/vision/answers/answer_cards.xlsx", type=Path
    )
    args = parser.parse_args()
    if args.split == "test" and not args.final:
        raise SystemExit("최종 평가용(test)은 --final을 붙여야 돕니다.")

    answers = load_answers(args.answers)
    with args.eval_set.open(encoding="utf-8-sig") as f:
        rows = [
            r
            for r in csv.DictReader(f)
            if r["split"] == args.split and r["video_point_of_view"] == "1"
        ]

    print(f"기준값: {THRESHOLDS}\n")
    print(
        f"{'영상':26} {'정답B움직임':10} {'계산':8} {'yaw':>6} {'peak':>6} {'전진':>6} {'속도':>5} "
        f"{'무늬':>4} "
        f"| {'정답충돌':>6} {'계산충돌':>6}"
    )
    out, mv_ok, ct_ok, ct_n = [], 0, 0, 0
    for r in rows:
        name = r["video_name"]
        ego = analyze_video(str(args.videos / f"{name}.mp4"))
        ans = answers.get(name, {})
        truth_mv = ans.get("movement_B")
        truth_ms = ans.get("collision_time_ms")
        got_ms = frame_to_ms(ego.collision_frame) if ego.collision_frame else None
        mv = truth_mv == ego.movement
        ct = (
            truth_ms is not None
            and got_ms is not None
            and same_value("collision_time_ms", got_ms, truth_ms)
        )
        mv_ok += mv
        if truth_ms is not None:
            ct_ok += ct
            ct_n += 1
        tf = round(truth_ms * 15 / 1000) if truth_ms else None
        print(
            f"{name:26} {str(truth_mv):10} {ego.movement:8} {ego.yaw:6.2f} {ego.yaw_peak:6.2f} "
            f"{ego.forward:6.2f} "
            f"{ego.speed:5.2f} {ego.texture:4.1f} | {str(tf):>6} {str(ego.collision_frame):>6} "
            f"{'✓' if mv else '✗'}{'✓' if ct else '✗'}"
        )
        out.append(
            {
                "video": name,
                "truth_movement_B": truth_mv,
                "ego_movement": ego.movement,
                "ego_conf": ego.movement_confidence,
                "yaw": ego.yaw,
                "yaw_peak": ego.yaw_peak,
                "forward": ego.forward,
                "speed": ego.speed,
                "texture": ego.texture,
                "truth_collision_frame": tf,
                "ego_collision_frame": ego.collision_frame,
                "collision_conf": ego.collision_confidence,
                "movement_ok": mv,
                "collision_ok": ct,
            }
        )

    n = len(rows)
    print(f"\nB 움직임 정답률: {mv_ok}/{n} = {mv_ok / n:.0%}" if n else "영상 없음")
    if ct_n:
        print(f"충돌 순간(±3프레임) 정답률: {ct_ok}/{ct_n} = {ct_ok / ct_n:.0%}")
    out_dir = ROOT / "evals/results/vision/ego"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{args.split}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]) if out else ["video"])
        w.writeheader()
        w.writerows(out)
    print(f"저장: {out_dir / (args.split + '.csv')}")


if __name__ == "__main__":
    main()
