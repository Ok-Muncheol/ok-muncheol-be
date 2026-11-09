"""B(통합): supabase/migrations/*.sql을 빈 Postgres에 적용하고 RLS·제약을 검사한다.

`MIGRATION_TEST_DATABASE_URL`에 버려도 되는 Postgres의 관리자 주소를 주면 돈다. 없으면 건너뛴다.
매번 임시 데이터베이스를 만들어 Supabase 흉내(supabase_stub.sql),
마이그레이션(파일명 순, 파일마다 한 트랜잭션), 동작 검사(behavior.sql) 순으로 적용하고 지운다.
실행 방법은 supabase/migrations/README.md 「로컬 검사」.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
ADMIN_URL = os.environ.get("MIGRATION_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or shutil.which("psql") is None,
    reason="MIGRATION_TEST_DATABASE_URL과 psql이 있어야 한다(supabase/migrations/README.md)",
)


def _psql(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", url, *args],
        capture_output=True, text=True, timeout=120, check=False,
    )


@pytest.fixture
def database_url():
    parts = urlsplit(ADMIN_URL)
    if "supabase" in (parts.hostname or ""):
        pytest.fail("버려도 되는 로컬 Postgres 주소만 쓴다(Supabase 주소 금지)")
    name = f"okm_migration_{uuid.uuid4().hex[:12]}"
    created = _psql(ADMIN_URL, "-c", f'create database "{name}"')
    assert created.returncode == 0, created.stderr
    try:
        yield urlunsplit(parts._replace(path=f"/{name}"))
    finally:
        _psql(ADMIN_URL, "-c", f'drop database if exists "{name}" with (force)')


def test_migrations_apply_and_enforce_contract(database_url):
    assert MIGRATIONS, "supabase/migrations/*.sql이 없다"
    stub = _psql(database_url, "-f", str(HERE / "supabase_stub.sql"))
    assert stub.returncode == 0, stub.stderr
    for migration in MIGRATIONS:
        # Supabase CLI처럼 파일마다 한 트랜잭션으로 적용한다
        applied = _psql(database_url, "--single-transaction", "-f", str(migration))
        assert applied.returncode == 0, f"{migration.name}\n{applied.stderr}"
    behavior = _psql(database_url, "-f", str(HERE / "behavior.sql"))
    assert behavior.returncode == 0, behavior.stderr
    assert "MIGRATION_BEHAVIOR_OK" in behavior.stdout
