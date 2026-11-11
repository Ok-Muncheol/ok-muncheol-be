"""B(통합): 서버 시작 시 지식베이스 연결.

LOAD_KNOWLEDGE_BASE가 켜질 때만 부르고, 연결 실패는 경고만 남긴다.
진짜 연결(코퍼스 88MB 적재·OpenAI)은 부르지 않는다.
reports.configure_with_knowledge_base를 바꿔 끼운다.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from app import main
from app.api import reports
from app.api.reports import EmbeddingSettingsInvalid
from app.config import get_settings
from app.retrieval.embedding import RetrievalNotConfigured
from app.retrieval.loader import CorpusDataError


@pytest.fixture
def connect(monkeypatch):
    """연결 함수 호출 횟수를 센다. raise_with에 예외를 넣으면 그 예외로 실패한다."""
    state: dict[str, object] = {"calls": 0, "raise_with": None}

    def fake() -> reports.ReportServices:
        state["calls"] += 1  # type: ignore[operator]
        if state["raise_with"] is not None:
            raise state["raise_with"]  # type: ignore[misc]
        return reports.services

    monkeypatch.setattr(reports, "configure_with_knowledge_base", fake)
    yield state
    get_settings.cache_clear()


def serve(monkeypatch, value: str | None) -> TestClient:
    """LOAD_KNOWLEDGE_BASE를 정하고 앱을 만든다.

    with 블록에 들어갈 때 lifespan(서버 시작)이 돈다.
    """
    if value is None:
        monkeypatch.delenv("LOAD_KNOWLEDGE_BASE", raising=False)
    else:
        monkeypatch.setenv("LOAD_KNOWLEDGE_BASE", value)
    get_settings.cache_clear()
    return TestClient(main.create_app())


@pytest.mark.parametrize("value", [None, "", "0", "false", "no"])
def test_off_by_default(monkeypatch, connect, value):
    with serve(monkeypatch, value) as client:
        assert client.get("/health").status_code == 200
    assert connect["calls"] == 0


@pytest.mark.parametrize("value", ["1", "true", " TRUE "])
def test_on_connects_once_at_startup(monkeypatch, connect, caplog, value):
    with caplog.at_level(logging.INFO, logger="app.main"), serve(monkeypatch, value) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200  # 요청마다 다시 부르지 않는다
    assert connect["calls"] == 1
    assert "지식베이스를 켰습니다" in caplog.text


def test_building_the_app_does_not_connect(monkeypatch, connect):
    """앱을 만들기만 하면(테스트 import·Vercel 진입점) 적재하지 않는다.

    서버가 시작할 때만 부른다.
    """
    monkeypatch.setenv("LOAD_KNOWLEDGE_BASE", "1")
    get_settings.cache_clear()
    main.create_app()
    assert connect["calls"] == 0


@pytest.mark.parametrize(
    "error",
    [
        RetrievalNotConfigured("D: 코퍼스 적재 파일이 없습니다: db_import"),
        CorpusDataError("D: 코퍼스 파일 corpus_chunks.jsonl 3번째 줄: 깨진 JSON"),
        EmbeddingSettingsInvalid("D: 질의 임베딩 설정이 맞지 않습니다: 차원 1536 ≠ 3072"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_not_configured_starts_without_it(monkeypatch, connect, caplog, error):
    connect["raise_with"] = error
    with caplog.at_level(logging.INFO, logger="app.main"), serve(monkeypatch, "1") as client:
        assert client.get("/health").status_code == 200
    [record] = [r for r in caplog.records if r.name == "app.main"]
    assert record.levelno == logging.WARNING
    assert f"{type(error).__name__}: {error}" in record.getMessage()


def test_code_bug_fails_startup(monkeypatch, connect):
    """코드 버그는 감싸지 않는다. 경고 한 줄로 묻히지 않고 서버 시작이 실패한다."""
    connect["raise_with"] = TypeError("버그")
    with pytest.raises(TypeError, match="버그"), serve(monkeypatch, "1"):
        pass
