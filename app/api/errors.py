"""B(통합): 모든 오류를 `ApiError`(code·message·retryable) 한 모양으로 돌려준다.

담당 서비스 함수는 `ApiException`을 던진다. HTTP 상태와 코드는 아래 생성 함수로 맞춘다.
`NotImplementedError`는 501 `not_implemented`로 바꿔 FE가 미구현 경로를 구분하게 한다.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.common import ApiError

logger = logging.getLogger(__name__)


class ApiException(Exception):  # noqa: N818 — 담당 모듈이 읽기 쉬운 이름을 유지한다
    def __init__(self, status_code: int, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.error: ApiError = {"code": code, "message": message, "retryable": retryable}


def unauthorized(message: str = "인증 토큰이 없거나 유효하지 않습니다.") -> ApiException:
    return ApiException(401, "unauthorized", message)


def forbidden(message: str = "이 사건에 접근할 권한이 없습니다.") -> ApiException:
    return ApiException(403, "forbidden", message)


def not_found(message: str = "대상을 찾을 수 없습니다.") -> ApiException:
    return ApiException(404, "not_found", message)


def version_conflict(
    message: str = "다른 버전이 이미 저장되었습니다. 다시 조회하세요.",
) -> ApiException:
    return ApiException(409, "version_conflict", message)


def unprocessable(code: str, message: str) -> ApiException:
    """범위 밖·미확정 카드 등 요청 모양은 맞지만 처리할 수 없는 상태."""
    return ApiException(422, code, message)


def _body(code: str, message: str, retryable: bool) -> dict[str, object]:
    error: ApiError = {"code": code, "message": message, "retryable": retryable}
    return {"error": error}


_HTTP_CODES = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiException)
    async def _api(_: Request, exc: ApiException) -> JSONResponse:
        return JSONResponse({"error": exc.error}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        return JSONResponse(
            _body("invalid_request", f"요청 값이 올바르지 않습니다: {', '.join(fields)}", False),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, f"http_{exc.status_code}")
        return JSONResponse(
            _body(code, str(exc.detail), exc.status_code >= 500), status_code=exc.status_code
        )

    @app.exception_handler(NotImplementedError)
    async def _not_implemented(_: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse(
            _body("not_implemented", f"아직 구현되지 않은 기능입니다 ({exc})", False),
            status_code=501,
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 내부 메시지·스택은 응답에 싣지 않고 서버 로그에만 남긴다
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            _body("internal_error", "서버 오류가 발생했습니다. 잠시 후 다시 시도하세요.", True),
            status_code=500,
        )
