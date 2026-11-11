"""평가 영상에 사실 카드 생성을 돌리고 결과를 저장한다.

사용:
    .venv/Scripts/python evals/vision/run_cards.py                     # 1단계(VLM 단독), 개선용 10건
    .venv/Scripts/python evals/vision/run_cards.py --limit 2            # 2건만 빠르게
    .venv/Scripts/python evals/vision/run_cards.py --split test --final # 최종 평가(마지막에 한 번만)
    .venv/Scripts/python evals/vision/run_cards.py --vlm openai --stage 2   # OpenAI gpt-4o-mini (유료)

결과: evals/results/vision/runs/<run_id>/<video>.json, run.json(설정·시간)
최종 평가용(test)은 --final 없이는 돌지 않는다. 프롬프트·코드를 고치는 데 쓰지 않기 위해서다.
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from workers.vision.fact_card import (  # noqa: E402
    PROMPT_VERSION, PROMPT_VERSIONS, ClaudeVLM, OllamaVLM, OpenAIVLM, extract_fact_card_sync,
)
from evals.vision.gt_tracks import load_gt_tracks  # noqa: E402
from workers.vision.ego_motion import analyze_video  # noqa: E402


# 100만 토큰당 (입력, 출력) 달러. 2026-11-06 기준 어림값 — 정확한 금액은 OpenAI 사용량 페이지에서 확인
PRICE_PER_M = {"gpt-4o": (2.50, 10.00), "gpt-4o-mini": (0.15, 0.60)}
KRW_PER_USD = 1400


def main():
    parser = argparse.ArgumentParser()
    # human: 구준휘님 사람 라벨 62건(움직임 A·B만 정답, evals/results/vision/human62_set.csv, 2026-11-10)
    parser.add_argument("--split", default="dev", choices=["dev", "test", "human"])
    parser.add_argument("--final", action="store_true", help="최종 평가용(test)을 돌릴 때만 붙인다")
    parser.add_argument("--stage", default="1", choices=["1", "2"], help="1: VLM 단독, 2: VLM + 정답 박스 궤적")
    parser.add_argument("--vlm", default="openai", choices=["ollama", "openai", "claude"],
                        help="openai(기본): OPENAI_API_KEY 필요(유료) / ollama: 노트북 로컬 Qwen / claude: ANTHROPIC_API_KEY 필요")
    parser.add_argument("--model", help="기본: openai=gpt-4o, ollama=qwen3.5:9b, claude=claude-opus-5-5")
    parser.add_argument("--prompt", default=PROMPT_VERSION, choices=list(PROMPT_VERSIONS),
                        help="질문 버전. v0.1: 처음 / v0.2: 1차 개선(기본, 65%%) / "
                             "v0.3: 신호·좌우·A 움직임 보강(58%%, 과보정) / v0.4: v0.2 + 신호 규칙만")
    parser.add_argument("--price-in", type=float, help="표에 없는 모델의 입력 단가(100만 토큰당 달러). 비용 계산용")
    parser.add_argument("--price-out", type=float, help="표에 없는 모델의 출력 단가(100만 토큰당 달러)")
    parser.add_argument("--type", nargs="?", const="score", choices=["score", "choose"], dest="type_select",
                        help="사고 유형을 골라 장소·움직임 등을 채운다. score(기본): 코드가 점수로 고름(추가 비용 없음) / "
                             "choose: VLM이 후보 목록에서 고름(비용 약 2배)")
    parser.add_argument("--ego", action="store_true",
                        help="1인칭 영상에 카메라 움직임 계산(무료, 노트북)을 붙인다: VLM 힌트 + 탐지 의견")
    parser.add_argument("--effort", help="openai: 추론 모델의 reasoning_effort / claude: low~max (기본 high)")
    parser.add_argument("--yolo", action="store_true",
                        help="YOLO로 상대 차를 골라 박스 판단(충돌 부위 B)을 카드에 넣는다(1인칭)")
    parser.add_argument("--motion", action="store_true",
                        help="움직임 분류기(motion_model)로 움직임 A·B를 정한다. --ego 필요(1인칭)")
    parser.add_argument("--video", action="store_true",
                        help="영상 모델(video_model, 무료)로 장소를 정한다(1인칭). 2026-11-10")
    parser.add_argument("--light-fact", action="store_true",
                        help="YOLO 신호등 탐지 사실 한 줄을 질문에 붙인다(장소 판단 보조, 1인칭)")
    parser.add_argument("--mark", action="store_true",
                        help="D안(버림, 2026-11-09): YOLO가 고른 상대 차에 빨간 박스 'A'를 그려 보낸다")
    parser.add_argument("--yolo-dir", default=ROOT / "evals/results/vision/yolo", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--eval-set", default=ROOT / "evals/results/vision/eval_set.csv", type=Path)
    parser.add_argument("--videos", default=ROOT / "data/private/aihub/videos", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.split == "test" and not args.final:
        raise SystemExit("최종 평가용(test)은 --final을 붙여야 돕니다. 개선 중에는 dev만 쓰세요.")

    with args.eval_set.open(encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["split"] == args.split][: args.limit]
    if args.vlm == "openai":
        # 교육사 지침: GPT-4o mini 또는 그 바로 위 모델까지만 사용(비용 제한)
        vlm = OpenAIVLM(model=args.model or "gpt-4o", reasoning_effort=args.effort)
    elif args.vlm == "claude":
        vlm = ClaudeVLM(model=args.model or "claude-opus-5-5", effort=args.effort or "high")
    else:
        vlm = OllamaVLM(model=args.model or "qwen3.5:9b")
    run_id = args.run_id or (f"{datetime.now():%m%d_%H%M}_s{args.stage}{'_ego' if args.ego else ''}"
                             f"{'_yolo' if args.yolo else ''}{'_motion' if args.motion else ''}"
                             f"{'_video' if args.video else ''}"
                             f"{'_light' if args.light_fact else ''}"
                             f"{'_mark' if args.mark else ''}"
                             f"{'_type' + ('' if args.type_select == 'choose' else 'score') if args.type_select else ''}"
                             f"_{args.split}_{args.vlm}_{args.prompt}")
    out_dir = ROOT / "evals/results/vision/runs" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {"run_id": run_id, "stage": args.stage, "split": args.split, "vlm": vlm.name,
            "prompt_version": args.prompt, "started": datetime.now().isoformat(timespec="seconds"),
            "videos": [], "failed": []}
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        name = r["video_name"]
        try:
            detections = None
            if args.stage == "2":
                tracks, size = load_gt_tracks(r, ROOT)
                detections = {"tracks": tracks, "size": size, "source": "정답 박스"}
            if args.ego and r["video_point_of_view"] == "1":
                ego = analyze_video(str(args.videos / f"{name}.mp4"))
                detections = {**(detections or {}), "hint": ego.summary(), "ego": ego}
            if args.yolo and r["video_point_of_view"] == "1":
                from workers.vision.detection import box_detections

                detections = {**(detections or {}),
                              **box_detections(str(args.videos / f"{name}.mp4"), args.yolo_dir / f"{name}.json")}
            if args.motion and args.ego and r["video_point_of_view"] == "1":
                from workers.vision.detection import detect_and_track, pick_opponent
                from workers.vision.motion_model import motion_opinions

                det = detect_and_track(str(args.videos / f"{name}.mp4"), cache_path=args.yolo_dir / f"{name}.json")
                from workers.vision.scene_embed import embed_video, predict_scene

                track_m, why_m = pick_opponent(det, stitch=True)  # 학습 때와 같은 방식
                emb = embed_video(str(args.videos / f"{name}.mp4"), name)
                scene = {k: p for k in ("location_type", "road_priority") if (p := predict_scene(k, emb))}
                detections = {**(detections or {}), "motion": motion_opinions(det, ego, track_m, why_m, emb),
                              "scene": scene}
            if args.video and r["video_point_of_view"] == "1":
                from workers.vision.type_model import type_opinions
                from workers.vision.video_model import predict

                video_path = str(args.videos / f"{name}.mp4")
                detections = {**(detections or {}), "video": predict(video_path, name),
                              "video_v3": predict(video_path, name, spec="v3"), "video_j3": predict(video_path, name, spec="j3"),
                          "type": type_opinions(video_path, name)}  # 상대 위치·도로 관계 확인 표시용
            if args.light_fact and r["video_point_of_view"] == "1":
                from workers.vision.detection import detect_and_track, light_fact

                det = detect_and_track(str(args.videos / f"{name}.mp4"), cache_path=args.yolo_dir / f"{name}.json")
                detections = {**(detections or {}), "facts": light_fact(det)}
            if args.mark and r["video_point_of_view"] == "1":
                from workers.vision.detection import detect_and_track, pick_is_sure, pick_opponent

                det = detect_and_track(str(args.videos / f"{name}.mp4"), cache_path=args.yolo_dir / f"{name}.json")
                track_a, why = pick_opponent(det)
                sure = bool(track_a) and pick_is_sure(why)
                detections = {**(detections or {}), "mark_info": {**why, "drawn": sure}}
                if sure:
                    detections["mark_tracks"] = {"A": track_a}
            card = extract_fact_card_sync(name, str(args.videos / f"{name}.mp4"), detections,
                                          viewpoint=int(r["video_point_of_view"]), vlm=vlm,
                                          prompt_version=args.prompt, type_select=args.type_select)
        except Exception as e:  # 한 건 실패로 전체를 멈추지 않는다
            meta["failed"].append({"video": name, "error": repr(e)})
            print(f"[{i}/{len(rows)}] {name} 실패: {e!r}")
            continue
        (out_dir / f"{name}.json").write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
        review = sum(f["verification_status"] == "needs_review" for f in card["fields"].values())
        mi = card["model_info"]
        meta["videos"].append({"video": name, "seconds": mi["seconds"], "needs_review": review,
                               "input_tokens": mi.get("prompt_tokens"), "output_tokens": mi.get("output_tokens")})
        mark = mi.get("mark")
        print(f"[{i}/{len(rows)}] {name} {mi['seconds']}s, 확인 표시 {review}칸"
              + (f", 상대 차 표시 {'함' if mark['drawn'] else '안 함(애매)'}" if mark else ""))
    meta["total_seconds"] = round(time.time() - t0, 1)
    tin = sum(v.get("input_tokens") or 0 for v in meta["videos"])
    tout = sum(v.get("output_tokens") or 0 for v in meta["videos"])
    price = PRICE_PER_M.get(args.model or ("gpt-4o" if args.vlm == "openai" else ""))
    if args.price_in is not None and args.price_out is not None:
        price = (args.price_in, args.price_out)
    meta["tokens"] = {"input": tin, "output": tout}
    if price:
        usd = tin / 1e6 * price[0] + tout / 1e6 * price[1]
        meta["cost_usd_estimate"] = round(usd, 4)
    (out_dir / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n완료 {len(meta['videos'])}건, 실패 {len(meta['failed'])}건, {meta['total_seconds']}s → {out_dir}")
    n_ok = max(1, len(meta["videos"]))
    print(f"토큰: 입력 {tin:,} / 출력 {tout:,} (영상 1건 평균 입력 {tin // n_ok:,} / 출력 {tout // n_ok:,})"
          + (f" → 약 {usd:.3f}달러(약 {usd * KRW_PER_USD:,.0f}원, 어림값)" if price
             else " — 이 모델 단가를 모름: --price-in, --price-out을 넣으면 비용도 계산"))


if __name__ == "__main__":
    main()
