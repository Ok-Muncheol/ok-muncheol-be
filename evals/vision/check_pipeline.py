"""pipeline(workers/vision/pipeline.py)이 평가 재조립(replay_cards.py)과 같은 카드를 내는지
확인한다. 무료.

저장된 VLM 답(ReplayVLM)을 다시 쓰므로 API를 부르지 않는다. 노트북 GPU로 영상당 수십 초.

사용:
    .venv/Scripts/python evals/vision/check_pipeline.py                 # 30건 전부
    .venv/Scripts/python evals/vision/check_pipeline.py --limit 2       # 2건만 빠르게

비교: 같은 영상의 replay 결과(기본 runs/dev30_1110_typeflag)를 v1으로 바꾼 것 vs pipeline의 v1 카드.
칸마다 값·확인 표시가 다른 수를 센다. 알려진 차이: signal_A 확인 표시(11/11에 신호 교차로에서
항상 표시로 바꿈, typeflag 실행은 그 전).
결과 카드: evals/results/vision/runs/pipeline_check/<영상>.json
"""

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_cards import ReplayVLM  # noqa: E402

from workers.vision.contract import to_contract_card  # noqa: E402
from workers.vision.pipeline import PipelineOptions, analyze_video_sync  # noqa: E402

RUNS = ROOT / "evals/results/vision/runs"
VLM_RUNS = ("1109_1042_s1_ego_dev_openai_v0.2", "1109_1717_s1_ego_yolo_motion_test_openai_v0.2")
VIDEOS = ROOT / "data/private/aihub/videos"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", default=RUNS / "dev30_1110_typeflag", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-j3", action="store_true")
    args = parser.parse_args()
    out_dir = RUNS / "pipeline_check"
    out_dir.mkdir(parents=True, exist_ok=True)

    olds = sorted(
        p for run in VLM_RUNS for p in (RUNS / run).glob("*.json") if p.name != "run.json"
    )
    olds = olds[: args.limit]
    diff_value, diff_status, checked = Counter(), Counter(), 0
    t0 = time.time()
    for i, path in enumerate(olds, 1):
        old = json.loads(path.read_text(encoding="utf-8"))
        name = old["case_id"]
        ref_path = args.reference / f"{name}.json"
        if not ref_path.exists():
            print(f"[{i}] {name}: 비교할 replay 카드 없음, 건너뜀")
            continue
        opts = PipelineOptions(
            vlm=ReplayVLM(old),
            use_j3=not args.no_j3,
            prompt_version=old["model_info"].get("prompt_version", "v0.2"),
        )
        card = analyze_video_sync(name, str(VIDEOS / f"{name}.mp4"), options=opts)
        (out_dir / f"{name}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        ref = to_contract_card(json.loads(ref_path.read_text(encoding="utf-8")))
        diffs = []
        for f, got in card["fields"].items():
            want = ref["fields"].get(f, {})
            if got.get("value") != want.get("value"):
                diff_value[f] += 1
                diffs.append(f"{f} 값 {want.get('value')}→{got.get('value')}")
            if got.get("verification_status") != want.get("verification_status"):
                diff_status[f] += 1
                diffs.append(
                    f"{f} 표시 {want.get('verification_status')}→{got.get('verification_status')}"
                )
        checked += 1
        sec = card["model_info"]["pipeline"]["seconds"]
        print(
            f"[{i}/{len(olds)}] {name} {sum(sec.values()):.0f}초 | 다른 칸 {len(diffs)}"
            + (": " + ", ".join(diffs) if diffs else ""),
            flush=True,
        )
    print(f"\n{checked}건 비교, {time.time() - t0:.0f}초")
    print("값이 다른 칸:", dict(diff_value) or "없음")
    print("확인 표시가 다른 칸:", dict(diff_status) or "없음")
    print(f"카드: {out_dir}")


if __name__ == "__main__":
    main()
