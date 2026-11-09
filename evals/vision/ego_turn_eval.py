"""회전각 규칙만으로 촬영 차량 진행(movement_B)을 채점한다. VLM 호출 없음.

VLM은 궤적 텍스트의 회전 판정을 거의 그대로 따르므로(사람 라벨 64건 중 62건) 규칙 변경의 효과는
규칙만 채점해도 잡힌다. 캐시(--trajectory-cache)가 예전 버전이면 cached_features가 광류만 다시
재서 올린다(YOLO 재실행 없음, 건당 1초 미만). 캐시에 남긴 프레임별 신호로 --margin, --turn-deg를
바꿔 가며 다시 판정할 수 있다.

채점:
- --strict: 촬영 차량=B가 확실한 정답셋(사람 라벨). movement_B와 정확 비교.
- 기본(lenient): 촬영 차량이 A/B 중 누구인지 모르는 AI Hub 라벨. 두 진행 중 하나와 맞으면
  정답(상한).
  두 진행이 같은 건(직진 대 직진 등)은 애매함이 없어 unambiguous로 따로 센다.
- 3인칭(CCTV) 영상은 촬영 차량이 없어 뺀다(--include-cctv로 포함).

    python -m evals.vision.ego_turn_eval --gt data/private/aihub/vision_gt_human.jsonl --strict \
        --out evals/results/ego_turn_human.json
    python -m evals.vision.ego_turn_eval --gt data/private/aihub/vision_gt.jsonl --simple-scope \
        --out evals/results/ego_turn_aihub_simple.json
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from evals.vision import trajectory
from evals.vision.evaluate import read_jsonl, select_rows

TURNS = ("straight", "left_turn", "right_turn", "u_turn")


def judge(features: dict, margin_sec: float, turn_deg: float) -> tuple[str, str, dict]:
    """(전체 누적 규칙 판정, 충돌 전 절단 규칙 판정, 수치)."""
    f = features["features"]
    pre, full, collision = trajectory.split_at_collision(
        f["ego_yaw_per_frame"], f["motion_per_frame"], f["fps"], margin_sec)
    detail = {"yaw_full": round(full, 1), "yaw_pre": round(pre, 1), "collision_sec": collision}
    return (trajectory.classify_turn(full, turn_deg), trajectory.classify_turn(pre, turn_deg),
            detail)


def hit(pred: str, fields: dict, strict: bool) -> bool:
    if strict:
        return pred == fields["movement_B"]
    return pred in (fields["movement_A"], fields["movement_B"])


def _acc(stat: Counter) -> dict:
    n = stat["n"]
    return {"n": n, "old": stat["old"], "new": stat["new"],
            "old_accuracy": round(stat["old"] / n, 4) if n else None,
            "new_accuracy": round(stat["new"] / n, 4) if n else None}


def score(rows: list[dict], cache_dir: Path, strict: bool, margin_sec: float,
          turn_deg: float) -> dict:
    groups: dict[str, Counter] = {"overall": Counter(), "unambiguous": Counter()}
    flips: list[dict] = []
    flip_outcome: Counter = Counter()
    for i, r in enumerate(rows, start=1):
        feats = trajectory.cached_features(Path(r["video_path"]), cache_dir, r["example_id"])
        old, new, detail = judge(feats, margin_sec, turn_deg)
        fields = r["fields"]
        keys = ["overall", f"loc:{fields.get('location_type')}"]
        if not strict and fields["movement_A"] == fields["movement_B"]:
            keys.append("unambiguous")
        for key in keys:
            stat = groups.setdefault(key, Counter())
            stat["n"] += 1
            stat["old"] += hit(old, fields, strict)
            stat["new"] += hit(new, fields, strict)
        if old != new:
            o, n = hit(old, fields, strict), hit(new, fields, strict)
            outcome = ("both" if o and n else "new" if n else "old" if o else "neither")
            flip_outcome[outcome] += 1
            flips.append({"example_id": r["example_id"], "old": old, "new": new,
                          "gt_A": fields["movement_A"], "gt_B": fields["movement_B"],
                          "correct": outcome, **detail})
        if i % 25 == 0 or i == len(rows):
            print(f"[{i}/{len(rows)}]", file=sys.stderr)
    return {"n": len(rows), "strict": strict, "margin_sec": margin_sec, "turn_deg": turn_deg,
            "groups": {k: _acc(v) for k, v in groups.items() if v["n"]},
            "flip_pairs": {f"{a}->{b}": c for (a, b), c in
                           Counter((f["old"], f["new"]) for f in flips).most_common()},
            "flip_outcome": dict(flip_outcome), "flips": flips}


def format_table(report: dict) -> str:
    lines = [f"n={report['n']} strict={report['strict']} margin={report['margin_sec']}s "
             f"turn={report['turn_deg']}도",
             f"{'group':<36}{'n':>5}{'전체누적':>10}{'충돌전절단':>12}{'차이':>6}"]
    for key, g in report["groups"].items():
        lines.append(f"{key:<36}{g['n']:>5}{g['old_accuracy']:>10.3f}{g['new_accuracy']:>12.3f}"
                     f"{g['new'] - g['old']:>+6d}")
    lines.append(f"판정 바뀐 건 {len(report['flips'])}: {report['flip_pairs']}")
    lines.append(f"바뀐 건의 정답 쪽: {report['flip_outcome']} "
                 "(new=절단만 정답, old=전체누적만 정답, both/neither=둘 다 정답/오답)")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gt", type=Path, required=True)
    parser.add_argument("--trajectory-cache", type=Path,
                        default=Path("evals/results/trajectory_cache"))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--strict", action="store_true",
                        help="촬영 차량=B가 확실한 정답셋(사람 라벨). "
                             "기본은 A/B 중 하나와 맞으면 정답")
    parser.add_argument("--all-scope", action="store_true")
    parser.add_argument("--simple-scope", action="store_true")
    parser.add_argument("--min-sharpness", type=float, default=None)
    parser.add_argument("--include-cctv", action="store_true",
                        help="3인칭 영상도 포함(촬영 차량이 없어 기본 제외)")
    parser.add_argument("--margin", type=float, default=trajectory.COLLISION_MARGIN_SEC,
                        help="충돌(차분 피크) 몇 초 전까지 누적할지")
    parser.add_argument("--turn-deg", type=float, default=trajectory.TURN_DEG)
    args = parser.parse_args()
    rows = select_rows(read_jsonl(args.gt), mvp_only=not args.all_scope,
                       min_sharpness=args.min_sharpness, simple_scope=args.simple_scope)
    rows = [r for r in rows if r.get("video_path")
            and (args.include_cctv or r.get("point_of_view") == 1)]
    report = score(rows, args.trajectory_cache, args.strict, args.margin, args.turn_deg)
    report["gt"] = str(args.gt)
    report["filters"] = {"simple_scope": args.simple_scope, "min_sharpness": args.min_sharpness,
                         "include_cctv": args.include_cctv}
    print(format_table(report))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"-> {args.out}")


if __name__ == "__main__":
    main()
