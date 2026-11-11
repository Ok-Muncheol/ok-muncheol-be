"""움직임 A 확대 질문(evals/vision/experiments/crop_question.py)을 정답·기존 VLM 답과 비교한다. 💰
영상 1건당 약 5원.

사용:
    .venv/Scripts/python evals/vision/experiments/eval_crop_a.py --vlm-run
    evals/results/vision/runs/<기존 실행 폴더>

- 상대 차: YOLO 결과(evals/results/vision/yolo/)에서 pick_opponent로 고른 차
- 충돌 순간: 기존 실행에서 VLM이 찾은 값(없으면 YOLO 박스가 가장 컸던 프레임)
- 카메라 움직임 요약: 기존 실행 카드의 질문 문맥에 들어 있던 글
결과: evals/results/vision/crop_a/<시각>/ (영상별 답 JSON, 보낸 사진 JPG, summary.json)
"""

import argparse
import base64
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 이 폴더의 실험 모듈
from crop_question import ask_movement_a  # noqa: E402

from evals.vision.eval_boxes import MATCH_IOU, track_iou  # noqa: E402
from evals.vision.gt_tracks import load_gt_tracks  # noqa: E402
from evals.vision.score import load_answers  # noqa: E402
from workers.vision.detection import detect_and_track, pick_opponent  # noqa: E402
from workers.vision.fact_card import OpenAIVLM  # noqa: E402
from workers.vision.fields import FPS  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vlm-run", type=Path, required=True)
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--eval-set", default=ROOT / "evals/results/vision/eval_set.csv", type=Path)
    parser.add_argument(
        "--answers", default=ROOT / "evals/vision/answers/answer_cards.xlsx", type=Path
    )
    parser.add_argument("--videos", default=ROOT / "data/private/aihub/videos", type=Path)
    parser.add_argument("--yolo-dir", default=ROOT / "evals/results/vision/yolo", type=Path)
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
    out = ROOT / "evals/results/vision/crop_a" / f"{datetime.now():%m%d_%H%M}_{args.split}"
    out.mkdir(parents=True, exist_ok=True)
    vlm = OpenAIVLM(model=args.model)

    crop_ok = vlm_ok = n = 0
    tin = tout = 0
    summary = []
    for r in rows:
        name = r["video_name"]
        truth = answers.get(name, {}).get("movement_A")
        card = json.loads((args.vlm_run / f"{name}.json").read_text(encoding="utf-8"))
        vlm_value = card["fields"]["movement_A"]["value"]
        ms = card["fields"]["collision_time_ms"]["value"]
        det = detect_and_track(
            str(args.videos / f"{name}.mp4"), cache_path=args.yolo_dir / f"{name}.json"
        )
        hit = round(ms * FPS / 1000) if isinstance(ms, (int, float)) else None
        #  확대 사진은 충돌 '전' 모습이 필요하다. 박스가 가장 컸던 차는 추적이 끊겨 충돌 뒤 번호만
        # 남는 경우가 있어
        # (개선용 3건), VLM 충돌 순간 근처에서 고른다(상대 차 7/10, 사진 만들 수 있는 영상 8/10).
        track_a, why = pick_opponent(det, hit_frame=hit)
        hit = hit or why.get("peak_frame")
        gt_a = load_gt_tracks(r, ROOT)[0].get("A", [])
        m, _ = track_iou(gt_a, track_a) if (gt_a and track_a) else (None, 0)
        pick_right = m is not None and m >= MATCH_IOU
        ego_text = card["model_info"].get("context")  # --ego 실행이면 카메라 움직임 계산 요약 전체
        try:
            res = (
                ask_movement_a(vlm, str(args.videos / f"{name}.mp4"), track_a, hit, ego_text)
                if track_a
                else None
            )
        except Exception as e:  # 한 건 실패로 멈추지 않는다
            print(f"{name} 실패: {e!r}")
            continue
        got = res["value"] if res else None
        if res:
            (out / f"{name}.jpg").write_bytes(base64.b64decode(res.pop("image")))
            tin += res["tokens"][0] or 0
            tout += res["tokens"][1] or 0
        n += 1
        got = (
            got if got not in (None, "불명") else vlm_value
        )  # 사진을 못 만들거나 모르면 기존 VLM 답을 쓴다
        crop_ok += got == truth
        vlm_ok += vlm_value == truth
        rec = {
            "video": name,
            "truth": truth,
            "vlm": vlm_value,
            "crop": got,
            "pick_right": pick_right,
            "hit_frame": hit,
            **(res or {"note": "잘라낼 박스 부족"}),
        }
        summary.append(rec)
        (out / f"{name}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(
            f"{name[:24]} 정답={truth} | 기존 VLM={vlm_value}{'✓' if vlm_value == truth else '✗'}"
            f" | 확대={got}{'✓' if got == truth else '✗'} (상대 차 {'맞음' if pick_right else '틀림'})"
        )
        if res:
            print(f"    관찰: {(res.get('observations') or '')[:160]}")
    usd = tin / 1e6 * 2.5 + tout / 1e6 * 10 if args.model == "gpt-4o" else None
    print(
        f"\n움직임 A: 확대 질문(사진 없거나 불명이면 기존 답) {crop_ok}/{n} vs 기존 VLM {vlm_ok}/{n}"
    )
    print(
        f"토큰 입력 {tin:,} / 출력 {tout:,}" + (f" → 약 {usd * 1400:,.0f}원(어림값)" if usd else "")
    )
    (out / "summary.json").write_text(
        json.dumps(
            {"crop_ok": crop_ok, "vlm_ok": vlm_ok, "n": n, "rows": summary},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"→ {out}")


if __name__ == "__main__":
    main()
