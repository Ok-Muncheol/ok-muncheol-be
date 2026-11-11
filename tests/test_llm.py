"""B: LLM 호출 1회 정책(app/llm.py).

호출 1회 시간 상한, SDK 재시도 끔, 재시도는 일시 오류만 1회.
"""

import asyncio

import openai
import pytest

from app import llm
from tests.fake_openai import REQUEST, status_error, timeout_error


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "BACKOFF_SECONDS", 0)


@pytest.mark.parametrize(("exc", "kind"), [
    (status_error(500), llm.ErrorKind.RETRYABLE),
    (status_error(503), llm.ErrorKind.RETRYABLE),
    (timeout_error(), llm.ErrorKind.RETRYABLE),
    (openai.APIConnectionError(request=REQUEST), llm.ErrorKind.RETRYABLE),
    (TimeoutError(), llm.ErrorKind.RETRYABLE),
    (ConnectionError(), llm.ErrorKind.RETRYABLE),
    (status_error(400), llm.ErrorKind.CLIENT),
    (status_error(401), llm.ErrorKind.CLIENT),
    (status_error(429), llm.ErrorKind.CLIENT),
    (ValueError("응답 모양이 틀렸다"), llm.ErrorKind.OTHER),
])
def test_errors_are_classified_for_retry(exc, kind):
    assert llm.classify(exc) is kind


def test_timeouts_include_the_sdk_timeout_error():
    assert llm.is_timeout(timeout_error()) and llm.is_timeout(TimeoutError())
    assert not llm.is_timeout(openai.APIConnectionError(request=REQUEST))
    assert not llm.is_timeout(status_error(504))


def scripted(*outcomes: object):
    calls: list[int] = []

    async def attempt():
        calls.append(1)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return attempt, calls


def test_a_transient_error_is_retried_once():
    attempt, calls = scripted(timeout_error(), "답")
    assert asyncio.run(llm.with_retry(attempt)) == "답"
    assert len(calls) == llm.MAX_ATTEMPTS == 2


def test_it_gives_up_after_the_retry():
    attempt, calls = scripted(status_error(502), status_error(503), "쓰이지 않음")
    with pytest.raises(openai.APIStatusError):
        asyncio.run(llm.with_retry(attempt))
    assert len(calls) == 2


@pytest.mark.parametrize("exc", [status_error(429), status_error(400), ValueError("우리 코드")])
def test_client_errors_and_our_errors_are_not_retried(exc):
    attempt, calls = scripted(exc, "쓰이지 않음")
    with pytest.raises(type(exc)):
        asyncio.run(llm.with_retry(attempt))
    assert len(calls) == 1


def test_the_client_cuts_one_call_and_leaves_retries_to_the_policy(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = llm.openai_client()
    try:
        # SDK 기본값(600초·재시도 2회)이 아니라 호출 1회 45초·SDK 재시도 없음
        assert (client.timeout, client.max_retries) == (llm.LLM_TIMEOUT_SECONDS, 0)
        assert llm.LLM_TIMEOUT_SECONDS == 45
    finally:
        asyncio.run(client.close())
