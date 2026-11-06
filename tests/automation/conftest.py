import time

import pytest


@pytest.fixture
def fake_gh(monkeypatch):
    """스크립트의 gh 호출을 대역으로 바꾼다. 응답을 차례로 돌려주고, 예외면 던지고, 대기는 건너뛴다.

    받은 인자 목록을 돌려준다.
    """

    def install(module, *responses):
        queue = list(responses)
        calls = []

        def gh(*args):
            calls.append(args)
            response = queue.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(module, "gh", gh)
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        return calls

    return install
