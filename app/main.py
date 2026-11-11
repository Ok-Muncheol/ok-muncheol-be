"""B(통합): FastAPI 조립 — 오류 응답(ApiError)·CORS·담당별 라우터·시작 시 지식베이스 연결.

Vercel 배포 진입점.
"""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import reports
from app.api.errors import install_error_handlers
from app.api.routes import router
from app.config import get_settings
from app.retrieval.embedding import RetrievalNotConfigured

logger = logging.getLogger(__name__)


def connect_knowledge_base() -> bool:
    """D 지식베이스(코퍼스 색인·질의 임베더·면책 조항 판정)를 켠다. 켜졌으면 True.

    파일·키·패키지·설정이 없거나 못 쓰면(RetrievalNotConfigured와 하위 클래스)
    경고 로그만 남기고 꺼진 채로 둔다. 연결 함수는 실패하면 전역 설정을 바꾸지 않는다.
    코드 버그(TypeError 등)는 감싸지 않아 서버 시작 실패로 드러난다.
    """
    started = time.perf_counter()
    try:
        reports.configure_with_knowledge_base()
    except RetrievalNotConfigured as exc:
        logger.warning("지식베이스 없이 시작합니다 — %s: %s", type(exc).__name__, exc)
        return False
    logger.info("지식베이스를 켰습니다(%.1f초)", time.perf_counter() - started)
    return True


def create_app() -> FastAPI:
    """보호 경로는 전부 `app/api/deps.py`의 토큰 검증을 거친다. `/health`만 인증 없이 열린다.

    지식베이스는 LOAD_KNOWLEDGE_BASE가 켜졌을 때만 서버 시작(lifespan)에서 연결한다.
    로컬에서 약 12초·230MB가 들어 기본은 꺼짐이고, 앱을 만들거나 import만 해서는 적재하지 않는다.
    """
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.load_knowledge_base:
            connect_knowledge_base()
        yield

    application = FastAPI(title="ok-muncheol-be", version="0.1.0", lifespan=lifespan)
    install_error_handlers(application)
    if settings.cors_allow_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )

    @application.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    application.include_router(router)
    return application


# Vercel Python 런타임은 모듈 수준 `app`을 진입점으로 찾는다(PYTHON_ENTRYPOINT_NOT_FOUND 방지).
app = create_app()
