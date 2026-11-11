"""LLM 호출 정책 테스트용 가짜 OpenAI 클라이언트.

app/llm.py openai_client 자리에 넣는다. 네트워크를 쓰지 않는다.
"""

from types import SimpleNamespace

import httpx
import openai

REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=REQUEST)


def status_error(code: int) -> openai.APIStatusError:
    return openai.APIStatusError(
        "upstream", response=httpx.Response(code, request=REQUEST), body=None
    )


def completion(content: str | None, tool_calls: list | None = None) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClients:
    """openai_client() 대역. 호출마다 outcomes를 앞에서부터 하나씩 돌려주거나 던진다."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.made: list[float] = []  # 만든 클라이언트의 호출 1회 상한(재시도마다 새로 만든다)
        self.requests: list[dict] = []

    def __call__(self, timeout: float = 0.0) -> "FakeClients":
        self.made.append(timeout)
        return self

    async def __aenter__(self) -> "FakeClients":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    @property
    def chat(self) -> "FakeClients":
        return self

    @property
    def completions(self) -> "FakeClients":
        return self

    async def create(self, **request: object) -> object:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
