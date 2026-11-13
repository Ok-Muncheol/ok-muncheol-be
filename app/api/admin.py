"""B(통합): 관리자(관제) 조회 — `GET /admin/overview`.

관리자가 담당자별 배정 현황을 한 번에 본다. 담당자 목록(역할·분야·지역·맡은 사건 수)과
전체 사건 요약(대물·대인 담당자 포함)이다. 읽기만 한다. 접수·배정·이관은 다음 단계에서 더한다.

- 권한은 경로가 건다(deps.require_admin — 토큰 app_metadata.staff_role이 admin).
  서버 연결은 RLS를 건너뛰어 모든 사건이 보이므로, 이 모듈의 함수는 관리자 확인 뒤에만 부른다.
- 담당자의 원천은 Supabase Auth다. auth.users의 app_metadata(staff_role·coverage·region)를 읽고
  담당자 표를 따로 두지 않는다. 역할을 두 곳에 적으면 어긋난다.
- 사건 요약은 담당자 화면과 같은 SQL·같은 단계 판정을 쓴다(cases.SUMMARY_SQL·cases.summarize).
- 요청 하나에 DB 연결 하나, 쿼리 둘(담당자·사건)이다. 담당자별 사건 수는 그 결과로 센다.
  사건이 수천 건으로 늘면 쪽 나눔과 SQL 집계로 바꾼다.
"""

import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app import db
from app.api import cases
from app.schemas.admin import AdminOverview, Coverage, StaffCaseCounts, StaffMember, StaffRole

STAFF_ROLES: tuple[str, ...] = typing.get_args(StaffRole)
COVERAGES: tuple[str, ...] = typing.get_args(Coverage)

# 역할이 있는 계정. 정지(banned_until이 아직 남음)·삭제된 계정은 뺀다.
STAFF_SQL = """
select u.id, u.email,
       u.raw_user_meta_data ->> 'name' as name,
       u.raw_app_meta_data ->> 'staff_role' as staff_role,
       u.raw_app_meta_data ->> 'coverage' as coverage,
       u.raw_app_meta_data ->> 'region' as region
from auth.users u
where u.raw_app_meta_data ->> 'staff_role' = any(%s)
  and u.deleted_at is null
  and (u.banned_until is null or u.banned_until < now())
order by u.created_at, u.id
"""


class PostgresAdminRepo:
    """auth.users(담당자)와 public.cases(전체 사건 요약). 연결 하나로 한 요청을 처리한다."""

    def __init__(self, conn: Any):
        self.conn = conn

    async def staff(self) -> list[dict]:
        cur = await self.conn.execute(STAFF_SQL, (list(STAFF_ROLES),))
        return list(await cur.fetchall())

    async def case_rows(self) -> list[dict]:
        cur = await self.conn.execute(cases.SUMMARY_SQL + " order by c.updated_at desc")
        return list(await cur.fetchall())


@dataclass
class AdminServices:
    """테스트에서 바꿔 끼울 수 있게 DB 연결·저장소를 모은다(cases.py와 같은 방식)."""

    connect: Callable = db.connect
    repo: Callable[[Any], PostgresAdminRepo] = PostgresAdminRepo


services = AdminServices()


def configure(**kw: object) -> AdminServices:
    global services
    services = AdminServices(**{**services.__dict__, **kw})  # type: ignore[arg-type]
    return services


def is_open(summary: dict[str, object]) -> bool:
    """확인 완료(보고서 승인) 전이면 진행 중이다."""
    return not (summary["stage"] == "report" and summary["approval_status"] == "approved")


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _no_cases() -> dict[str, int]:
    return {"property": 0, "injury": 0, "open": 0}


async def get_overview(admin_id: str) -> AdminOverview:
    """담당자 목록과 전체 사건의 배정 현황. 관리자 확인(require_admin) 뒤에만 부른다."""
    async with services.connect() as conn:
        repo = services.repo(conn)
        staff_rows = await repo.staff()
        case_rows = await repo.case_rows()

    counts: dict[str, dict[str, int]] = {}
    listed: list[dict[str, object]] = []
    for row in case_rows:
        summary = cases.summarize(row)
        property_id = str(row["assignee_id"])
        injury_id = str(row["injury_assignee_id"]) if row["injury_assignee_id"] else None
        listed.append({**summary, "assignee_id": property_id, "injury_assignee_id": injury_id})
        for line, user_id in (("property", property_id), ("injury", injury_id)):
            if user_id is not None:
                counts.setdefault(user_id, _no_cases())[line] += 1
        if is_open(summary):
            # 한 사람이 두 역할을 겸한 사건은 한 번만 센다
            for user_id in {property_id, injury_id} - {None}:
                counts[user_id]["open"] += 1  # type: ignore[index]

    staff: list[StaffMember] = []
    for row in staff_rows:
        user_id = str(row["id"])
        mine = counts.get(user_id, _no_cases())
        staff.append({
            "user_id": user_id,
            "email": _text(row["email"]),
            "name": _text(row["name"]),
            "staff_role": row["staff_role"],
            "coverage": row["coverage"] if row["coverage"] in COVERAGES else None,
            "region": _text(row["region"]),
            "cases": StaffCaseCounts(
                property=mine["property"], injury=mine["injury"], open=mine["open"]
            ),
        })
    return {"staff": staff, "cases": listed}
