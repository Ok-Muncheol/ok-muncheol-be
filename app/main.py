"""B(통합): FastAPI 조립 — 오류 응답(ApiError)·CORS·담당별 라우터. Vercel 배포 진입점."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.routes import router
from app.config import get_settings


def create_app() -> FastAPI:
    """보호 경로는 전부 `app/api/deps.py`의 토큰 검증을 거친다. `/health`만 인증 없이 열린다."""
    settings = get_settings()
    application = FastAPI(title="ok-muncheol-be", version="0.1.0")
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
