"""A: 사진 + 질문 → JSON 답을 주는 VLM 연결. 모두 VLM 규약(generate)을 따른다.

- OpenAIVLM: 서비스 기본(gpt-4o). 키는 환경 변수 OPENAI_API_KEY(workers/vision/.env).
- ClaudeVLM·OllamaVLM: 비교 실험용(evals/vision/run_cards.py --vlm).
- 외부 패키지(openai·anthropic·requests)는 함수 안에서 불러온다(팀 CI에는 없음).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Protocol

from workers.vision.env import load_env


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
