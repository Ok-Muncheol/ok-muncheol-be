"""저장된 VLM 답으로 카드를 지금 코드로 다시 조립한다. API를 부르지 않아 무료.

사용:
    .venv/Scripts/python evals/vision/replay_cards.py evals/results/vision/runs/<run_id> --ego --yolo

질문(VLM)은 그대로 두고, 그 뒤 단계(카메라 계산, YOLO 박스 판단, 장소 정리 규칙, 확인 표시 규칙)를
바꿨을 때 효과를 보려고 쓴다. 결과: <run_id>__replay 폴더(원래 폴더는 안 바꿈). 이어서 score.py로 채점한다.
주의: 원래 실행과 같은 프레임·질문 버전이어야 한다(VLM 답은 그 사진을 보고 낸 것).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from workers.vision.fact_card import extract_fact_card_sync  # noqa: E402
from workers.vision.fields import FPS  # noqa: E402


class ReplayVLM:
    """저장된 카드의 VLM 값(vision_detail.vlm)을 원래 VLM 출력 모양으로 돌려준다."""

    def __init__(self, card: dict):
        self.card = card
        self.name = f"replay:{card['model_info'].get('vlm')}"

    def generate(self, prompt, images, schema):
        raw = {}
        for name, f in self.card["fields"].items():
            vlm = (f.get("vision_detail") or {}).get("vlm")
            if not vlm:
                continue
            if name == "collision_time_ms":
                ms = vlm.get("value")
                frame = round(ms * FPS / 1000) if isinstance(ms, (int, float)) else None
                raw["collision_frame"] = {"value": frame, "confidence": vlm.get("confidence")}
            elif name == "narrative":
                raw[name] = vlm.get("value")
            else:
                raw[name] = {"value": vlm.get("value"), "confidence": vlm.get("confidence")}
        raw["scene_notes"] = self.card["model_info"].get("scene_notes")
        return raw, {"prompt_tokens": 0, "output_tokens": 0, "seconds": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--ego", action="store_true")
    parser.add_argument("--yolo", action="store_true")
    parser.add_argument("--motion", action="store_true", help="움직임 분류기(--ego 필요)")
    parser.add_argument("--video", action="store_true", help="영상 모델로 장소를 정한다(2026-11-10)")
    parser.add_argument("--videos", default=ROOT / "data/private/aihub/videos", type=Path)
    parser.add_argument("--yolo-dir", default=ROOT / "evals/results/vision/yolo", type=Path)
    args = parser.parse_args()
    out = args.run_dir.parent / (f"{args.run_dir.name}__replay{'_motion' if args.motion else ''}"
                                 f"{'_video' if args.video else ''}")
    out.mkdir(exist_ok=True)

    for path in sorted(args.run_dir.glob("*.json")):
        if path.name == "run.json":
            continue
        old = json.loads(path.read_text(encoding="utf-8"))
        name, mi = old["case_id"], old["model_info"]
        video = str(args.videos / f"{name}.mp4")
        viewpoint = 1 if "_1_" in name else 3
        detections = None
        if args.ego and viewpoint == 1:
            from workers.vision.ego_motion import analyze_video

            ego = analyze_video(video)
            detections = {"hint": ego.summary(), "ego": ego}
        if args.yolo and viewpoint == 1:
            from workers.vision.detection import box_detections

            detections = {**(detections or {}), **box_detections(video, args.yolo_dir / f"{name}.json")}
        if args.motion and args.ego and viewpoint == 1:
            from workers.vision.detection import detect_and_track, pick_opponent
            from workers.vision.motion_model import motion_opinions

            det = detect_and_track(video, cache_path=args.yolo_dir / f"{name}.json")
            from workers.vision.scene_embed import embed_video, predict_scene

            track_m, why_m = pick_opponent(det, stitch=True)
            emb = embed_video(video, name)
            scene = {k: p for k in ("location_type", "road_priority") if (p := predict_scene(k, emb))}
            detections = {**(detections or {}), "motion": motion_opinions(det, ego, track_m, why_m, emb),
                          "scene": scene}
        if args.video and viewpoint == 1:
            from workers.vision.type_model import type_opinions
            from workers.vision.video_model import predict

            detections = {**(detections or {}), "video": predict(video, name),
                          "video_v3": predict(video, name, spec="v3"), "video_j3": predict(video, name, spec="j3"),
                          "type": type_opinions(video, name)}  # 상대 위치·도로 관계 확인 표시용
        card = extract_fact_card_sync(name, video, detections, viewpoint=viewpoint, vlm=ReplayVLM(old),
                                      prompt_version=mi.get("prompt_version", "v0.2"))
        (out / path.name).write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
        review = sum(f["verification_status"] == "needs_review" for f in card["fields"].values())
        print(f"{name} 확인 표시 {review}칸, 단계 {card['model_info']['stage']}")
    print(f"→ {out}")
    subprocess.run([sys.executable, str(ROOT / "evals/vision/score.py"), str(out)], check=True)


if __name__ == "__main__":
    main()
