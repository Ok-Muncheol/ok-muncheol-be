"""B(통합): 관리자(관제) 조회 — app/api/admin.py get_overview.

DB 없이 메모리 저장소(admin.configure)로 확인한다. SQL은 tests/api/test_admin_db.py가 본다.
관리자만 부를 수 있는지는 경로가 건다(tests/api/test_app.py).
- 담당자: 역할·분야·지역이 그대로 나오고, 모르는 분야 값과 빈 글자는 null
- 사건 수: 대물·대인으로 맡은 수와 진행 중(확인 완료 전) 수.
  두 역할을 겸한 사건은 진행 중을 한 번만 센다
- 사건: 담당자 화면과 같은 요약에 대물·대인 담당자 ID가 붙고 순서는 저장소가 준 그대로다
"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.api import admin
from tests.api.test_cases import row

PROPERTY = "11111111-1111-1111-1111-111111111111"
INJURY = "55555555-5555-5555-5555-555555555555"
ADMIN = "99999999-9999-9999-9999-999999999999"
GHOST = "77777777-7777-7777-7777-777777777777"  # 역할이 없는 계정


def run(coro):
    return asyncio.run(coro)


def member(user_id: str, **kw) -> dict:
    """STAFF_SQL 한 행."""
    base = {"id": user_id, "email": f"{user_id[:4]}@example.com", "name": None,
            "staff_role": "adjuster", "coverage": "property", "region": None}
    return {**base, **kw}


def case(case_id: str, **kw) -> dict:
    """SUMMARY_SQL 한 행. 기본은 대물 담당자 PROPERTY의 접수 단계 사건."""
    return row(**{"id": case_id, "assignee_id": PROPERTY, **kw})


def approved(case_id: str, **kw) -> dict:
    """확인 완료(보고서 승인)까지 끝난 사건."""
    return case(case_id, card_version=3, run_card_version=3, judgment_version=1,
                run_status="completed", judgment_status="draft_ready", report_version=1,
                report_judgment_version=1, approval_status="approved", **kw)


class MemoryAdminRepo:
    def __init__(self, staff: list[dict], cases: list[dict]):
        self._staff, self._cases = staff, cases

    async def staff(self):
        return list(self._staff)

    async def case_rows(self):
        return list(self._cases)


@pytest.fixture
def overview():
    """(담당자 행, 사건 행) → get_overview 결과."""
    old = admin.services

    @asynccontextmanager
    async def connect():
        yield None

    def call(staff: list[dict], cases: list[dict]):
        admin.configure(connect=connect, repo=lambda conn: MemoryAdminRepo(staff, cases))
        return run(admin.get_overview(ADMIN))

    yield call
    admin.services = old


def by_id(result) -> dict[str, dict]:
    return {m["user_id"]: m for m in result["staff"]}


def test_staff_carry_role_coverage_region_and_counts(overview):
    staff = [
        member(PROPERTY, name="김대물", region="서울"),
        member(INJURY, coverage="injury", region="서울"),
        member(ADMIN, staff_role="admin", coverage=None),
    ]
    cases = [
        case("c1", injury_assignee_id=INJURY),  # 진행 중, 두 담당자
        case("c2"),  # 진행 중, 대물만
        approved("c3", injury_assignee_id=INJURY),  # 확인 완료
    ]
    out = by_id(overview(staff, cases))

    assert out[PROPERTY] == {
        "user_id": PROPERTY, "email": "1111@example.com", "name": "김대물",
        "staff_role": "adjuster", "coverage": "property", "region": "서울",
        "cases": {"property": 3, "injury": 0, "open": 2},
    }
    assert out[INJURY]["coverage"] == "injury"
    assert out[INJURY]["cases"] == {"property": 0, "injury": 2, "open": 1}
    assert out[ADMIN]["staff_role"] == "admin" and out[ADMIN]["coverage"] is None
    assert out[ADMIN]["cases"] == {"property": 0, "injury": 0, "open": 0}


def test_one_person_holding_both_roles_counts_the_open_case_once(overview):
    out = by_id(overview([member(PROPERTY)], [case("c1", injury_assignee_id=PROPERTY)]))
    assert out[PROPERTY]["cases"] == {"property": 1, "injury": 1, "open": 1}


def test_unknown_coverage_and_blank_text_become_null(overview):
    staff = [member(PROPERTY, coverage="life", name="  ", region="", email=None)]
    only = overview(staff, [])["staff"][0]
    assert (only["coverage"], only["name"], only["region"], only["email"]) == (None,) * 4


def test_cases_keep_summary_order_and_assignees(overview):
    cases = [approved("c3", injury_assignee_id=INJURY), case("c2"), case("c1", assignee_id=GHOST)]
    listed = overview([member(PROPERTY), member(INJURY, coverage="injury")], cases)["cases"]

    assert [c["case_id"] for c in listed] == ["c3", "c2", "c1"]
    assert (listed[0]["stage"], listed[0]["approval_status"]) == ("report", "approved")
    assert (listed[0]["assignee_id"], listed[0]["injury_assignee_id"]) == (PROPERTY, INJURY)
    assert (listed[1]["stage"], listed[1]["injury_assignee_id"]) == ("intake", None)
    # 역할이 없는 계정이 맡은 사건도 빠지지 않는다(담당자 목록에는 없다)
    assert listed[2]["assignee_id"] == GHOST


def test_is_open_until_the_report_is_approved():
    assert admin.is_open({"stage": "intake", "approval_status": None})
    assert admin.is_open({"stage": "report", "approval_status": "draft"})
    assert not admin.is_open({"stage": "report", "approval_status": "approved"})
