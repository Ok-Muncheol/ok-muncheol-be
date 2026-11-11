"""이미 돌린 실행 결과(1차 VLM 답이 저장된 카드)로 '점수로 유형 고르기'를 다시 계산한다. API를 쓰지 않아 무료.

사용:
    .venv/Scripts/python evals/vision/experiments/rescore_type.py evals/results/vision/runs/<원래 실행 폴더>
결과: evals/results/vision/runs/<원래 폴더>_typescore/  (그다음 score.py로 채점)

원래 실행에 카메라 계산(--ego)이 있었으면 그 결과(탐지 의견)도 그대로 쓴다.
가중치·빈도 반영을 바꿔 볼 때 매번 VLM을 다시 부르지 않으려고 만든 도구다.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from workers.vision.fact_card import assemble_card, describe_choice, score_type  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--eval-set", default=ROOT / "evals/results/vision/eval_set.csv", type=Path)
    parser.add_argument("--prior-weight", type=float, help="흔한 유형 가산점 세기(기본: accident_types.PRIOR_WEIGHT). 0이면 빈도 안 씀")
    args = parser.parse_args()
    with args.eval_set.open(encoding="utf-8-sig") as f:
        views = {r["video_name"]: int(r["video_point_of_view"]) for r in csv.DictReader(f)}
    suffix = "" if args.prior_weight is None else f"_p{args.prior_weight:g}"
    out_dir = args.run_dir.parent / f"{args.run_dir.name}_typescore{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)

    n = 0
    for path in sorted(args.run_dir.glob("*.json")):
        if path.name == "run.json":
            continue
        card = json.loads(path.read_text(encoding="utf-8"))
        info = card["model_info"]
        raw = info.get("raw_output")
        if not raw:
            print(f"건너뜀(1차 답 없음): {path.name}")
            continue
        viewpoint = views.get(card["case_id"], 1)
        # 원래 실행의 탐지 의견(카메라 계산 등)을 그대로 살린다
        dets = {k: f["vision_detail"]["detection"] for k, f in card["fields"].items()
                if f.get("vision_detail", {}).get("detection")}
        ego_movement = (dets.get("movement_B") or {}).get("value")
        pick = score_type(raw, viewpoint, ego_movement, args.prior_weight)
        new_info = {k: v for k, v in info.items() if k not in ("type_choice", "raw_output")}
        new_info["stage"] = (info.get("stage") or "vlm") + "+typescore(재계산)"
        if pick:
            c = pick["candidate"]
            new_info["type_choice"] = {"accident_type": c["type"], "swap": c["swap"], "description": describe_choice(c),
                                       "confidence": pick["confidence"], "reason": pick["reason"],
                                       "n_candidates": pick["n_candidates"], "mode": "score", "top3": pick["top3"]}
        new = assemble_card(card["case_id"], raw, {"field_opinions": dets} if dets else None,
                            info.get("frames", []), info.get("collision_hint", 75), viewpoint, new_info, pick)
        new["model_info"]["raw_output"] = raw
        (out_dir / path.name).write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
        if pick:
            print(f"{card['case_id'][:24]} → 유형 {pick['candidate']['type']} (확신 {pick['confidence']})")
    print(f"\n{n}건 다시 계산 → {out_dir}")
    print(f"채점: .venv\\Scripts\\python evals\\vision\\score.py {out_dir}")


if __name__ == "__main__":
    main()
