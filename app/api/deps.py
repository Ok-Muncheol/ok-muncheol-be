"""B(통합): 요청 공통 의존성. `actor_id`는 검증한 Supabase 토큰의 `sub`에서만 얻는다.

브라우저가 보낸 값으로 담당자를 정하지 않는다.
사건 소유권 검사는 각 서비스 함수(A 사건 테이블·RLS)가 한다.
토큰 검증 순서: SUPABASE_URL의 JWKS(비대칭 서명) → SUPABASE_JWT_SECRET(레거시 HS256).
"""

from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header

from app.api.errors import ApiException, unauthorized
from app.config import Settings, get_settings

ASYMMETRIC_ALGORITHMS = ["ES256", "RS256", "EdDSA"]


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=600)


def _decode(token: str, settings: Settings) -> dict[str, object]:
    options = {"require": ["exp", "sub"]}
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise unauthorized() from exc
    algorithm = header.get("alg")
    try:
        if algorithm == "HS256":
            if not settings.supabase_jwt_secret:
                raise unauthorized("이 서명 방식의 토큰은 받지 않습니다.")
            return jwt.decode(
                token, settings.supabase_jwt_secret, algorithms=["HS256"],
                audience=settings.jwt_audience, options=options,
            )
        if algorithm in ASYMMETRIC_ALGORITHMS and settings.jwks_url:
            try:
                signing_key = _jwks_client(settings.jwks_url).get_signing_key_from_jwt(token)
            except jwt.PyJWKClientConnectionError as exc:
                # 공개키 목록을 받아 오지 못한 것만 서버 쪽 문제(503)다.
                # 목록에 없는 키 ID는 위조·다른 프로젝트 토큰이므로 아래에서 401로 떨어진다
                raise ApiException(
                    503, "auth_unavailable", "인증 키를 불러오지 못했습니다.", True
                ) from exc
            return jwt.decode(
                token, signing_key.key, algorithms=ASYMMETRIC_ALGORITHMS,
                audience=settings.jwt_audience, issuer=settings.jwt_issuer, options=options,
            )
    except jwt.PyJWTError as exc:
        raise unauthorized() from exc
    raise unauthorized("이 서명 방식의 토큰은 받지 않습니다.")


def get_actor_id(
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),  # noqa: B008 — FastAPI 의존성 선언
) -> str:
    if settings.dev_auth_enabled and not authorization:
        return str(settings.dev_actor_id)
    if not (settings.jwks_url or settings.supabase_jwt_secret):
        raise ApiException(503, "auth_not_configured", "서버 인증 설정이 없습니다.")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized()
    claims = _decode(token.strip(), settings)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise unauthorized()
    return subject


ActorId = Annotated[str, Depends(get_actor_id)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
