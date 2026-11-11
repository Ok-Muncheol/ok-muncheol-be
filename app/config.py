"""B(통합): 서버 환경 변수. 값은 `.env`(로컬)·Vercel 환경 변수에만 둔다. 양식은 `.env.example`.

인증은 기본으로 닫혀 있다(fail-closed). 검증 수단이 하나도 없으면 보호 경로는 503을 돌려준다.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache


def _split(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    on_vercel: bool
    supabase_url: str | None
    supabase_jwt_secret: str | None
    jwt_audience: str
    cors_allow_origins: tuple[str, ...]
    dev_actor_id: str | None
    # 켜면 서버 시작 때 D 지식베이스(근거 검색·면책 조항 판정)를 연결한다(app/main.py).
    # 로컬 전용: 시작에 약 12초·230MB. 기본 꺼짐이고 Vercel에는 두지 않는다
    load_knowledge_base: bool = False
    # 서버 전용 Postgres 주소(Supabase Session pooler). 없으면 DB를 쓰는 경로는 503(app/db.py)
    database_url: str | None = None
    # 서버 전용 Supabase Storage 키(app/storage.py, apikey 헤더). RLS를 건너뛰므로 BE에만 둔다.
    # 없으면 Storage를 쓰는 경로는 503. repr에서 빼 설정 객체를 출력해도 값이 보이지 않게 한다
    supabase_secret_key: str | None = field(default=None, repr=False)
    # 영상 분석 Modal 호출(app/modal_dispatch.py). 넷이 다 있어야 분석 접수가 Modal을 부른다.
    # 프록시 인증 키·비밀은 repr에서 뺀다.
    # 콜백 주소는 PUBLIC_BACKEND_URL, 없으면 Vercel 운영 도메인
    modal_analyze_url: str | None = None
    modal_proxy_key: str | None = field(default=None, repr=False)
    modal_proxy_secret: str | None = field(default=None, repr=False)
    public_backend_url: str | None = None

    @property
    def jwks_url(self) -> str | None:
        if not self.supabase_url:
            return None
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def jwt_issuer(self) -> str | None:
        if not self.supabase_url:
            return None
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def dev_auth_enabled(self) -> bool:
        """로컬 우회는 APP_ENV=local + DEV_ACTOR_ID일 때만. Vercel 위에서는 어떤 값이어도 끈다."""
        return self.app_env == "local" and not self.on_vercel and bool(self.dev_actor_id)


def _public_backend_url(env: Mapping[str, str]) -> str | None:
    """Modal이 콜백할 BE 주소. PUBLIC_BACKEND_URL이 먼저고, 없으면 Vercel 운영 도메인."""
    explicit = env.get("PUBLIC_BACKEND_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    host = env.get("VERCEL_PROJECT_PRODUCTION_URL", "").strip().rstrip("/")
    return f"https://{host}" if host else None


@lru_cache
def get_settings() -> Settings:
    env = os.environ
    return Settings(
        app_env=env.get("APP_ENV", "production"),
        on_vercel=bool(env.get("VERCEL") or env.get("VERCEL_ENV")),
        supabase_url=env.get("SUPABASE_URL") or None,
        supabase_jwt_secret=env.get("SUPABASE_JWT_SECRET") or None,
        jwt_audience=env.get("SUPABASE_JWT_AUDIENCE", "authenticated"),
        cors_allow_origins=_split(env.get("CORS_ALLOW_ORIGINS", "")),
        dev_actor_id=env.get("DEV_ACTOR_ID") or None,
        load_knowledge_base=_flag(env.get("LOAD_KNOWLEDGE_BASE", "")),
        database_url=env.get("DATABASE_URL") or None,
        supabase_secret_key=env.get("SUPABASE_SECRET_KEY") or None,
        modal_analyze_url=env.get("MODAL_ANALYZE_URL") or None,
        modal_proxy_key=env.get("MODAL_PROXY_KEY") or None,
        modal_proxy_secret=env.get("MODAL_PROXY_SECRET") or None,
        public_backend_url=_public_backend_url(env),
    )
