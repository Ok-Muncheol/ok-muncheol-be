"""B(통합): 서버 DB 연결 문자열 정리와 설정 없음 처리. 실제 DB 없이 돈다."""

import asyncio

import pytest

from app import db
from app.api.errors import ApiException
from app.config import get_settings


@pytest.mark.parametrize(("raw", "expected"), [
    # Supabase 화면의 주소에 비밀번호를 그대로 넣은 경우: 특수문자 인코딩 + SSL 강제
    ("postgresql://postgres.ref:p@ss/w#rd:1@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres",
     "postgresql://postgres.ref:p%40ss%2Fw%23rd%3A1@aws-0-ap-northeast-2.pooler.supabase.com:5432"
     "/postgres?sslmode=require"),
    # 이미 인코딩된 값은 그대로, 지정한 sslmode는 존중
    ("postgresql://u:p%40ss@db.ref.supabase.co:5432/postgres?sslmode=verify-full",
     "postgresql://u:p%40ss@db.ref.supabase.co:5432/postgres?sslmode=verify-full"),
    # 로컬 Postgres는 SSL을 붙이지 않는다
    ("postgresql://postgres:postgres@127.0.0.1:54329/postgres",
     "postgresql://postgres:postgres@127.0.0.1:54329/postgres"),
    ("postgres://localhost/db", "postgres://localhost/db"),
])
def test_normalize_database_url(raw, expected):
    assert db.normalize_database_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "mysql://u:p@h/db", "localhost:5432"])
def test_normalize_rejects_non_postgres(raw):
    with pytest.raises(ValueError):
        db.normalize_database_url(raw)


def test_missing_database_url_is_503(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()

    async def use() -> None:
        async with db.connect():
            pass

    try:
        with pytest.raises(ApiException) as caught:
            asyncio.run(use())
    finally:
        get_settings.cache_clear()
    assert (caught.value.status_code, caught.value.error["code"]) == (503, "db_not_configured")
