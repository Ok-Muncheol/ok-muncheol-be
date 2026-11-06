"""B(통합): FastAPI 조립. 지금은 Vercel 배포 진입점과 /health만 있고 인증·라우터는 비어 있다."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """인증·권한·오류·라우터·수명주기를 연결하고 OpenAPI를 제공한다.

    TODO(B): 인증/권한 의존성, 담당별 라우터(app/api/*), 오류 응답(ApiError) 연결.
    """
    application = FastAPI(title="ok-muncheol-be", version="0.1.0")

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


# Vercel Python 런타임은 모듈 수준 `app`을 진입점으로 찾는다(PYTHON_ENTRYPOINT_NOT_FOUND 방지).
app = create_app()
