"""B(통합): 관리자(관제) 조회의 SQL을 실제 Postgres로 검사한다.

`MIGRATION_TEST_DATABASE_URL`(버려도 되는 로컬·CI Postgres)과 psql이 있을 때만 돈다.
- 담당자 목록(STAFF_SQL): auth.users의 app_metadata에서 역할·분야·지역을 읽고, 역할이 없거나
  정지·삭제된 계정은 뺀다. 지난 정지는 다시 넣는다
- 전체 사건: 담당자와 상관없이 모든 사건이 대물·대인 담당자와 함께 나온다
"""

import asyncio
import shutil
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app import db
from app.api import admin
from tests.api.test_case_assignees_db import INJURY, OTHER, PROPERTY, temp_database
from tests.migrations.test_migrations_apply import ADMIN_URL, MIGRATIONS

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or shutil.which("psql") is None,
    reason="MIGRATION_TEST_DATABASE_URL과 psql이 있어야 한다(supabase/migrations/README.md)",
)

ADMIN, BANNED, DELETED, RETURNED = (str(uuid.uuid4()) for _ in range(4))


def profile(conn, user_id: str, app_meta: dict, *, email: str | None = None,
            name: str | None = None, extra: str = "") -> None:
    """계정의 메타데이터를 채운다(없으면 만든다). extra는 banned_until·deleted_at 대입식."""
    conn.execute("insert into auth.users (id) values (%s) on conflict do nothing", (user_id,))
    conn.execute(
        "update auth.users set email = %s, raw_app_meta_data = %s, raw_user_meta_data = %s"
        + extra + " where id = %s",
        (email, Jsonb({"provider": "email", **app_meta}), Jsonb({"name": name} if name else {}),
         user_id),
    )


@pytest.fixture(scope="module")
def database_url():
    with temp_database(MIGRATIONS) as url:
        with psycopg.connect(url, autocommit=True) as conn:
            adjuster = {"staff_role": "adjuster"}
            profile(conn, PROPERTY, {**adjuster, "coverage": "property", "region": "서울"},
                    email="property@example.com", name="김대물")
            profile(conn, INJURY, {**adjuster, "coverage": "injury"}, email="injury@example.com")
            profile(conn, ADMIN, {"staff_role": "admin"}, email="admin@example.com")
            profile(conn, OTHER, {}, email="no-role@example.com")  # 역할 없음
            profile(conn, BANNED, adjuster, extra=", banned_until = now() + interval '1 day'")
            profile(conn, DELETED, adjuster, extra=", deleted_at = now()")
            profile(conn, RETURNED, adjuster, extra=", banned_until = now() - interval '1 day'")
        yield url


@pytest.fixture
def overview(database_url):
    old = admin.services
    admin.configure(connect=lambda: db.connect(database_url))
    yield lambda: asyncio.run(admin.get_overview(ADMIN))
    admin.services = old


def new_case(url: str, assignee: str, injury: str | None = None) -> str:
    case_id = str(uuid.uuid4())
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.cases (id, assignee_id, injury_assignee_id) values (%s, %s, %s)",
            (case_id, assignee, injury),
        )
    return case_id


def test_staff_come_from_auth_app_metadata(overview):
    staff = {m["user_id"]: m for m in overview()["staff"]}

    assert set(staff) == {PROPERTY, INJURY, ADMIN, RETURNED}  # 역할 없음·정지·삭제는 빠진다
    assert staff[PROPERTY] | {"cases": None} == {
        "user_id": PROPERTY, "email": "property@example.com", "name": "김대물",
        "staff_role": "adjuster", "coverage": "property", "region": "서울", "cases": None,
    }
    assert (staff[INJURY]["coverage"], staff[INJURY]["region"], staff[INJURY]["name"]) \
        == ("injury", None, None)
    assert (staff[ADMIN]["staff_role"], staff[ADMIN]["coverage"]) == ("admin", None)


def test_every_case_is_listed_with_both_assignees(database_url, overview):
    shared = new_case(database_url, PROPERTY, INJURY)
    alone = new_case(database_url, PROPERTY)
    stray = new_case(database_url, OTHER)  # 역할이 없는 계정의 사건

    out = overview()
    listed = {c["case_id"]: c for c in out["cases"]}
    assert {shared, alone, stray} <= set(listed)
    assert (listed[shared]["assignee_id"], listed[shared]["injury_assignee_id"]) \
        == (PROPERTY, INJURY)
    assert listed[alone]["injury_assignee_id"] is None and listed[alone]["stage"] == "intake"
    assert listed[stray]["assignee_id"] == OTHER
    # 최근 바뀐 순
    stamps = [c["updated_at"] for c in out["cases"]]
    assert stamps == sorted(stamps, reverse=True)

    staff = {m["user_id"]: m for m in out["staff"]}
    assert staff[PROPERTY]["cases"] == {"property": 2, "injury": 0, "open": 2}
    assert staff[INJURY]["cases"] == {"property": 0, "injury": 1, "open": 1}
    assert OTHER not in staff
