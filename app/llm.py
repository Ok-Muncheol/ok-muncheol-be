"""B: LLM 호출 1회의 시간 상한·재시도 정책.

lumi-wellness-agent-be `app/agent/llm.py`(원본 cure-agent-be §11 4단 방어)를 옮겼다.
쓰는 곳은 질의응답 에이전트(app/agents/qa.py)와 반박 검토(app/validation/rebuttal.py)다.

- 호출 1회는 LLM_TIMEOUT_SECONDS(45초)에서 끊는다. SDK 기본값(OpenAI 600초)은 Vercel 함수
  한도(Hobby 300초)보다 길다. 45초는 cure-agent-be가 추론 모델의 첫 응답에 맞춰 잡은 값이다
  (gpt-5-mini 실측 약 9.5초라 10초로는 매번 끊겼다).
- 벤더 SDK의 자체 재시도(기본 2회)는 끈다(max_retries=0). 여기 규칙과 이중화되기 때문이다.
- 재시도는 타임아웃·네트워크·5xx만 1회(BACKOFF_SECONDS 뒤). 4xx(400·401·403·429 …)는 같은
  요청을 다시 보내도 결과가 같으므로(설정·키·한도) 바로 올린다.
- 상태 코드도 연결 오류도 아닌 예외(응답 모양 검사 같은 우리 코드의 오류)는 재시도 없이 올린다.
- 시간 제한은 LLM 호출에만 둔다. DB 조회·검색·판단 그래프 같은 도구 실행에는 두지 않는다.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 45.0  # 호출 1회
MAX_ATTEMPTS = 2  # 재시도 1회
BACKOFF_SECONDS = 0.3  # × 시도 번호


class ErrorKind(StrEnum):
    RETRYABLE = "RETRYABLE"  # 5xx·네트워크·타임아웃 → 같은 호출을 1회 더
    CLIENT = "CLIENT"  # 4xx → 재시도 없이 올린다
    OTHER = "OTHER"  # 공급자 장애가 아니다 → 그대로 올린다


def _connection_error_types() -> tuple[type[BaseException], ...]:
    import openai

    # APITimeoutError는 APIConnectionError의 하위 클래스다
    return (TimeoutError, ConnectionError, openai.APIConnectionError)


def classify(exc: BaseException) -> ErrorKind:
    """SDK 예외를 재시도·전파로 가른다. 상태 코드는 SDK의 APIStatusError가 가진다."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return ErrorKind.RETRYABLE if status >= 500 else ErrorKind.CLIENT
    if isinstance(exc, _connection_error_types()):
        return ErrorKind.RETRYABLE
    return ErrorKind.OTHER


def is_timeout(exc: BaseException) -> bool:
    """호출 시간 초과. openai.APITimeoutError는 파이썬 TimeoutError의 하위 클래스가 아니다."""
    import openai

    return isinstance(exc, TimeoutError | openai.APITimeoutError)


async def with_retry[T](attempt: Callable[[], Awaitable[T]], *, name: str = "llm") -> T:
    """일시 오류(RETRYABLE)만 MAX_ATTEMPTS까지 다시 시도한다. 그 밖의 오류는 그대로 올린다."""
    for i in range(MAX_ATTEMPTS):
        try:
            return await attempt()
        except Exception as exc:
            if classify(exc) is not ErrorKind.RETRYABLE or i == MAX_ATTEMPTS - 1:
                raise
            delay = BACKOFF_SECONDS * (i + 1)
            logger.warning(
                "LLM %s 일시 오류(%s) — %.1fs 뒤 재시도", name, type(exc).__name__, delay
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def openai_client(timeout: float = LLM_TIMEOUT_SECONDS) -> "AsyncOpenAI":
    """호출 1회 상한을 건 OpenAI 클라이언트. SDK 자체 재시도는 끈다(재시도는 with_retry)."""
    from openai import AsyncOpenAI

    return AsyncOpenAI(timeout=timeout, max_retries=0)
