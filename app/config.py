"""B(통합): 서버 환경 변수. 값은 `.env`(로컬)·Vercel 환경 변수에만 둔다. 양식은 `.env.example`.

인증은 기본으로 닫혀 있다(fail-closed). 검증 수단이 하나도 없으면 보호 경로는 503을 돌려준다.
"""

import os
from dataclasses import dataclass
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
    # 서버 전용 Postgres 주소(Supabase Session pooler). 없으면 DB를 쓰는 경로는 503(app/db.py)
    database_url: str | None = None
    # 켜면 서버 시작 때 D 지식베이스(근거 검색·면책 조항 판정)를 연결한다(app/main.py).
    # 로컬 전용: 시작에 약 12초·230MB. 기본 꺼짐이고 Vercel에는 두지 않는다
    load_knowledge_base: bool = False

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
        database_url=env.get("DATABASE_URL") or None,
        load_knowledge_base=_flag(env.get("LOAD_KNOWLEDGE_BASE", "")),
    )
