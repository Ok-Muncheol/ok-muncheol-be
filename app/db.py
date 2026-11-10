"""B(통합): 서버 전용 Postgres 연결.

공용 Supabase DB에는 Session pooler 주소(`DATABASE_URL`)로 붙는다.

- 쓰기는 BE만 한다. 브라우저는 RLS를 거친 조회·Realtime만 쓴다(supabase/migrations/README.md).
- LangGraph 체크포인터(`AsyncPostgresSaver`)와 같은 연결을 쓴다.
  라이브러리 요구대로 autocommit·dict_row이고,
  풀러와 맞도록 서버 쪽 준비 문(prepared statement)을 쓰지 않는다.
- `search_path`는 `langgraph, public`이다. 체크포인트 테이블은 이름에 스키마를 붙이지 않으므로
  langgraph에 가고, 판단 테이블 SQL은 `public.`을 붙여 쓴다.
  세션 설정이라 Transaction pooler(6543)에서는 유지되지 않는다.
- 주소·비밀번호는 로그·응답에 싣지 않는다. 연결 실패는 503 `db_unavailable`로만 알린다.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote

import psycopg
from psycopg.rows import dict_row

from app.api.errors import ApiException
from app.config import get_settings

SEARCH_PATH = "langgraph, public"


def normalize_database_url(raw: str) -> str:
    """비밀번호의 특수문자를 인코딩하고 Supabase 주소면 SSL을 강제한다.

    Supabase 화면의 연결 문자열에 비밀번호를 그대로 넣어도 되게 한다.
    이미 인코딩된 값은 그대로 둔다.
    """
    scheme, sep, rest = raw.strip().partition("://")
    if not sep or not scheme.startswith("postgres"):
        raise ValueError("DATABASE_URL은 postgresql://로 시작해야 한다")
    userinfo, at, hostpart = rest.rpartition("@")
    if at:
        user, colon, password = userinfo.partition(":")
        userinfo = quote(unquote(user), safe="")
        if colon:
            userinfo += ":" + quote(unquote(password), safe="")
        rest = f"{userinfo}@{hostpart}"
    host = hostpart.split("/", 1)[0].split("?", 1)[0].rsplit(":", 1)[0]
    url = f"{scheme}://{rest}"
    if host.endswith((".supabase.co", ".supabase.com")) and "sslmode=" not in hostpart:
        url += ("&" if "?" in hostpart else "?") + "sslmode=require"
    return url


@asynccontextmanager
async def connect(database_url: str | None = None) -> AsyncIterator[psycopg.AsyncConnection]:
    """요청마다 연결 하나. 주소를 넘기지 않으면 설정(`DATABASE_URL`)을 쓴다."""
    url = database_url or get_settings().database_url
    if not url:
        raise ApiException(503, "db_not_configured", "서버 DB 설정이 없습니다.")
    try:
        conn = await psycopg.AsyncConnection.connect(
            normalize_database_url(url), autocommit=True, row_factory=dict_row,
            prepare_threshold=None, connect_timeout=10,
        )
    except psycopg.OperationalError as exc:
        raise ApiException(503, "db_unavailable", "DB에 연결하지 못했습니다.", True) from exc
    async with conn:
        await conn.execute(f"set search_path = {SEARCH_PATH}")
        yield conn
