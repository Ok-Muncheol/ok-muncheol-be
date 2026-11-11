"""박스 위치로 코드가 판단한 칸(box_features)을 정답, 그리고 VLM 답과 비교한다. API를 쓰지 않아
무료.

사용:
    .venv/Scripts/python evals/vision/eval_boxes.py --vlm-run evals/results/vision/runs/<VLM 실행
    폴더>
    .venv/Scripts/python evals/vision/eval_boxes.py --boxes yolo --vlm-run
    evals/results/vision/runs/<VLM 실행 폴더>
    .venv/Scripts/python evals/vision/eval_boxes.py --boxes yolo --use-ego ... # 카메라 계산의 충돌
    순간으로
    상대 차 찾기

--boxes gt  : AI Hub 정답 박스(영상당 12~17프레임, 상대 차가 라벨로 정해져 있음). 13번 실험.
--boxes yolo: YOLO 탐지·추적 + 상대 차 고르기(workers/vision/detection.py). 서비스와 같은 조건.
              YOLO 결과는 evals/results/vision/yolo/<영상>.json에 저장해 두고 다시 쓴다(처음 한 번만
              GPU로 돌림).
              추가로 본다: 상대 차를 맞게 골랐나(정답 A 박스와 겹침), 신호등이 보였나(C안: 신호 B
              판단 보조).
1인칭 영상만 본다(카메라 = 블랙박스 차 B 시점).
"""

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evals.vision.gt_tracks import load_gt_tracks  # noqa: E402
from evals.vision.score import load_answers  # noqa: E402

#  noqa: E402
from workers.vision.box_features import BOX_THRESHOLDS, box_opinions, relative_before_hit  # noqa: E402
from workers.vision.fields import same_value  # noqa: E402

FIELDS = ("collision_part_B", "relative_position", "collision_time_ms")
MATCH_IOU = 0.5  # 고른 차와 정답 A 박스의 겹침(중앙값)이 이 이상이면 같은 차로 본다


def iou(a, b) -> float:
    ax, ay, aw, ah = a[1:5]
    bx, by, bw, bh = b[1:5]
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def track_iou(gt: list, boxes: list) -> tuple[float | None, int]:
    """같은 프레임끼리 겹침의 중앙값과 겹친 프레임 수."""
    by_frame = {b[0]: b for b in boxes}
    vals = [iou(g, by_frame[g[0]]) for g in gt if g[0] in by_frame]
    return (statistics.median(vals) if vals else None), len(vals)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--vlm-run", type=Path, help="비교할 VLM 실행 폴더(없으면 박스 판단만 채점)"
    )
    parser.add_argument("--boxes", default="gt", choices=["gt", "yolo"])
    parser.add_argument(
        "--use-ego",
        action="store_true",
        help="yolo: 카메라 계산의 충돌 순간 근처에서 상대 차를 찾는다",
    )
    parser.add_argument(
        "--rel",
        default="first",
        choices=["first", "before_hit"],
        help="상대 위치 규칙: first=처음 보인 곳(기존), before_hit=충돌 약 1초 전 위치(VLM 충돌 "
             "순간 기준)",
    )
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

    print(
        f"박스: {args.boxes}{' + 카메라 충돌 '
                           '순간' if args.use_ego else ''} | 기준값: {BOX_THRESHOLDS}\n"
    )
    box_ok = {k: 0 for k in FIELDS}
    vlm_ok = {k: 0 for k in FIELDS}
    n = {k: 0 for k in FIELDS}
    picked_right = picked_n = detected_any = 0
    sig = {"before": 0, "after": 0, "n": 0}
    for r in rows:
        name = r["video_name"]
        gt_tracks, (w, h) = load_gt_tracks(r, ROOT)
        gt_a = gt_tracks.get("A", [])
        pick_note = ""
        if args.boxes == "gt":
            track_a = gt_a
        else:
            from workers.vision.detection import detect_and_track, light_summary, pick_opponent

            video = args.videos / f"{name}.mp4"
            det = detect_and_track(str(video), cache_path=args.yolo_dir / f"{name}.json")
            hit = None
            if args.use_ego:
                from workers.vision.ego_motion import analyze_video

                hit = analyze_video(str(video)).collision_frame
            track_a, why = pick_opponent(det, hit_frame=hit)
            w, h = det["size"]
            # 상대 차를 맞게 골랐나: 정답 A 박스와 같은 프레임끼리 겹침
            if gt_a:
                picked_n += 1
                m, k = track_iou(gt_a, track_a) if track_a else (None, 0)
                ok = m is not None and m >= MATCH_IOU
                picked_right += ok
                # 고른 차가 틀렸다면, YOLO가 정답 차를 잡기는 했나
                best = max(
                    (
                        track_iou(gt_a, [tuple(b[:5]) for b in t["boxes"]])[0] or 0.0
                        for t in det["tracks"].values()
                    ),
                    default=0.0,
                )
                detected_any += best >= MATCH_IOU
                pick_note = (
                    f"상대 차 {'✓' if ok else '✗'} (겹침 {m if m is None else round(m, 2)}, "
                    f"{k}프레임"
                    f"{'' if ok else f'; YOLO가 정답 차를 잡은 최대 겹침 {best:.2f}'})"
                )
            # 신호등(C안): 충돌 순간까지 신호등이 안 보이면 신호 B를 '불명'으로
            truth_sig = answers.get(name, {}).get("signal_B")
            lights = light_summary(det, until_frame=(hit or 75))
            vlm_sig = None
            if args.vlm_run and (args.vlm_run / f"{name}.json").exists():
                vlm_sig = json.loads((args.vlm_run / f"{name}.json").read_text(encoding="utf-8"))[
                    "fields"
                ]["signal_B"]["value"]
            if truth_sig and vlm_sig is not None:
                after = vlm_sig if lights["visible"] else "불명"
                sig["n"] += 1
                sig["before"] += vlm_sig == truth_sig
                sig["after"] += after == truth_sig
            pick_note += (
                f" | 신호등 {'보임' if lights['visible'] else '안 보임'}({lights['frames']}프레임)"
                f" 정답 신호B={truth_sig} VLM={vlm_sig}"
            )
            pick_note = f"{why} {pick_note}"
        res = box_opinions(track_a, w, h)
        vlm_card = None
        if args.vlm_run and (args.vlm_run / f"{name}.json").exists():
            vlm_card = json.loads((args.vlm_run / f"{name}.json").read_text(encoding="utf-8"))
        if args.rel == "before_hit" and res:
            # 충돌 순간: VLM이 찾은 값(개선용 6/10), 없으면 박스가 가장 컸던 프레임
            vlm_ms = (vlm_card or {}).get("fields", {}).get("collision_time_ms", {}).get("value")
            hit = (
                round(vlm_ms * 15 / 1000)
                if isinstance(vlm_ms, (int, float))
                else res["why"]["hit_frame"]
            )
            rel = relative_before_hit(track_a, w, h, hit)
            if rel:
                res["opinions"]["relative_position"] = rel
                res["why"]["rel"] = rel["why"]
        ans = answers.get(name, {})
        cells = []
        for k in FIELDS:
            truth = ans.get(k)
            if truth in (None, ""):
                continue
            n[k] += 1
            got = (res.get("opinions", {}).get(k) or {}).get("value")
            ok = got is not None and same_value(
                k, got, truth if k == "collision_time_ms" else str(truth)
            )
            box_ok[k] += ok
            vgot = None
            if vlm_card:
                vgot = vlm_card["fields"][k]["value"]
                vlm_ok[k] += same_value(k, vgot, truth if k == "collision_time_ms" else str(truth))
            show = lambda v, k=k: (  # noqa: E731
                round(v * 15 / 1000)
                if k == "collision_time_ms" and isinstance(v, (int, float))
                else v
            )
            cells.append(
                f"{k[:14]}: 정답={show(truth)} 박스={show(got)}{'✓' if ok else '✗'}"
                + (f" VLM={show(vgot)}" if vlm_card else "")
            )
        print(f"{name[:24]} {res.get('why', '박스 부족')}")
        if pick_note:
            print(f"    {pick_note}")
        for c in cells:
            print("    " + c)
    print("\n칸별 정답 (1인칭)")
    for k in FIELDS:
        line = f"  {k:18} 박스 {box_ok[k]}/{n[k]}"
        if args.vlm_run:
            line += f" | VLM {vlm_ok[k]}/{n[k]}"
        print(line)
    if args.boxes == "yolo":
        print(
            f"\n상대 차를 맞게 고름: {picked_right}/{picked_n}  (YOLO가 정답 차를 잡기는 함: "
            f"{detected_any}/{picked_n})"
        )
        if sig["n"]:
            print(
                f"신호 B (C안: 신호등 안 보이면 불명): VLM 그대로 {sig['before']}/{sig['n']} → "
                f"적용 후 {sig['after']}/{sig['n']}"
            )


if __name__ == "__main__":
    main()
