"""키프레임을 VLM(OpenAI 또는 Claude)에 보내 사실 카드 칸을 뽑고 예측 JSONL을 쓴다.

    python -m evals.vision.run_vlm --gt data/private/aihub/vision_gt.jsonl \
        --provider openai --model gpt-5.5 --strategy adaptive --frames 10 --limit 50 \
        --out evals/results/vision_pred_gpt55_adaptive.jsonl
OPENAI_API_KEY / ANTHROPIC_API_KEY는 환경 변수 또는 .env에서 읽는다.
같은 --out에 다시 실행하면 끝난 사건은 건너뛴다. 두 전략·모델 비교는 같은 사건·같은 장수로 돌린다.
"""

import argparse
import base64
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.schemas.chart_scope import FIELD_OPTIONS
from evals.vision import trajectory
from evals.vision.keyframes import Strategy, extract_keyframes, signal_crops

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS = {"openai": "gpt-5.4-mini", "anthropic": "claude-opus-5-5"}

PREDICTED_FIELDS: tuple[str, ...] = (
    "location_type", "road_priority", "movement_A", "movement_B",
    "signal_A", "signal_B", "entry_order", "night",
)

SYSTEM_PROMPT = """당신은 자동차보험 보상 담당자를 돕는 블랙박스 사고 영상 분석기다.
주어진 키프레임(시간순, 타임스탬프 표기)만 보고 사고 사실 카드의 칸을 채운다.
확인할 수 없는 칸은 null로 두고 지어내지 않는다. 속도·과속은 판정하지 않는다.

당사자 표기 규칙:
- 촬영 차량(블랙박스가 달린 차, 화면에 보닛이 보이는 차)을 B, 상대 차량을 A로 한다.
- CCTV 등 3인칭 영상이면 먼저 충돌에 관여한 두 차량을 정하고, 먼저 화면에 나타난 차를 A로 한다.

칸 정의와 허용값:
- location_type: 사고 지점 종류. signalized_intersection(신호등 있는 교차로),
  unsignalized_intersection(신호등 없는 교차로, T자형 포함), roundabout(회전교차로),
  other(직선도로·주차장 등 교차로가 아님)
- road_priority: 신호 없는 교차로에서 두 도로의 관계. same_width(동일폭), major_minor(대로와 소로),
  stop_sign(한쪽에만 일시정지 표지). 신호 교차로이거나 판단 불가면 null
- movement_A / movement_B: 충돌 직전 각 차의 진행(의도 포함).
  straight, left_turn, right_turn, u_turn.
  판정 기준: 회전 차로에 있거나 방향지시등을 켜고 회전을 시작했으면 아직 차체가 많이 돌지
  않았어도 회전이다. 같은 방향으로 가던 상대 차가 촬영 차량 앞을 왼쪽으로 가로지르면 left_turn,
  마주 오던 상대 차가 촬영 차량 진행 경로를 가로질러 들어오면 상대는 left_turn,
  옆 도로에서 나와 교차로를 곧장 가로지르면 straight다
- signal_A / signal_B: 각 차가 교차로에 진입할 때 그 차의 진행 방향 신호. green, yellow, red,
  green_left_arrow(녹색 좌회전 화살표), no_signal(신호등 없음).
  신호등은 있으나 그 차의 신호를 볼 수 없으면 null
- entry_order: A 기준 교차로 진입 순서. first(A가 먼저), second(B가 먼저), simultaneous.
  신호 있는 교차로이거나 판단 불가면 null
- night: 야간이면 "true", 주간이면 "false"

각 칸에 model_confidence(0~1)와 근거가 된 프레임 번호, 한 줄 근거를 붙인다."""


def _schema(provider: str) -> dict:
    def nullable_enum(options: tuple[str, ...]) -> dict:
        if provider == "openai":  # strict 모드는 anyOf로 null을 허용한다
            return {"anyOf": [{"type": "string", "enum": list(options)}, {"type": "null"}]}
        return {"type": ["string", "null"], "enum": [*options, None]}

    def field(options: tuple[str, ...]) -> dict:
        return {
            "type": "object",
            "properties": {
                "value": nullable_enum(options),
                "model_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence_frames": {"type": "array", "items": {"type": "integer"}},
                "reason": {"type": "string"},
            },
            "required": ["value", "model_confidence", "evidence_frames", "reason"],
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "properties": {
            **{key: field(FIELD_OPTIONS[key]) for key in PREDICTED_FIELDS},
            "narrative": {"type": "string"},
        },
        "required": [*PREDICTED_FIELDS, "narrative"],
        "additionalProperties": False,
    }


def load_env() -> None:
    """.env의 KEY=VALUE를 환경 변수에 채운다(이미 있으면 유지). python-dotenv 없이 간단히."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


SIGNAL_CROP_RULE = "signal 칸은 이 이미지의 신호등 색을 우선 근거로 삼아라."
# 램프 위치(왼쪽부터 적·황·좌회전·녹)로 세게 한 변형은 2026-11-09 실험에서 더 나빴다(README 참고)


def _image_part(jpeg: bytes, provider: str) -> dict:
    b64 = base64.standard_b64encode(jpeg).decode()
    if provider == "openai":
        return {"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{b64}", "detail": "high"}}
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg", "data": b64}}


def build_content(keyframes, point_of_view: int, provider: str,
                  known: dict[str, object] | None = None,
                  trajectory_text: str | None = None, crops=()) -> list[dict]:
    content: list[dict] = []
    for i, kf in enumerate(keyframes, start=1):
        content.append({"type": "text", "text": f"프레임 {i} (t={kf.timestamp_ms / 1000:.2f}s)"})
        content.append(_image_part(kf.jpeg, provider))
    for i, kf in enumerate(crops, start=1):
        # 신호등 판독용. 프레임 번호 체계와 섞이지 않게 따로 이름 붙인다
        label = f"신호등 확인용 확대 이미지 {i} (화면 상단, t={kf.timestamp_ms / 1000:.2f}s). "
        content.append({"type": "text", "text": label + SIGNAL_CROP_RULE})
        content.append(_image_part(kf.jpeg, provider))
    view = "1인칭 블랙박스 영상(촬영 차량=B)" if point_of_view == 1 else "3인칭(CCTV) 영상"
    text = f"이 영상은 {view}이다."
    if known:
        # 지도·GPS 등 영상 밖 정보로 확정된 칸. 모델은 그대로 받아 나머지 칸 판단에만 쓴다
        given = ", ".join(f"{k}={v}" for k, v in known.items())
        text += f" 다음 칸은 지도 정보로 확정되었으니 그대로 쓰고 나머지 판단에 참고하라: {given}."
    if trajectory_text:
        text += ("\n\n" + trajectory_text
                 + "\n촬영 차량 회전각이 25도 이상이면 그 방향 회전이 확실하다. 25도 미만이면 "
                 "직진이 아니라 '아직 돌지 않음'이므로 회전 차로·방향지시등·충돌 위치로 "
                 "의도를 읽어라. "
                 "상대 차량은 궤적의 x 변화와 크기 변화로 원래 진행 방향"
                 "(같은 방향/마주 옴/옆 도로)을 "
                 "먼저 정한 뒤 위 판정 기준을 적용하라.")
    content.append({"type": "text", "text": text + " 위 프레임으로 사실 카드 칸을 채워라."})
    return content


def call_openai(client, model: str, content: list[dict]) -> tuple[dict, dict, str]:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": content}],
        response_format={"type": "json_schema", "json_schema": {
            "name": "fact_card", "strict": True, "schema": _schema("openai")}},
    )
    choice = response.choices[0]
    if choice.finish_reason == "content_filter" or choice.message.refusal:
        raise RuntimeError(f"refusal: {choice.message.refusal or choice.finish_reason}")
    return json.loads(choice.message.content), response.usage.model_dump(), response.model


def call_anthropic(client, model: str, content: list[dict]) -> tuple[dict, dict, str]:
    response = client.beta.messages.create(
        model=model,
        max_tokens=4096,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": _schema("anthropic")}},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"refusal: {getattr(response, 'stop_details', None)}")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text), response.usage.to_dict(), response.model


def predict_one(client, provider: str, model: str, example: dict, strategy: Strategy,
                frames: int, evidence_dir: Path | None, known_fields: tuple[str, ...] = (),
                max_width: int = 768, trajectory_cache: Path | None = None,
                signal_crop_count: int = 0,
                focus_window: tuple[float, float] | None = None) -> dict:
    started = time.time()
    trajectory_text = None
    trajectory_meta: dict = {}
    if trajectory_cache is not None:
        # 광류·YOLO 궤적 특징. 건당 수 초 걸리므로 JSON 캐시(예전 버전은 광류만 다시 잰다)
        features = trajectory.cached_features(Path(example["video_path"]), trajectory_cache,
                                              example["example_id"])
        trajectory_text = features["text"]
        trajectory_meta = {"ego_turn": features["ego_turn"],
                           "ego_total_yaw": features["ego_total_yaw"],
                           "collision_sec": features.get("collision_sec")}
    window = None
    if focus_window is not None:
        # 충돌 시각(YOLO 궤적 기준) 앞뒤 구간만 본다. 충돌 시각이 없으면 전체 구간
        collision = (example.get("video_quality") or {}).get("collision_sec")
        if collision is not None:
            window = (collision - focus_window[0], collision + focus_window[1])
    keyframes = extract_keyframes(Path(example["video_path"]), k=frames, strategy=strategy,
                                  max_width=max_width, window_sec=window)
    known = {k: example["fields"][k] for k in known_fields if example["fields"].get(k) is not None}
    if evidence_dir:
        folder = evidence_dir / example["example_id"]
        folder.mkdir(parents=True, exist_ok=True)
        for i, kf in enumerate(keyframes, start=1):
            (folder / f"{i:02d}_{kf.frame_index}.jpg").write_bytes(kf.jpeg)
    crops = []
    if signal_crop_count:
        collision = (example.get("video_quality") or {}).get("collision_sec")
        crops = signal_crops(Path(example["video_path"]), collision, n=signal_crop_count)
    content = build_content(keyframes, example["point_of_view"], provider, known,
                            trajectory_text, crops)
    call = call_openai if provider == "openai" else call_anthropic
    parsed, usage, served_model = call(client, model, content)
    fields = {}
    for key in PREDICTED_FIELDS:
        item = parsed[key]
        fields[key] = {
            "value": item["value"],
            "model_confidence": item["model_confidence"],
            "evidence_frame_ids": [
                f"{keyframes[i - 1].frame_index}" for i in item["evidence_frames"]
                if 1 <= i <= len(keyframes)
            ],
            "reason": item["reason"],
        }
    for key, value in known.items():
        fields[key] = {"value": value, "model_confidence": None, "evidence_frame_ids": [],
                       "reason": "given"}
    fields["accident_type"] = {"value": "car_to_car", "model_confidence": None}
    return {
        "example_id": example["example_id"],
        "fields": fields,
        "narrative": parsed["narrative"],
        "meta": {
            "provider": provider, "model": served_model, "strategy": strategy,
            "frames": len(keyframes), "keyframe_indices": [kf.frame_index for kf in keyframes],
            "max_width": max_width, "known_fields": sorted(known), "trajectory": trajectory_meta,
            "signal_crops": [kf.frame_index for kf in crops],
            "focus_window": list(window) if window else None,
            "usage": usage, "seconds": round(time.time() - started, 2),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--provider", choices=("openai", "anthropic"), default="openai")
    parser.add_argument("--model", default=None,
                        help="기본: openai=gpt-5.5, anthropic=claude-opus-5-5")
    parser.add_argument("--strategy", choices=("uniform", "adaptive"), default="adaptive")
    parser.add_argument("--frames", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--all-scope", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, default=None,
                        help="선택한 키프레임 JPEG을 사건별로 저장(무시 경로에 둘 것)")
    parser.add_argument("--known-fields", default="",
                        help="정답을 입력으로 주는 칸(쉼표 구분). 예: location_type")
    parser.add_argument("--max-width", type=int, default=768, help="키프레임 가로 픽셀")
    parser.add_argument("--focus-window", default=None,
                        help="'전,후' 초. 충돌 시각 기준 그 구간에서만 키프레임을 뽑는다. "
                             "예: 3,0.5")
    parser.add_argument("--signal-crops", type=int, default=0,
                        help="충돌 전 N초부터 1초 간격으로 화면 상단을 고해상도로 잘라 "
                             "신호등 판독에 붙인다")
    parser.add_argument("--trajectory-cache", type=Path, default=None,
                        help="주면 광류·YOLO 궤적 특징을 프롬프트에 붙인다(캐시 디렉터리). "
                             "예전 버전 캐시는 YOLO 결과를 두고 광류만 다시 재서 올린다")
    args = parser.parse_args()
    known_fields = tuple(k for k in args.known_fields.split(",") if k)
    focus_window = None
    if args.focus_window:
        before, after = (float(x) for x in args.focus_window.split(","))
        focus_window = (before, after)

    load_env()
    model = args.model or DEFAULT_MODELS[args.provider]
    if args.provider == "openai":
        from openai import OpenAI

        client = OpenAI()
    else:
        import anthropic

        client = anthropic.Anthropic()
    examples = [json.loads(line) for line in args.gt.read_text().splitlines() if line.strip()]
    examples = [e for e in examples if e["video_path"] and (args.all_scope or e["in_mvp_scope"])]
    done: set[str] = set()
    if args.out.exists():
        # 실패(error) 행은 버리고 성공 행만 남긴 뒤 이어서 돌린다
        kept = [json.loads(line) for line in args.out.read_text().splitlines() if line.strip()]
        kept = [row for row in kept if "error" not in row]
        done = {row["example_id"] for row in kept}
        args.out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept))
    todo = [e for e in examples if e["example_id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} to run ({len(done)} done) {args.provider}/{model} "
          f"strategy={args.strategy} frames={args.frames} width={args.max_width} "
          f"known={list(known_fields)} trajectory={args.trajectory_cache is not None} "
          f"signal_crops={args.signal_crops} focus_window={focus_window}",
          file=sys.stderr)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a") as out, ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(predict_one, client, args.provider, model, e, args.strategy,
                               args.frames, args.evidence_dir, known_fields,
                               args.max_width, args.trajectory_cache,
                               args.signal_crops, focus_window): e for e in todo}
        for n, future in enumerate(as_completed(futures), start=1):
            example = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001 - 한 건 실패가 전체 실행을 멈추지 않게 기록한다
                row = {"example_id": example["example_id"], "error": repr(exc)}
                if "no credits" in row["error"] or "insufficient_quota" in row["error"]:
                    # 잔액 소진은 모든 건이 같이 실패하므로 즉시 멈춘다
                    # (실패 행은 재실행 때 버려진다)
                    for pending in futures:
                        pending.cancel()
                    raise SystemExit(f"API 잔액 없음: {row['error'][:160]}") from exc
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            print(f"[{n}/{len(todo)}] {example['example_id']} "
                  f"{'ERROR ' + row['error'] if 'error' in row else 'ok'}", file=sys.stderr)


if __name__ == "__main__":
    main()
