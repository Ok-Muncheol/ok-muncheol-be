"""B(통합): FastAPI 조립 — 오류 응답(ApiError)·CORS·담당별 라우터·시작 시 지식베이스 연결.

Vercel 배포 진입점.
"""

import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from app import corpus_import, modal_dispatch
from app.api import analysis, reports
from app.api.errors import install_error_handlers
from app.api.routes import router
from app.config import get_settings
from app.retrieval.embedding import RetrievalNotConfigured

logger = logging.getLogger(__name__)

# 지식베이스 연결이 실패한 뒤 다시 해 보기까지(요청마다 코퍼스를 받으려 하지 않게)
KNOWLEDGE_BASE_RETRY_SECONDS = 60.0
_now = time.monotonic


def connect_knowledge_base() -> bool:
    """D 지식베이스(코퍼스 색인·질의 임베더·면책 조항 판정)를 켠다. 켜졌으면 True.

    CORPUS_IMPORT_PATH가 있으면(운영) 비공개 Storage의 db_import 사본을 받아 SHA-256으로 검증한
    폴더를 읽고, CORPUS_IMPORT_EVIDENCE_STORE면 그 사본으로 EvidenceStore 어댑터도 등록한다
    (app/corpus_import.py). 없으면(로컬) 로더 기본 경로 파일을 읽는다.
    파일·키·패키지·설정이 없거나 못 쓰면(RetrievalNotConfigured와 하위 클래스)
    경고 로그만 남기고 꺼진 채로 둔다. 연결 함수는 실패하면 전역 설정을 바꾸지 않는다.
    코드 버그(TypeError 등)는 감싸지 않아 서버 시작 실패로 드러난다.
    """
    settings = get_settings()
    started = time.perf_counter()
    try:
        if settings.corpus_import_path:
            base = corpus_import.fetch_db_import(settings)
            reports.configure_with_knowledge_base(
                base, import_evidence_store=settings.corpus_import_evidence_store
            )
        else:
            reports.configure_with_knowledge_base()
    except RetrievalNotConfigured as exc:
        logger.warning("지식베이스 없이 시작합니다 — %s: %s", type(exc).__name__, exc)
        return False
    logger.info("지식베이스를 켰습니다(%.1f초)", time.perf_counter() - started)
    return True


class KnowledgeBaseSwitch:
    """앱 하나의 지식베이스 연결. 한 번 켜지면 다시 부르지 않고, 실패하면 잠시 뒤 다시 해 본다.

    서버 시작(lifespan)이 먼저 켜 보고, 시작이 돌지 않은 런타임이나 실패한 뒤에는 첫 요청이 켠다.
    동시에 들어온 요청은 잠금으로 한 번만 연결한다.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.connected = False
        self._failed_at: float | None = None
        self._lock = threading.Lock()

    def ensure(self, *, during_request: bool = False) -> bool:
        """켜져 있으면 True. 요청 중이면 코드 버그도 로그만 남겨 다른 경로까지 막지 않는다."""
        if not self.enabled or self.connected:
            return self.connected
        with self._lock:
            if self.connected:
                return True
            if self._failed_at is not None and \
                    _now() - self._failed_at < KNOWLEDGE_BASE_RETRY_SECONDS:
                return False
            try:
                self.connected = connect_knowledge_base()
            except Exception:
                if not during_request:
                    raise
                logger.exception("지식베이스 연결 중 오류 — 잠시 뒤 요청에서 다시 해 본다")
                self.connected = False
            self._failed_at = None if self.connected else _now()
            return self.connected


class KnowledgeBaseOnFirstRequest:
    """순수 ASGI 미들웨어: 지식베이스가 아직 꺼져 있으면 `/health`·OPTIONS가 아닌 요청 앞에서 켠다.

    BaseHTTPMiddleware를 쓰지 않아 응답 본문(스트리밍 포함)을 건드리지 않는다.
    """

    def __init__(self, app: ASGIApp, switch: KnowledgeBaseSwitch) -> None:
        self.app = app
        self.switch = switch

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http" and not self.switch.connected
                and scope.get("method") != "OPTIONS" and scope.get("path") != "/health"):
            await run_in_threadpool(self.switch.ensure, during_request=True)
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    """보호 경로는 전부 `app/api/deps.py`의 토큰 검증을 거친다. `/health`만 인증 없이 열리고,
    Modal 콜백은 사용자 토큰 대신 작업별 일회용 토큰으로 확인한다(`app/api/routes.py`).

    Modal 설정(`MODAL_ANALYZE_URL` 등)이 다 있으면 영상 분석 접수가 Modal을 부르게 끼운다
    (`app/modal_dispatch.py`). 없으면 접수는 `analysis_not_configured`로 실패한다.

    지식베이스는 LOAD_KNOWLEDGE_BASE가 켜졌을 때만 서버 시작(lifespan)에서 연결한다. 시작이 돌지
    않았거나 실패했으면 `/health`·OPTIONS가 아닌 첫 요청이 연결한다(KnowledgeBaseSwitch).
    시작에 몇 초·약 300MB가 들어 기본은 꺼짐이고, 앱을 만들거나 import만 해서는 적재하지 않는다.
    """
    settings = get_settings()
    if modal_dispatch.is_configured(settings):
        analysis.configure(dispatch=modal_dispatch.dispatch_to_modal)
    knowledge_base = KnowledgeBaseSwitch(settings.load_knowledge_base)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        knowledge_base.ensure()
        yield

    application = FastAPI(title="ok-muncheol-be", version="0.1.0", lifespan=lifespan)
    install_error_handlers(application)
    if settings.load_knowledge_base:
        # CORS보다 먼저 더해 안쪽에 둔다. 오류 응답에도 CORS 헤더가 붙는다
        application.add_middleware(KnowledgeBaseOnFirstRequest, switch=knowledge_base)

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
