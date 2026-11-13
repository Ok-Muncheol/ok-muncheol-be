"""B(통합): 사건의 두 담당자(대물·대인) 판정과 사건 목록 SQL을 실제 Postgres로 검사한다.

`MIGRATION_TEST_DATABASE_URL`(버려도 되는 로컬·CI Postgres)과 psql이 있을 때만 돈다.
메모리 저장소 테스트(tests/api/test_cases.py)가 보지 못하는 SQL을 본다.
- runs.case_assignees: 대물 담당자, 있으면 대인 담당자. 사건이 없으면 None
- SUMMARY_SQL의 담당자 조건: 대물·대인 담당자 모두 자기 목록에서 사건을 보고, 다른 사람은 못 본다
브라우저 쪽 같은 판정(RLS private.can_access_case)은 tests/migrations/behavior.sql 11절이 본다.
"""

import asyncio
import shutil
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from app import db
from app.api import cases
from app.api.errors import ApiException
from app.workflow import runs
from tests.migrations.test_migrations_apply import ADMIN_URL, HERE, MIGRATIONS, _psql

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or shutil.which("psql") is None,
    reason="MIGRATION_TEST_DATABASE_URL과 psql이 있어야 한다(supabase/migrations/README.md)",
)

PROPERTY, INJURY, OTHER = (str(uuid.uuid4()) for _ in range(3))


@contextmanager
def temp_database(migrations):
    """임시 데이터베이스에 Supabase 흉내와 주어진 마이그레이션을 적용하고, 끝나면 지운다."""
    parts = urlsplit(ADMIN_URL)
    if "supabase" in (parts.hostname or ""):
        pytest.fail("버려도 되는 로컬 Postgres 주소만 쓴다(Supabase 주소 금지)")
    name = f"okm_assignees_{uuid.uuid4().hex[:12]}"
    created = _psql(ADMIN_URL, "-c", f'create database "{name}"')
    assert created.returncode == 0, created.stderr
    url = urlunsplit(parts._replace(path=f"/{name}"))
    try:
        assert _psql(url, "-f", str(HERE / "supabase_stub.sql")).returncode == 0
        for migration in migrations:
            applied = _psql(url, "--single-transaction", "-f", str(migration))
            assert applied.returncode == 0, f"{migration.name}\n{applied.stderr}"
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "insert into auth.users (id) values (%s), (%s), (%s)", (PROPERTY, INJURY, OTHER)
            )
        yield url
    finally:
        _psql(ADMIN_URL, "-c", f'drop database if exists "{name}" with (force)')


@contextmanager
def cases_on(url: str):
    """사건 서비스가 그 DB에 붙게 한다."""
    old = cases.services
    cases.configure(connect=lambda: db.connect(url))
    try:
        yield url
    finally:
        cases.services = old


@pytest.fixture(scope="module")
def database_url():
    with temp_database(MIGRATIONS) as url:
        yield url


@pytest.fixture
def service(database_url):
    with cases_on(database_url) as url:
        yield url


def run(coro):
    return asyncio.run(coro)


def new_case(url: str, injury: str | None = None) -> str:
    case_id = str(uuid.uuid4())
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.cases (id, assignee_id, injury_assignee_id) values (%s, %s, %s)",
            (case_id, PROPERTY, injury),
        )
    return case_id


def assignees(url: str, case_id: str) -> tuple[str, ...] | None:
    async def read():
        async with db.connect(url) as conn:
            return await runs.case_assignees(conn, case_id)

    return run(read())


def listed(actor: str) -> set[str]:
    return {str(c["case_id"]) for c in run(cases.list_cases(actor))}


def test_case_assignees_is_property_then_injury(service):
    alone = new_case(service)
    shared = new_case(service, injury=INJURY)
    assert assignees(service, alone) == (PROPERTY,)
    assert assignees(service, shared) == (PROPERTY, INJURY)
    assert assignees(service, str(uuid.uuid4())) is None


def test_both_assignees_list_and_read_the_case(service):
    alone = new_case(service)
    shared = new_case(service, injury=INJURY)

    assert {alone, shared} <= listed(PROPERTY)
    assert shared in listed(INJURY) and alone not in listed(INJURY)
    assert not {alone, shared} & listed(OTHER)

    assert run(cases.get_case(INJURY, shared))["case_id"] == shared
    assert run(cases.get_case(PROPERTY, shared))["case_id"] == shared
    for actor, case_id in ((INJURY, alone), (OTHER, shared)):
        with pytest.raises(ApiException) as caught:
            run(cases.get_case(actor, case_id))
        assert (caught.value.status_code, caught.value.error["code"]) == (403, "forbidden")


def test_transfer_moves_access_with_the_columns(service):
    """이관은 담당자 칼럼을 바꾸는 일이다. 바꾸면 이전 담당자는 바로 막히고 새 담당자가 본다."""
    case_id = new_case(service, injury=INJURY)
    with psycopg.connect(service, autocommit=True) as conn:
        conn.execute(
            "update public.cases set assignee_id = %s, injury_assignee_id = null where id = %s",
            (OTHER, case_id),
        )
    assert assignees(service, case_id) == (OTHER,)
    assert case_id in listed(OTHER)
    assert case_id not in listed(PROPERTY) and case_id not in listed(INJURY)
    for actor in (PROPERTY, INJURY):
        with pytest.raises(ApiException) as caught:
            run(cases.get_case(actor, case_id))
        assert caught.value.status_code == 403

