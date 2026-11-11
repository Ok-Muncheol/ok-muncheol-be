"""A: VLM 출력과 탐지 결과를 교차 확인해 미확정 사실 카드를 만든다.

카드 모양은 docs/contracts/fact-card-v0.md(합의 전 초안)를 따른다.
- VLM은 바꿔 끼울 수 있다(`VLM` 규약). 지금은 로컬 Ollama만 구현했다.
- detections가 없으면 VLM 단독(실험 1단계)으로 동작한다.
- OpenCV·requests는 비전 환경(.venv)에만 있으므로 함수 안에서 불러온다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.schemas.facts import FactCard
from workers.vision.env import load_env
from workers.vision.fields import (
    FIELDS,
    FPS,
    REVIEW_THRESHOLD,
    SIGNAL_PLACES,
    derive_fields,
    frame_to_ms,
    is_required,
    same_value,
    tidy_by_location,
)
from workers.vision.tracks import draw_boxes, summarize_tracks

PROMPT_VERSIONS = ("v0.1", "v0.2", "v0.3", "v0.4")
PROMPT_VERSION = "v0.2"  # 기본값: 지금까지 가장 좋은 버전(개선용 10건 65%)
# v0.1: 충돌(hint=75) 기준 간격. 충돌 뒤 장면은 움직임 판단을 망치므로 앞쪽 위주
FRAME_OFFSETS = [-45, -35, -27, -20, -14, -9, -5, -2, 0, 6]
#  v0.2: 앞쪽 2장(도로 모양 파악용) + 충돌 구간(약 3~6.5초)을 촘촘하게. 충돌 시점을 미리 알려 주지
# 않는다
FRAMES_V02 = [1, 30, 50, 58, 63, 67, 70, 73, 76, 80, 85, 95]


# ---------- 프레임 ----------


def select_frames(
    collision_hint: int = 75, total: int = 150, version: str = PROMPT_VERSION
) -> list[int]:
    if version == "v0.1":
        return sorted({min(total, max(1, collision_hint + d)) for d in FRAME_OFFSETS})
    return [f for f in FRAMES_V02 if f <= total]


def render_frames(
    video_path: str,
    frames: list[int],
    hint: int,
    size=(768, 432),
    tracks: dict | None = None,
    version: str = PROMPT_VERSION,
) -> list[str]:
    """프레임을 JPG(base64)로. 왼쪽 위에 프레임 번호와 시간을 적어 시간 순서를 분명히 한다.

    v0.1은 충돌 추정 시점 기준(+/-초), v0.2는 영상 시작 기준 초(충돌 시점 힌트를 주지 않기 위해).
    tracks가 있으면 그 프레임(±2) 근처의 A·B 박스를 그린다.
    """
    import cv2

    cap = cv2.VideoCapture(video_path)
    out = []
    for n in frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n - 1)
        ok, img = cap.read()
        if not ok:
            raise RuntimeError(f"프레임 {n} 읽기 실패: {video_path}")
        if tracks:
            draw_boxes(img, tracks, n)
        img = cv2.resize(img, size)
        label = (
            f"#{n} ({(n - hint) / FPS:+.1f}s)" if version == "v0.1" else f"#{n} ({n / FPS:.1f}s)"
        )
        cv2.rectangle(img, (0, 0), (190, 30), (0, 0, 0), -1)
        cv2.putText(img, label, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
        out.append(
            base64.b64encode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1]).decode()
        )
    return out


# ---------- 질문 ----------

ASKED = [f for f in FIELDS if f[0] != "collision_time_ms"]


def response_schema(version: str = PROMPT_VERSION) -> dict:
    props: dict[str, Any] = {}
    if version != "v0.1":
        # 답하기 전에 관찰을 먼저 적게 한다(순서가 앞이어야 효과가 있음). 채점하지 않는다
        props["scene_notes"] = {"type": "string"}
    for name, values, _, _ in ASKED:
        if isinstance(values, list):
            props[name] = {
                "type": "object",
                "required": ["value", "confidence"],
                "properties": {"value": {"enum": values}, "confidence": {"type": "number"}},
            }
        elif values == "int":
            props[name] = {
                "type": "object",
                "required": ["value", "confidence"],
                "properties": {
                    "value": {"type": ["integer", "null"]},
                    "confidence": {"type": "number"},
                },
            }
        else:
            props[name] = {"type": "string"}
    props["collision_frame"] = {
        "type": "object",
        "required": ["value", "confidence"],
        "properties": {"value": {"type": ["integer", "null"]}, "confidence": {"type": "number"}},
    }
    return {"type": "object", "properties": props, "required": list(props)}


def build_prompt(
    viewpoint: int,
    frames: list[int],
    hint: int,
    context: str | None = None,
    version: str = PROMPT_VERSION,
) -> str:
    if version == "v0.1":
        return _build_prompt_v01(viewpoint, frames, hint, context)
    if version == "v0.4":
        #  v0.3 결과: A 움직임 규칙이 과하게 먹혀 차로 변경·중앙선을 7/10 '있음'으로 답함(정답
        # 2·1건) → 뺌.
        # 신호 규칙(신호등 위치를 적어야 색을 고름, 가로등·브레이크등 제외)만 남긴다.
        return _build_prompt_v02(viewpoint, frames, context, notes={"2"}, rules=V03_RULES[:2])
    if version == "v0.3":
        return _build_prompt_v02(viewpoint, frames, context, notes={"2", "4"}, rules=V03_RULES)
    return _build_prompt_v02(viewpoint, frames, context)


# v0.3에서 바뀌는 문장(v0.2 오답 분석, gpt-4o 개선용 10건)
#  - 신호 B를 '적색'으로 지어냄(7개 중 6개), 신호 없는 야간 사거리에서도 '신호등 보임, 적색' →
# 가로등·브레이크등 착각
# - 그 때문에 장소를 '사거리_신호있음'으로 틀림
# - 충돌 부위 A의 좌우가 반대(앞모습이 보이면 거울처럼 반대)
# - A 움직임을 '직진'으로만 답함(진로변경·우회전·후진·추월)
V03_NOTES = {
    "2": "2) 신호등: 신호등이 보이면 몇 번 사진의 어디(예: #30 화면 위 가운데)에 있는지와 색을 적는다. "
    "보이지 않으면 '신호등 안 보임'이라고 적는다",
    "4": "4) A: 처음 보인 사진·위치, A의 앞모습이 보이는지 뒷모습이 보이는지, 차선을 넘었는지, "
    "방향을 틀었는지, 충돌 직전 위치",
}
V03_RULES = [
    "- 신호등 판단: 위에 매달리거나 기둥에 달린 신호등 몸체(3~4개 등이 나란한 상자)가 보여야 신호등이다. "
    "가로등, 앞차 브레이크등·후미등, 간판 불빛은 신호등이 아니다. scene_notes 2)에 신호등 위치를 적지 못했으면 "
    "신호 색을 고르지 말고 '불명'(신호등이 있을 법하지만 안 보임) 또는 '신호없음'(신호등이 없는 곳)을 고른다.",
    "- location_type의 '사거리_신호있음'은 신호등 몸체가 실제로 보일 때만 고른다. 교차로인데 신호등이 안 보이면 "
    "'사거리_신호없음' 또는 'T자형교차로'.",
    "- 충돌 부위 좌우: 각 차 운전석에 앉은 사람 기준이다. 차의 앞모습이 화면에 보이면 그 차의 왼쪽은 화면 오른쪽, "
    "오른쪽은 화면 왼쪽이다(거울처럼 반대). 뒷모습이 보이면 화면과 같은 쪽이다.",
    "- A의 움직임: '직진'은 A가 자기 차선을 따라 곧게 왔을 때만 고른다. 차선을 넘어 들어오면 진로변경(앞질러 가면 추월), "
    "옆 도로에서 방향을 틀며 들어오면 좌회전·우회전, 뒷모습이 B 쪽으로 다가오면 후진.",
]


def _build_prompt_v02(
    viewpoint: int,
    frames: list[int],
    context: str | None,
    notes: set[str] = frozenset(),
    rules: list[str] = (),
) -> str:
    """notes: V03_NOTES에서 바꿔 끼울 메모 항목 번호, rules: 덧붙일 판단 규칙."""
    """v0.1 오답 분석(gpt-4o, 개선용 10건)으로 고친 질문.

    - 장소를 거의 다 '직선도로'로 답함 → 앞쪽 사진으로 도로 모양을 먼저 보게 함
    - 신호를 '불명'으로 답함 → '신호등이 아예 없음'(신호없음)과 '색이 안 보임'(불명)을 구분
    - 충돌 순간을 힌트(75번) 그대로 답함 → 힌트를 빼고 사진에서 직접 찾게 함
    - 충돌 부위를 '앞'으로만 답함, 1인칭 B 움직임을 '직진'으로만 답함 → 판단 기준 추가
    """
    if viewpoint == 1:
        who = (
            "1인칭 블랙박스 영상이다. 블랙박스가 달린 촬영 차량이 B, 부딪힌 상대 차량이 A다. "
            "B는 화면에 거의 보이지 않으므로 B의 움직임은 사진 사이 풍경 변화로 판단한다."
        )
    else:
        who = (
            "사고와 관계없는 제3의 카메라 영상이다. 서로 부딪힌 두 차 중 하나를 A, 다른 하나를 B로 정하고 "
            "scene_notes 첫 줄에 A와 B가 각각 어떤 차인지(색·위치) 적어라."
        )
    lines = [
        f"교통사고 영상(15fps, 150프레임 = 10초)에서 시간 순서대로 뽑은 사진 {len(frames)}장이다.",
        "각 사진 왼쪽 위에 프레임 번호와 영상 시작부터의 시간이 적혀 있다.",
        "앞쪽 사진은 사고 전 도로 모양을 보여 주고, 나머지는 충돌 전후를 촘촘하게 보여 준다. "
        "충돌 시점은 영상마다 다르니 사진을 보고 직접 찾아라.",
        who,
        "",
        "먼저 scene_notes에 아래 순서로 짧게 관찰을 적고, 그 관찰과 맞게 각 칸을 채워라.",
        "1) 도로 모양: 앞쪽 사진에서 가로지르는 도로·횡단보도·정지선이 있는지, 회전교차로·주차장·고속도로인지",
        V03_NOTES["2"]
        if "2" in notes
        else "2) 신호등: 보이는지, 보이면 어느 방향 신호이고 무슨 색인지",
        "3) B의 움직임",
        V03_NOTES["4"] if "4" in notes else "4) A가 처음 보인 위치와 충돌 직전까지의 경로",
        "5) 두 차가 처음 닿는 사진 번호와 닿는 부위",
        "",
        "칸별 판단 기준:",
        "- location_type: 충돌 지점만 보지 말고 앞쪽 사진의 도로 모양으로 정한다. 다른 도로가 가로지르거나 "
        "합쳐지면 교차로 종류를 고르고, '직선도로'는 교차로가 전혀 없을 때만 고른다.",
        "- road_priority, entry_order: 교차로(사거리·T자형·회전교차로)이면 반드시 판단한다. "
        "교차로가 아닐 때만 '해당없음'.",
        "- signal_A, signal_B: 사고 장소에 신호등이 아예 없으면 '신호없음'. 신호등은 있는데 그 차 방향의 색을 "
        "알 수 없으면 '불명'. 1인칭에서 B의 신호는 화면 앞쪽에 보이는 신호등이다.",
        "- 1인칭 B의 움직임: 풍경이 사진마다 오른쪽으로 흘러가면 좌회전, 왼쪽으로 흘러가면 우회전, "
        "차선을 비스듬히 넘어가면 진로변경, 풍경이 전혀 안 바뀌면 주정차, 풍경이 멀어지면 후진, "
        "멈춰 있다가 움직이기 시작하면 정차후출발, 곧게 앞으로 가면 직진.",
        "- A의 움직임: A의 앞모습/뒷모습과 이동 방향으로 판단한다. A의 뒷모습이 B 쪽으로 가까워지면 후진일 수 있다.",
        "- relative_position(B 기준 A가 충돌 직전 있던 쪽): 1인칭이면 A가 화면 왼쪽에 있거나 왼쪽에서 다가오면 "
        "'왼쪽', 오른쪽이면 '오른쪽', A의 앞모습이 정면에서 다가오면 '맞은편', A의 뒷모습이 앞에 있으면 "
        "'같은방향_앞', 뒤에서 오면 '같은방향_뒤'.",
        "- collision_part_A/B: 각 차 자기 기준 부위다. 정면 가운데만 '앞', 모서리 쪽이면 '앞왼쪽'·'앞오른쪽', "
        "옆면이면 '왼쪽'·'오른쪽'. 1인칭에서 상대 차가 화면 오른쪽 아래에서 닿으면 B의 '앞오른쪽'.",
        "- collision_frame: 두 차가 처음 닿거나 화면이 갑자기 흔들리는 첫 사진의 프레임 번호.",
        "- 그 상황에 없는 칸은 '해당없음', 화면으로 알 수 없으면 '불명'.",
        "- confidence: 사진에서 직접 보이면 0.8 이상, 짐작이면 0.5 이하로 적는다.",
        *rules,
        "",
        "칸 설명:",
    ]
    for name, values, _, desc in ASKED:
        lines.append(f"- {name}: {desc}")
    lines.append("- collision_frame: 두 차가 처음 닿는 프레임 번호(1~150). 모르면 null")
    if context:
        lines += ["", "추가 정보:", context]
    lines += ["", "JSON만 출력한다."]
    return "\n".join(lines)


def _build_prompt_v01(
    viewpoint: int, frames: list[int], hint: int, context: str | None = None
) -> str:
    if viewpoint == 1:
        who = (
            "블랙박스가 달린 촬영 차량이 B, 부딪힌 상대 차량이 A다. B는 화면에 거의 보이지 않는다."
        )
    else:
        who = (
            "사고와 관계없는 제3의 카메라 영상이다. 서로 부딪힌 두 차 중 하나를 A, 다른 하나를 B로 정하고 "
            "narrative 첫 문장에 A와 B가 각각 어떤 차인지(색·위치) 적어라."
        )
    lines = [
        f"교통사고 영상에서 시간 순서대로 뽑은 프레임 {len(frames)}장이다. 영상은 15fps, 150프레임(10초)이다.",
        f"각 사진 왼쪽 위에 프레임 번호와 충돌 추정 시점({hint}번) 기준 시간이 적혀 있다. 실제 충돌은 조금 앞뒤일 수 있다.",
        who,
        "충돌 뒤에는 차가 돌거나 화면이 흔들린다. 움직임·차로·진입 순서는 충돌 '직전'까지의 장면으로 판단하라.",
        "각 칸에 정해진 값 중 하나와 확신도(0~1)를 적어라. 화면으로 알 수 없으면 '불명'을 고르고 확신도를 낮게 적어라.",
        "그 상황에 없는 칸(예: 직선 도로의 진입 순서)은 '해당없음'.",
        "상대 차 신호는 화면에 신호등이 보이지 않으면 추측하지 말고 '불명'.",
        "",
        "칸 설명:",
    ]
    for name, values, _, desc in ASKED:
        lines.append(f"- {name}: {desc}")
    lines.append("- collision_frame: 두 차가 처음 닿는 프레임 번호(1~150). 모르면 null")
    if context:
        lines += ["", "추가 정보:", context]
    lines += ["", "JSON만 출력한다."]
    return "\n".join(lines)


# ---------- VLM ----------


class VLM(Protocol):
    name: str

    def generate(self, prompt: str, images: list[str], schema: dict) -> tuple[dict, dict]:
        """(파싱한 JSON, 메타정보)를 돌려준다."""


@dataclass
class OllamaVLM:
    model: str = "qwen3.5:9b"
    url: str = "http://localhost:11434"
    think: bool = False  # 켜면 형식 출력이 비는 문제가 있어 기본은 끔
    # num_ctx 기본값(4096)은 사진 10장+질문을 다 못 담아 출력이 잘린다
    options: dict = field(
        default_factory=lambda: {"temperature": 0, "num_ctx": 16384, "num_predict": 2048}
    )

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    def generate(self, prompt: str, images: list[str], schema: dict) -> tuple[dict, dict]:
        import requests

        t0 = time.time()
        resp = requests.post(
            f"{self.url}/api/chat",
            timeout=900,
            json={
                "model": self.model,
                "stream": False,
                "think": self.think,
                "format": schema,
                "options": self.options,
                "messages": [{"role": "user", "content": prompt, "images": images}],
            },
        )
        resp.raise_for_status()
        body = resp.json()
        text = body.get("message", {}).get("content", "")
        meta = {
            "seconds": round(time.time() - t0, 1),
            "prompt_tokens": body.get("prompt_eval_count"),
            "output_tokens": body.get("eval_count"),
            "raw": text,
        }
        return json.loads(text), meta


def strict_schema(node):
    """Claude 구조화 출력용: 모든 object에 additionalProperties=false, 문자열 enum에 type, null
    허용은 anyOf로."""
    if isinstance(node, list):
        return [strict_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    node = {k: strict_schema(v) for k, v in node.items()}
    if node.get("type") == "object":
        node["additionalProperties"] = False
        node.setdefault("required", list(node.get("properties", {})))
    if "enum" in node and "type" not in node:
        node["type"] = "string"
    if isinstance(node.get("type"), list):
        node = {
            **{k: v for k, v in node.items() if k != "type"},
            "anyOf": [{"type": t} for t in node["type"]],
        }
    return node


@dataclass
class OpenAIVLM:
    """OpenAI API (Chat Completions + 구조화 출력). 키는 환경 변수 OPENAI_API_KEY에서
    읽는다(코드·깃에 넣지 않음).

    model: 계정에서 쓸 수 있는 이미지 입력 가능 모델 이름(예: 팀이 정한 모델). 필수.
    reasoning_effort: 추론 모델일 때만 넘긴다(None이면 보내지 않음).
    """

    model: str
    reasoning_effort: str | None = None
    detail: str = "high"  # 사진 해상도 처리: low(싸고 거침) / high / auto
    max_completion_tokens: int = 16000
    temperature: float | None = 0  # 답 흔들림을 줄인다. 추론 모델처럼 받지 않는 모델은 None

    @property
    def name(self) -> str:
        return f"openai:{self.model}" + (
            f":{self.reasoning_effort}" if self.reasoning_effort else ""
        )

    def generate(self, prompt: str, images: list[str], schema: dict) -> tuple[dict, dict]:
        import openai

        load_env()
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY가 없습니다. workers/vision/.env에 넣고 저장했는지 확인하세요."
            )
        client = openai.OpenAI()
        content = [{"type": "text", "text": prompt}]
        content += [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img}", "detail": self.detail},
            }
            for img in images
        ]
        kwargs = {}
        # 추론형 모델(GPT-5·o 계열)은 temperature를 받지 않을 수 있어 보내지 않는다
        reasoning_model = self.model.startswith(("gpt-5", "o1", "o3", "o4"))
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        elif self.temperature is not None and not reasoning_model:
            kwargs["temperature"] = self.temperature
        t0 = time.time()
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "fact_card",
                    "strict": True,
                    "schema": strict_schema(schema),
                },
            },
            max_completion_tokens=self.max_completion_tokens,
            **kwargs,
        )
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise RuntimeError(f"모델이 거절함: {choice.message.refusal}")
        if choice.finish_reason == "length":
            raise RuntimeError("출력이 max_completion_tokens에서 잘림")
        text = choice.message.content
        u = resp.usage
        meta = {
            "seconds": round(time.time() - t0, 1),
            "prompt_tokens": getattr(u, "prompt_tokens", None),
            "output_tokens": getattr(u, "completion_tokens", None),
            "model_served": resp.model,
            "request_id": getattr(resp, "_request_id", None),
            "raw": text,
        }
        return json.loads(text), meta


@dataclass
class ClaudeVLM:
    """Anthropic Claude API. 키는 환경 변수 ANTHROPIC_API_KEY에서 읽는다(코드·깃에 넣지 않음).

    - 거절(refusal) 시 서버가 다른 모델로 다시 돌리는 fallbacks="default"를 켜 둔다.
    - Claude Opus 5.5는 생각하기가 항상 켜져 있고, 깊이는 effort로 조절한다(기본 medium → 여기선
      high).
    """

    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_tokens: int = 16000

    @property
    def name(self) -> str:
        return f"anthropic:{self.model}:{self.effort}"

    def generate(self, prompt: str, images: list[str], schema: dict) -> tuple[dict, dict]:
        import anthropic

        load_env()
        client = anthropic.Anthropic()
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        t0 = time.time()
        resp = client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": strict_schema(schema)},
            },
            messages=[{"role": "user", "content": content}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"모델이 거절함: {getattr(resp, 'stop_details', None)}")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("출력이 max_tokens에서 잘림")
        text = next(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        meta = {
            "seconds": round(time.time() - t0, 1),
            "prompt_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "model_served": resp.model,
            "request_id": resp._request_id,
            "raw": text,
        }
        return json.loads(text), meta


# ---------- 2차 질문: 사고 유형 고르기 ----------


def describe_choice(c: dict) -> str:
    from workers.vision.accident_types import describe

    return describe(c)


TYPE_SCHEMA = {
    "type": "object",
    "required": ["reason", "choice", "confidence"],
    "properties": {
        "reason": {"type": "string"},
        "choice": {"type": "integer"},
        "confidence": {"type": "number"},
    },
}


def score_type(
    raw: dict, viewpoint: int, ego_movement: str | None, prior_weight: float | None = None
) -> dict | None:
    """1차 답·카메라 계산·유형 빈도로 모든 유형에 점수를 매겨 1등을 고른다. 확신도는 1·2등 점수 차로
    정한다."""
    import math

    from workers.vision.accident_types import score_candidates

    ranked = score_candidates(raw, viewpoint, ego_movement, prior_weight)
    if not ranked:
        return None
    top = ranked[0]
    gap = top["score"] - ranked[1]["score"] if len(ranked) > 1 else 3.0
    conf = round(1 / (1 + math.exp(-gap)), 2)  # 점수 차 0 → 0.5, 2 → 0.88
    return {
        "candidate": top,
        "confidence": conf,
        "reason": f"맞은 칸 {top['matched']}, 빈도 {top['prior']}, 점수 {top['score']} (2등과 차이 {gap:.2f})",
        "n_candidates": len(ranked),
        "top3": [(c["type"], c["swap"], c["score"]) for c in ranked[:3]],
        "prompt_tokens": 0,
        "output_tokens": 0,
    }


def choose_type(
    vlm: VLM,
    images: list[str],
    viewpoint: int,
    location: str | None,
    ego_movement: str | None,
    scene_notes: str | None,
) -> dict | None:
    """장소(1차 답)의 사고 유형 후보 중 하나를 고르게 한다. 고르지 못하면 None.

    1인칭이면 카메라로 본 B 움직임과 맞는 후보만 보여 준다. 고른 유형에서 장소·움직임·진입 순서 등을
    채운다.
    """
    from workers.vision.accident_types import candidates, describe

    cands = candidates(location, ego_movement if viewpoint == 1 else None)
    if not cands:
        return None
    who = (
        "1인칭 블랙박스 영상이다. B는 블랙박스가 달린 촬영 차량, A는 부딪힌 상대 차량이다."
        if viewpoint == 1
        else "제3의 카메라 영상이다. 부딪힌 두 차를 A·B로 놓고, 설명과 맞는 방향의 후보를 고른다."
    )
    lines = [
        "앞의 사진들과 같은 교통사고다. " + who,
        "아래 후보는 과실비율 인정기준의 사고 유형이다. 사진과 가장 잘 맞는 하나의 번호를 choice에 적어라.",
        "같은 유형이 A·B를 바꿔 두 번 나올 수 있다. 블랙박스 차(B)가 실제로 한 움직임과 맞는 쪽을 고른다.",
        "도로 모양(교차로 종류, 대로·소로, 일시정지 표지), 두 차가 온 방향, 누가 먼저 들어왔는지, 신호를 비교하라.",
        "맞는 후보가 하나도 없으면 choice를 0으로 적는다. reason에 고른 이유를 짧게 적고, confidence는 0~1.",
    ]
    if scene_notes:
        lines += ["", "앞서 사진을 보고 적은 관찰:", scene_notes]
    lines += ["", "후보:"] + [describe(c) for c in cands] + ["", "JSON만 출력한다."]
    raw, meta = vlm.generate("\n".join(lines), images, TYPE_SCHEMA)
    pick = next((c for c in cands if c["id"] == raw.get("choice")), None)
    conf = raw.get("confidence")
    return {
        "candidate": pick,
        "confidence": float(conf) if isinstance(conf, (int, float)) else None,
        "reason": raw.get("reason"),
        "n_candidates": len(cands),
        "prompt_tokens": meta.get("prompt_tokens"),
        "output_tokens": meta.get("output_tokens"),
    }


# ---------- 카드 조립 ----------


def opinion(raw: dict, name: str) -> dict | None:
    item = raw.get(name)
    if not isinstance(item, dict):
        return None
    value = item.get("value")
    if name == "collision_time_ms":
        value = frame_to_ms(value) if isinstance(value, int) else None
    conf = item.get("confidence")
    return {"value": value, "confidence": float(conf) if isinstance(conf, (int, float)) else None}


#  둘이 다를 때 탐지(코드 계산) 값을 최종값으로 쓰는 칸. 1인칭 B 움직임은 카메라 계산이 VLM보다
# 낫다(2026-11-06 개선용)
#  충돌 부위 B: YOLO 상대 차 박스 판단 9/10 vs VLM 5/10(VLM이 왼쪽·오른쪽을 거꾸로 봄, 2026-11-09
# 개선용 10건)
#  움직임 A: 탐지 값은 움직임 분류기에서만 온다(VLM 실행마다 5·3·3/10 vs 분류기 4/10 고정, 라벨
# 1,000건 57%)
#  장소: 영상 모델(video_model) 값이 있으면 그 값. 사람 검수 30건 20 vs VLM 15, 사람 정답 92건
# 74%(2026-11-10)
PREFER_DETECTION = {
    "movement_A",
    "movement_B",
    "lane_change_B",
    "motion_trend_B",
    "collision_part_B",
    "location_type",
}
#  탐지 값이 있으면 VLM과 달라도 확인 표시를 붙이지 않는 칸(VLM이 일정하게 틀려서, 다를 때마다
# 표시하면 거의 매번 붙음).
# 탐지 값이 없어 VLM 값을 쓰면 표시한다.
TRUST_DETECTION = {"collision_part_B"}
# 값과 상관없이 항상 담당자 확인 표시를 붙이는 칸: 개선용 평가에서 믿기 어려운 칸.
#  2026-11-09 개선용 10건(v0.2 + ego, 장소 확인 후): 4칸(+충돌 부위 B) 표시 시 표시 없는 칸 정확도
# 77%→89%, 카드당 6칸.
# 충돌 부위 B를 YOLO로 바꾸고 뺌 → 89% 유지, 카드당 5칸.
# 충돌 부위 A는 도표를 고르지 않는 근거 칸이라 뺌 → 핵심 칸 중 표시 없는 칸 88% 그대로, 카드당 4칸.
# (D안: 사진에 상대 차 표시는 움직임 A를 못 고쳐서 버림, 5/10→3/10.) 칸이 믿을 만해지면 여기서 뺀다.
#  장소: VLM 값을 미리 채워 보여 주되 항상 확인. 도표를 고르는 첫 칸인데 자동 6/10이고, 틀리면 정리
# 규칙이 딸린 칸까지
#  표시 없이 틀리게 채운다(개선용 표시 없이 틀린 핵심 13칸 중 8칸, 2026-11-09). 고치면 딸린 칸이
# 다시 표시된다.
#  signal_A 추가(2026-11-11 사용자 결정): 신호 판단이 아직 약해(30건 0/4) 신호 교차로에서는 A·B 모두
# 늘 확인
ALWAYS_REVIEW = {
    "location_type",
    "movement_A",
    "signal_A",
    "signal_B",
    "road_priority",
    "entry_order",
}
# 항상 표시하되 이 장소에서만:
# - 신호 B: 신호 교차로에서만 도표에 쓰인다(그 밖에서는 정리 규칙이 '신호없음'으로 둠)
#  - 도로 관계·진입 순서: 신호 없는 교차로에서 도표를 가르는 첫 기준인데 영상으로 판단이
# 어렵다(개선용 0/3).
#  그 밖에서는 정리 규칙의 '해당없음'이 맞으므로 표시하지 않는다. 담당자는 VLM이 미리 채운 값을
# 확인·수정한다.
UNSIGNALED_JUNCTIONS = {"사거리_신호없음", "T자형교차로"}
# 규칙(fields.derive_fields)으로 정해지면 믿고, 못 정하면 VLM 값에 확인 표시를 붙이는 칸
RULE_OR_REVIEW = {"collision_part_A"}
MOTION_CLASSES = ("직진", "좌회전", "우회전")  # 움직임 분류기가 아는 값(나머지는 '기타')
ALWAYS_REVIEW_ONLY_AT = {
    "signal_A": SIGNAL_PLACES,
    "signal_B": SIGNAL_PLACES,
    "road_priority": UNSIGNALED_JUNCTIONS,
    "entry_order": UNSIGNALED_JUNCTIONS,
}


def combine(name: str, det: dict | None, vlm: dict | None) -> tuple[Any, float | None, bool | None]:
    """(값, 확신도, 일치 여부). 규칙: fact-card-v0.md 4절. 다르면 기본은 VLM 값, PREFER_DETECTION
    칸은 탐지 값."""
    if det and vlm:
        agree = same_value(name, det["value"], vlm["value"])
        confs = [c for c in (det["confidence"], vlm["confidence"]) if c is not None]
        conf = (max(confs) if agree else min(confs)) if confs else None
        value = det["value"] if (not agree and name in PREFER_DETECTION) else vlm["value"]
        return value, conf, agree
    one = det or vlm
    return (one["value"], one["confidence"], None) if one else (None, None, None)


def needs_review(
    name: str, value, conf, agree, values: dict, viewpoint: int, has_det: bool = False
) -> bool:
    if name in ALWAYS_REVIEW:
        places = ALWAYS_REVIEW_ONLY_AT.get(name)
        if places is None or values.get("location_type") in places:
            return True
    if name in TRUST_DETECTION:
        return not has_det  # 탐지 값이 있으면 믿고, 없으면(VLM 값만 있으면) 확인

    if conf is not None and conf < REVIEW_THRESHOLD:
        return True
    if agree is False:
        return True
    unknown = value in ("불명", None)
    other_signal = name == "signal_A" if viewpoint == 1 else name.startswith("signal_")
    return unknown and is_required(name, values) and not other_signal


def assemble_card(
    case_id: str,
    raw: dict,
    detections: dict | None,
    frames: list[int],
    hint: int,
    viewpoint: int,
    model_info: dict,
    type_pick: dict | None = None,
) -> dict:
    """type_pick: choose_type 결과. 고른 유형에서 나온 칸은 그 값을 최종값으로 쓰고, 1차 답과 다르면
    확인 표시."""
    det_values = (detections or {}).get("field_opinions", {})
    vlm_values = dict(raw)
    vlm_values["collision_time_ms"] = raw.get("collision_frame")
    type_values, type_conf = {}, None
    if type_pick and type_pick.get("candidate"):
        from workers.vision.accident_types import TYPE_FIELDS

        type_values = {
            k: v for k, v in type_pick["candidate"]["fields"].items() if k in TYPE_FIELDS
        }
        type_conf = type_pick.get("confidence")
    combined, type_detail = {}, {}
    for name, *_ in FIELDS:
        if name == "narrative":
            continue
        det = det_values.get(name)
        vlm = opinion(vlm_values, name)
        value, conf, agree = combine(name, det, vlm)
        if name in type_values:
            tv = type_values[name]
            type_detail[name] = {"value": tv, "confidence": type_conf}
            t_agree = same_value(name, tv, (vlm or {}).get("value"))
            confs = [c for c in (type_conf, conf) if c is not None]
            conf = (max(confs) if t_agree else min(confs)) if confs else None
            value, agree = tv, (t_agree if agree is None else (agree and t_agree))
        combined[name] = (det, vlm, value, conf, agree)
    values = {k: v[2] for k, v in combined.items()}
    #  장소에 맞지 않는 값 정리(fields.tidy_by_location). '불명'이 되면 needs_review에서 확인 표시가
    # 붙는다.
    # 담당자가 장소를 고치면 같은 함수를 다시 돌린다(서비스 쪽).
    tidied = tidy_by_location(values)
    for name, (_, new) in tidied.items():
        det, vlm, _, conf, agree = combined[name]
        combined[name] = (det, vlm, new, conf, agree)
        values[name] = new
    #  도로 관계: 신호 없는 교차로인데 VLM이 '해당없음·불명'이면 장면 분류기(scene_embed) 값으로
    # 채운다(확인 표시는 유지).
    #  2026-11-09: VLM은 이 경우 0~1/3(거의 늘 해당없음), 분류기 교차 검증 66%(동일폭 위주)·개선용
    # 3/3
    rp = ((detections or {}).get("scene") or {}).get("road_priority")
    if (
        rp
        and values.get("location_type") in UNSIGNALED_JUNCTIONS
        and values.get("road_priority") in ("해당없음", "불명", None)
    ):
        _, vlm, old, _, _ = combined["road_priority"]
        combined["road_priority"] = (
            {**rp, "source": "장면 분류기"},
            vlm,
            rp["value"],
            rp["confidence"],
            None,
        )
        tidied["road_priority"] = (old, rp["value"])
        values["road_priority"] = rp["value"]
    #  다른 칸에서 논리적으로 정해지는 값(fields.derive_fields): 충돌 부위 A(추돌·정면), 회전교차로
    # 움직임
    derived = derive_fields(values)
    for name, (old, new) in derived.items():
        det, vlm, _, conf, agree = combined[name]
        combined[name] = (det, vlm, new, conf, agree)
        values[name] = new
        tidied[name] = (tidied.get(name, (old,))[0], new)
    # 규칙으로 정해졌는지(값이 이미 같아 바뀌지 않은 경우 포함)
    ruled = set(
        derive_fields({**values, "collision_part_A": None, "movement_A": None, "movement_B": None})
    )

    frame_ids = [f"f{n:03d}" for n in frames]
    fields = {}
    for name, (det, vlm, value, conf, agree) in combined.items():
        flag = needs_review(name, value, conf, agree, values, viewpoint, has_det=det is not None)
        if name in RULE_OR_REVIEW and name not in ruled:
            flag = (
                True  # 규칙으로 못 정한 경우(예: 옆에서 부딪힘)의 VLM 값은 믿기 어렵다(개선용 2/10)
            )
        if name == "movement_B" and (det or {}).get("source") == "분류기":
            # 영상 모델이 있으면 분류기와 영상 모델이 다를 때만 표시(사람 정답 92건, 2026-11-10):
            #   표시 16건·표시 없이 틀림 12건(표시 없는 칸 84%) vs 카메라 규칙과 비교 22건·14건(80%)
            #   영상 모델 74/92 vs 분류기 69/92는 차이가 확실하지 않아(p=0.30) 값은 분류기 그대로
            #  영상 모델이 없으면: 분류기와 카메라 규칙이 같으면 77%, 다르면 58%(라벨 1,230건) →
            # 다를 때만 표시
            vb = ((((detections or {}).get("video") or {}).get("movement_B")) or {}).get("value")
            if vb == "기타":  # 영상 모델이 '기타'면 카메라 규칙 값과 비교(평가 때와 같음)
                vb = det.get("rule")
            if vb is not None:
                flag = vb != det["value"]
                det = {**det, "video_model": vb}
            else:
                flag = det.get("rule") is not None and det["rule"] != det["value"]
            #  사고 유형 자신감(type_model)이 0.9 미만이면 표시를 더한다(2026-11-10,
            # eval_b_typeflag.py 사람 92건:
            # 표시 없이 틀림 12→7건, 표시 16→24건, 표시 없는 칸 정확도 84→90%). 값은 그대로 분류기
            tconf = (
                (((detections or {}).get("type") or {}).get("fields") or {}).get("movement_B") or {}
            ).get("confidence")
            if tconf is not None:
                flag = flag or tconf < 0.9
                det = {**det, "type_confidence": tconf}
        #  상대 위치·도로 관계: 영상 모델 v3와 다르면 확인 표시(값은 그대로). 2026-11-10 30건에서 v3
        # 20 vs VLM 20,
        #  19 vs 20이라 값으로는 안 쓰고(미리 정한 기준) 표시에만 쓴다. v3는 왼쪽·오른쪽을
        # '옆'으로만 안다
        #  도로 관계는 2026-11-10 저녁부터 j3 기준(정답 카드 30건 j3 21 vs v3 20). 상대 위치는 v3
        # 유지(j3 20 vs v3 22)
        flag_src = (
            "video_j3"
            if name == "road_priority" and (detections or {}).get("video_j3")
            else "video_v3"
        )
        v3 = ((((detections or {}).get(flag_src) or {}).get(name)) or {}).get("value")
        if v3 is not None and name in ("relative_position", "road_priority"):
            mine = "옆" if name == "relative_position" and value in ("왼쪽", "오른쪽") else value
            flag = flag or v3 != mine
        status = "needs_review" if flag else "unverified"
        fields[name] = {
            "value": value,
            "model_value": value,
            "source": "vision",
            "model_confidence": conf,
            "verification_status": status,
            "evidence_frame_ids": frame_ids,
            "source_document_ids": [],
            "vision_detail": {
                "detection": det,
                "vlm": vlm,
                "agree": agree,
                **(
                    {flag_src: v3}
                    if v3 is not None and name in ("relative_position", "road_priority")
                    else {}
                ),
                **({"type_choice": type_detail[name]} if name in type_detail else {}),
                **(
                    {"tidy": {"from": tidied[name][0], "to": tidied[name][1]}}
                    if name in tidied
                    else {}
                ),
            },
        }
    for field, by_place in ((detections or {}).get("motion_by_place") or {}).items():
        # 서비스: 담당자가 장소를 확인·수정하면 이 표에서 그 장소의 예측으로 바꾼다
        # (기타이거나 지금 값이 직진·좌·우 밖의 특별한 움직임이면 그대로)
        if field in fields:
            fields[field]["vision_detail"]["by_place"] = by_place
    text = raw.get("narrative") if isinstance(raw.get("narrative"), str) else None
    fields["narrative"] = {
        "value": text,
        "model_value": text,
        "source": "vision",
        "model_confidence": None,
        "verification_status": "unverified",
        "evidence_frame_ids": frame_ids,
        "source_document_ids": [],
        "vision_detail": {
            "detection": None,
            "vlm": {"value": text, "confidence": None},
            "agree": None,
        },
    }

    ego = "P1" if viewpoint == 1 else None
    return {
        "case_id": case_id,
        "version": 1,
        "accident_date": None,
        "fields": fields,
        "parties": (
            [
                {"party_id": "P1", "role": "촬영 차량", "track_id": None},
                {"party_id": "P2", "role": "상대 차량", "track_id": None},
            ]
            if viewpoint == 1
            else [
                {"party_id": "P1", "role": "상대 차량", "track_id": None},
                {"party_id": "P2", "role": "상대 차량", "track_id": None},
            ]
        ),
        "party_mapping": {
            "video_ego_party_id": ego,
            "insured_party_id": None,
            "chart_party_A_id": "P2" if viewpoint == 1 else "P1",
            "chart_party_B_id": "P1" if viewpoint == 1 else "P2",
        },
        "evidence_frames": [
            {"frame_id": f"f{n:03d}", "timestamp_ms": frame_to_ms(n), "storage_path": None}
            for n in frames
        ],
        "status": "draft",
        "confirmed_by": None,
        "confirmed_at": None,
        "model_info": {
            "prompt_version": PROMPT_VERSION,
            **model_info,
            "frames": frames,
            "collision_hint": hint,
        },
    }


def extract_fact_card_sync(
    case_id: str,
    video_path: str,
    detections: dict | None = None,
    *,
    viewpoint: int = 1,
    collision_hint: int = 75,
    vlm: VLM | None = None,
    context: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    type_select: bool = False,
) -> dict:
    """detections가 있으면 궤적을 글로 요약해 질문에 붙이고 사진에 박스를 그린다.

    detections = {"tracks": {"A": [(frame, x, y, w, h), ...], "B": [...]}, "size": (가로, 세로),
                  "source": "정답 박스" | "YOLO", "hint": 글, "ego": EgoMotion}
    prompt_version: "v0.1"(처음 질문) | "v0.2"(오답 분석 후 개선) ...
    type_select: True면 2차 질문으로 사고 유형을 고르게 하고, 유형에서 나온 칸을 최종값으로
    쓴다(비용 약 2배)
    """
    vlm = vlm or OllamaVLM()
    frames = select_frames(collision_hint, version=prompt_version)
    tracks = (detections or {}).get("tracks")
    if tracks:
        w, h = (detections or {}).get("size", (1920, 1080))
        summary = summarize_tracks(
            tracks, w, h, viewpoint, (detections or {}).get("source", "탐지")
        )
        guide = (
            "사진의 빨간 박스가 A, 파란 박스가 B다. 아래 궤적은 사진보다 많은 프레임에서 뽑은 위치이니 "
            "움직임·차로 변경·상대 위치·충돌 순간을 판단할 때 함께 써라."
        )
        context = "\n".join(c for c in (context, guide, summary) if c)
    # D안: 숫자 궤적 없이 사진에 상대 차(A) 박스만 그린다(YOLO가 고른 차, 확실할 때만)
    mark = (detections or {}).get("mark_tracks")
    if mark and not tracks:
        guide = (
            "사진의 빨간 박스 'A'가 상대 차다(자동 탐지라 드물게 틀릴 수 있음). "
            "A의 움직임과 충돌 부위는 이 차를 따라가며 판단하라."
        )
        context = "\n".join(c for c in (context, guide) if c)
    hint = (detections or {}).get("hint")  # 예: 카메라 움직임 계산 요약(ego_motion)
    if hint:
        context = "\n".join(c for c in (context, hint) if c)
    facts = (detections or {}).get("facts")  # 예: 신호등 탐지 사실(detection.light_fact)
    if facts:
        context = "\n".join(c for c in (context, facts) if c)
    images = render_frames(
        video_path, frames, collision_hint, tracks=tracks or mark, version=prompt_version
    )
    prompt = build_prompt(viewpoint, frames, collision_hint, context, version=prompt_version)
    raw, meta = vlm.generate(prompt, images, response_schema(prompt_version))
    ego = (detections or {}).get("ego")
    vlm_place = (raw.get("location_type") or {}).get("value")
    #  영상 모델 장소(detections["video"] = video_model.predict 결과)가 있으면 그 장소로 나머지
    # 계산(카메라 판단,
    # 움직임 분류기 장소별 예측)을 한다. 카드 장소 칸도 이 값(PREFER_DETECTION). 2026-11-10
    video_place = ((detections or {}).get("video") or {}).get("location_type") or {}
    place = video_place.get("value") or vlm_place
    ego_movement = None
    if ego is not None:
        #  카메라 계산의 충돌 순간은 덜 정확하므로(개선용 3/7 vs VLM 6/7) VLM이 찾은 충돌 순간
        # 직전으로 다시 판단
        vlm_frame = (raw.get("collision_frame") or {}).get("value")
        decided = ego.decide_at(vlm_frame if isinstance(vlm_frame, int) else None)
        ops = decided.field_opinions(place)
        detections = {**detections, "field_opinions": ops}
        ego_movement = ops["movement_B"]["value"]
    box_ops = (detections or {}).get(
        "box_opinions"
    )  # YOLO 상대 차 박스 판단(detection.box_detections)
    if box_ops:
        detections = {
            **detections,
            "field_opinions": {**detections.get("field_opinions", {}), **box_ops},
        }
    # 움직임 분류기(motion_model.motion_opinions): '기타'가 아니면 움직임 A·B 값으로 쓴다.
    # 회전교차로는 분류기가 직진·좌·우만 알아서 쓰지 않는다(진입·회전은 기존 규칙).
    motion = (detections or {}).get("motion")
    if motion and place != "회전교차로":
        ops = dict(detections.get("field_opinions", {}))
        for field, m in motion.items():
            if "by_place" in m:  # 카드 장소의 예측(담당자가 장소를 고치면 by_place에서 바꿈)
                detections = {
                    **detections,
                    "motion_by_place": {
                        **detections.get("motion_by_place", {}),
                        field: m["by_place"],
                    },
                }
                m = m["by_place"].get(place)
                if not m or m["value"] == "기타":
                    continue  # 기타면 VLM 값
            rule = (ops.get(field) or {}).get("value")
            #  지금 값(A: VLM, B: 카메라 규칙)이 직진·좌·우 밖의 특별한 움직임이면 분류기로 덮지
            # 않는다.
            #  분류기는 직진·좌·우·기타만 알아서 그런 경우 맞힐 수 없다(개선용: VLM 주정차·후진을
            # 직진으로 덮어 틀림)
            current = rule if field == "movement_B" else (raw.get(field) or {}).get("value")
            if current not in (None, "불명", *MOTION_CLASSES):
                continue
            ops[field] = {
                "value": m["value"],
                "confidence": m["confidence"],
                "source": "분류기",
                "probs": m.get("probs"),
                "rule": rule,
            }
        detections = {**detections, "field_opinions": ops}
    #  움직임 A: 영상 모델 값(2026-11-10). 사람 정답 92건 영상 모델 57 vs VLM 51(p=0.35, 미리 정한
    # 기준 미달),
    #  처음 보는 62건 45 vs 35(p=0.052). VLM은 거의 항상 직진(좌회전 5/20, 우회전 0/11), 영상 모델은
    # 12/20, 7/11.
    #  결과를 보고 바꾼 결정이라 새 정답 카드 세트로 다시 확인한다. 확인 표시는 그대로 항상
    # 붙인다(ALWAYS_REVIEW).
    #  영상 모델이 '기타'거나 VLM이 직진·좌·우 밖의 특별한 움직임이면 VLM 값(분류기와 같은 규칙),
    # 회전교차로는 안 씀
    video_a = ((detections or {}).get("video") or {}).get("movement_A") or {}
    #  섞기(2026-11-10): v3가 진로변경·추월이라고 하면 v3 값을 쓴다. 라벨 재확인 후 92건 v2 56 vs
    # 섞기 58,
    # 진로변경·추월 맞은 비율 6/11(기준 50%)로 아슬아슬하게 채택 — 근거 약함
    #  2026-11-10 저녁: 진로변경·추월 판단을 j3(V-JEPA 2를 우리 영상으로 학습)로 교체. 92건 섞기
    # v2+j3 59 vs v2+v3 58,
    #  맞은 비율 5/10(기준 50%) — 1건 차이(p=1.00)라 사실상 비김, 미리 정한 기준(많으면 교체)대로
    # 바꿈. j3가 없으면 v3
    special_src = "video_j3" if (detections or {}).get("video_j3") else "video_v3"
    v3_a = ((detections or {}).get(special_src) or {}).get("movement_A") or {}
    if v3_a.get("value") in ("진로변경", "추월"):
        video_a = v3_a
    vlm_a = (raw.get("movement_A") or {}).get("value")
    if (
        video_a.get("value") not in (None, "기타")
        and place != "회전교차로"
        and vlm_a in (None, "불명", *MOTION_CLASSES)
    ):
        detections = {
            **detections,
            "field_opinions": {
                **detections.get("field_opinions", {}),
                "movement_A": {
                    "value": video_a["value"],
                    "confidence": video_a.get("confidence"),
                    "source": f"영상 모델 {special_src[6:]}" if video_a is v3_a else "영상 모델",
                    "probs": video_a.get("probs"),
                },
            },
        }
    if video_place.get("value"):  # 앞 단계들이 field_opinions를 새로 만들므로 마지막에 넣는다
        detections = {
            **detections,
            "field_opinions": {
                **detections.get("field_opinions", {}),
                "location_type": {
                    "value": video_place["value"],
                    "confidence": video_place.get("confidence"),
                    "source": "영상 모델",
                    "probs": video_place.get("probs"),
                },
            },
        }
    type_pick = None
    if type_select == "choose":  # VLM이 후보 목록에서 고르기(2026-11-06: 앞 번호만 골라 1/10)
        type_pick = choose_type(vlm, images, viewpoint, place, ego_movement, raw.get("scene_notes"))
    elif type_select:  # 기본: 코드가 점수로 고르기(추가 API 없음)
        type_pick = score_type(raw, viewpoint, ego_movement)
    tin = meta["prompt_tokens"] + ((type_pick or {}).get("prompt_tokens") or 0)
    tout = meta["output_tokens"] + ((type_pick or {}).get("output_tokens") or 0)
    info = {
        "prompt_version": prompt_version,
        "scene_notes": raw.get("scene_notes"),
        "vlm": vlm.name,
        "seconds": meta["seconds"],
        "prompt_tokens": tin,
        "output_tokens": tout,
        "stage": "+".join(
            ["vlm"]
            + (["tracks"] if tracks else [])
            + (["mark"] if mark and not tracks else [])
            + (["ego"] if hint else [])
            + (["yolo"] if box_ops else [])
            + (["light"] if facts else [])
            + (["video"] if video_place.get("value") else [])
            + (["type"] if type_select else [])
        ),
        "context": context,
        "mark": (detections or {}).get("mark_info"),
        "scene_hint": (detections or {}).get("scene"),  # 담당자 참고: 장면 분류기의 장소·도로 관계
        "yolo": (detections or {}).get("yolo_info"),
    }
    if type_pick:
        c = type_pick.get("candidate")
        info["type_choice"] = {
            "accident_type": c and c["type"],
            "swap": c and c["swap"],
            "description": c and describe_choice(c),
            "confidence": type_pick["confidence"],
            "reason": type_pick["reason"],
            "n_candidates": type_pick["n_candidates"],
            "mode": "choose" if type_select == "choose" else "score",
            "top3": type_pick.get("top3"),
        }
    card = assemble_card(
        case_id, raw, detections, frames, collision_hint, viewpoint, info, type_pick
    )
    card["model_info"]["raw_output"] = raw
    return card


async def extract_fact_card(
    case_id: str, video_path: str, detections: dict[str, object]
) -> FactCard:
    """모델 원값·확신도·증거·불명/충돌 보존. 담당자 확인 전에는 draft다.

    반환 카드는 초안 v0 칸(parties, model_info, vision_detail)을 더 가진다. 계약 합의 후 FactCard를
    맞춘다.
    """
    #  type: ignore[return-value]
    return await asyncio.to_thread(extract_fact_card_sync, case_id, video_path, detections or None)
