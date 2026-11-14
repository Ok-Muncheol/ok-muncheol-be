"""B(통합): 접수·배정의 SQL. 연결 하나를 받아 쓰고, 트랜잭션 경계는 서비스(service.py)가 정한다.

시각은 모두 호출하는 쪽이 넘긴다(`now`). 가상 접수를 미리 만들 때는 그 접수가 들어오는 시각을
넘겨서, 그 시각 기준의 업무량으로 후보를 본다.
"""

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from app.dispatch.simulation import (
    CLOSED_SQL,
    OPEN_SQL,
    STAGE_SQL,
    VISIBLE_SQL,
    WAITING_SQL,
)

Row = dict[str, Any]

ASSIGNEE_COLUMNS = {"property": "property_assignee_id", "injury": "injury_assignee_id"}

# 자동 배정 후보: 그 지역·분야의 담당자 가운데 자동 배정에서 빼지 않은 계정. 업무량이 적은 순.
_CANDIDATES_SQL = """
select u.id,
       coalesce(nullif(u.raw_user_meta_data ->> 'name', ''), u.email) as name,
       (select count(*) from public.case_intakes i
         where i.{column} = u.id and {visible} and {open}) as open_cases
from auth.users u
where u.raw_app_meta_data ->> 'staff_role' = 'adjuster'
  and u.raw_app_meta_data ->> 'coverage' = %(coverage)s
  and u.raw_app_meta_data ->> 'region' = %(region)s
  and coalesce(u.raw_app_meta_data ->> 'auto_assign', 'true') <> 'false'
  and u.deleted_at is null
  and (u.banned_until is null or u.banned_until < now())
order by open_cases, u.id
"""

_INSERT_INTAKE_SQL = """
insert into public.case_intakes
  (received_at, accident_at, location, description, injured_count, registered_by, simulated,
   facts_at, judgment_at, report_at, closes_at)
values (%(received_at)s, %(accident_at)s, %(location)s, %(description)s, %(injured_count)s,
        %(registered_by)s, %(simulated)s,
        %(facts_at)s, %(judgment_at)s, %(report_at)s, %(closes_at)s)
returning *
"""

# 실제 사건(cases)과 접수 건을 맞춘다. 관리자 조회가 읽기 전에 부른다.
#   added  — 접수 없이 생긴 사건(담당자가 직접 만든 것)에 접수 건을 만든다
#   synced — 사건의 담당자가 접수 건의 사본과 다르면 사건 쪽 값으로 맞춘다(권한의 기준은 사건이다)
#   closed — 최신 보고서가 확인 완료(approved)된 사건의 접수 건을 종결로 바꾼다
_SYNC_SQL = """
with added as (
  insert into public.case_intakes
    (received_at, accident_at, location, description, status,
     property_assignee_id, injury_assignee_id, case_id)
  select c.created_at, c.accident_date, '기록 없음', '접수 없이 담당자가 직접 만든 사건',
         'assigned', c.assignee_id, c.injury_assignee_id, c.id
  from public.cases c
  where not exists (select 1 from public.case_intakes i where i.case_id = c.id)
  on conflict (case_id) do nothing
  returning 1
), synced as (
  update public.case_intakes i
     set property_assignee_id = c.assignee_id, injury_assignee_id = c.injury_assignee_id
    from public.cases c
   where i.case_id = c.id and i.status in ('assigned', 'closed')
     and (i.property_assignee_id, i.injury_assignee_id)
         is distinct from (c.assignee_id, c.injury_assignee_id)
  returning 1
), closed as (
  update public.case_intakes i
     set status = 'closed', closed_at = latest.updated_at
    from public.cases c
    cross join lateral (
      select approval_status, updated_at from public.review_reports r
      where r.case_id = c.id order by r.report_version desc limit 1) latest
   where i.case_id = c.id and i.status = 'assigned' and latest.approval_status = 'approved'
  returning 1
)
select (select count(*) from added) as added, (select count(*) from synced) as synced,
       (select count(*) from closed) as closed
"""

_LIST_SQL = """
select i.*, a.method as assignment_method, a.reason as assignment_reason,
       a.created_at as assignment_at,
       case when i.simulated then {stage} end as simulated_stage
from public.case_intakes i
left join lateral (
  select method, reason, created_at from public.case_assignments ca
  where ca.intake_id = i.id and ca.coverage = 'property'
  order by ca.created_at desc limit 1) a on true
where {where}
order by i.received_at desc, i.id
limit %(limit)s
"""

_SUMMARY_SQL = """
select
  count(*) filter (where {open}) as open,
  count(*) filter (where {waiting}) as unassigned,
  count(*) filter (where i.received_at >= %(day_start)s) as received_today,
  count(*) filter (where
    (i.status = 'closed' and i.closed_at >= %(day_start)s)
    or (i.simulated and i.status = 'assigned'
        and i.closes_at >= %(day_start)s and i.closes_at <= %(now)s)) as closed_today
from public.case_intakes i
where {visible}
"""

_SIMULATED_STAGES_SQL = """
select {stage} as stage, count(*) as n
from public.case_intakes i
where {visible} and i.simulated and {open}
group by 1
"""

_OPEN_REAL_CASES_SQL = """
select i.case_id from public.case_intakes i
where {visible} and not i.simulated and {open} and i.case_id is not null
"""

# 담당자 목록과 업무량. 접수 건을 한 번만 훑어 담당자별로 센다
_STAFF_SQL = """
with held as (
  select i.property_assignee_id as assignee, {open} as is_open, {closed} as is_closed
  from public.case_intakes i
  where {visible} and i.property_assignee_id is not null
  union all
  select i.injury_assignee_id, {open}, {closed}
  from public.case_intakes i
  where {visible} and i.injury_assignee_id is not null
    and i.injury_assignee_id is distinct from i.property_assignee_id
), load as (
  select assignee, count(*) filter (where is_open) as open_cases,
         count(*) filter (where is_closed) as closed_cases
  from held group by assignee
)
select u.id, u.email,
       u.raw_user_meta_data ->> 'name' as name,
       u.raw_app_meta_data ->> 'staff_role' as staff_role,
       u.raw_app_meta_data ->> 'coverage' as coverage,
       u.raw_app_meta_data ->> 'region' as region,
       coalesce(u.raw_app_meta_data ->> 'auto_assign', 'true') <> 'false' as auto_assign,
       coalesce(l.open_cases, 0) as open_cases, coalesce(l.closed_cases, 0) as closed_cases
from auth.users u
left join load l on l.assignee = u.id
where u.raw_app_meta_data ->> 'staff_role' = any(%(roles)s)
  and u.deleted_at is null
  and (u.banned_until is null or u.banned_until < now())
order by u.created_at, u.id
"""


def _fill(sql: str, **extra: str) -> str:
    return sql.format(visible=VISIBLE_SQL, open=OPEN_SQL, closed=CLOSED_SQL, stage=STAGE_SQL,
                      waiting=WAITING_SQL, **extra)


async def _all(conn: AsyncConnection, sql: str, params: dict) -> list[Row]:
    cur = await conn.execute(sql, params)
    return list(await cur.fetchall())  # type: ignore[arg-type]


async def _one(conn: AsyncConnection, sql: str, params: dict) -> Row:
    cur = await conn.execute(sql, params)
    return await cur.fetchone()  # type: ignore[return-value]


async def candidates(
    conn: AsyncConnection, region: str, coverage: str, now: datetime
) -> list[Row]:
    """그 지역·분야의 자동 배정 후보와 `now` 기준 진행 중 건수(적은 순)."""
    sql = _fill(_CANDIDATES_SQL, column=ASSIGNEE_COLUMNS[coverage])
    return await _all(conn, sql, {"region": region, "coverage": coverage, "now": now})


async def insert_intake(conn: AsyncConnection, values: dict) -> Row:
    return await _one(conn, _INSERT_INTAKE_SQL, values)


async def mark_unassigned(
    conn: AsyncConnection, intake_id: object, region: str | None, reason: str
) -> Row:
    return await _one(
        conn,
        "update public.case_intakes set status = 'unassigned', region = %(region)s,"
        " unassigned_reason = %(reason)s where id = %(id)s returning *",
        {"id": intake_id, "region": region, "reason": reason},
    )


async def mark_assigned(
    conn: AsyncConnection, intake_id: object, *, region: str, property_id: str,
    injury_id: str | None, case_id: object | None, note: str | None,
) -> Row:
    """대물 담당자가 정해졌다. note는 대인 담당자를 구하지 못한 까닭(있으면)."""
    return await _one(
        conn,
        "update public.case_intakes set status = 'assigned', region = %(region)s,"
        " property_assignee_id = %(property_id)s, injury_assignee_id = %(injury_id)s,"
        " case_id = %(case_id)s, unassigned_reason = %(note)s where id = %(id)s returning *",
        {"id": intake_id, "region": region, "property_id": property_id, "injury_id": injury_id,
         "case_id": case_id, "note": note},
    )


async def insert_case(
    conn: AsyncConnection, property_id: str, injury_id: str | None, accident_at: datetime | None
) -> object:
    """실제 접수가 배정되면 담당자 화면이 읽는 사건을 만든다(영상 분석으로 시작하는 사건)."""
    row = await _one(
        conn,
        "insert into public.cases (assignee_id, injury_assignee_id, accident_date, input_path)"
        " values (%(property_id)s, %(injury_id)s, %(accident_at)s, 'video') returning id",
        {"property_id": property_id, "injury_id": injury_id, "accident_at": accident_at},
    )
    return row["id"]


async def insert_assignment(
    conn: AsyncConnection, *, intake_id: object, coverage: str, to_assignee_id: str,
    method: str, reason: str, detail: dict | None, created_at: datetime,
    from_assignee_id: str | None = None, decided_by: str | None = None,
) -> None:
    await conn.execute(
        "insert into public.case_assignments (intake_id, coverage, from_assignee_id,"
        " to_assignee_id, method, reason, decided_by, detail, created_at)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (intake_id, coverage, from_assignee_id, to_assignee_id, method, reason, decided_by,
         Jsonb(detail) if detail is not None else None, created_at),
    )


async def sync_real_cases(conn: AsyncConnection) -> Row:
    """실제 사건과 접수 건을 맞춘다(접수 건 추가·담당자 사본·확인 완료 종결).

    여러 번 불러도 된다.
    """
    return await _one(conn, _SYNC_SQL, {})


async def list_intakes(
    conn: AsyncConnection, *, now: datetime, limit: int, state: str | None = None,
    assignee_id: str | None = None, region: str | None = None, before: datetime | None = None,
) -> list[Row]:
    """접수 시각이 온 접수 건(최근 접수 순). state: open·unassigned·closed."""
    where = [VISIBLE_SQL]
    if state == "open":
        where.append(OPEN_SQL)
    elif state == "unassigned":
        where.append(WAITING_SQL)
    elif state == "closed":
        where.append(CLOSED_SQL)
    if assignee_id is not None:
        where.append("%(assignee)s in (i.property_assignee_id, i.injury_assignee_id)")
    if region is not None:
        where.append("i.region = %(region)s")
    if before is not None:
        where.append("i.received_at < %(before)s")
    sql = _LIST_SQL.format(stage=STAGE_SQL, where=" and ".join(where))
    params = {"now": now, "limit": limit, "assignee": assignee_id, "region": region,
              "before": before}
    return await _all(conn, sql, params)


async def get_intake(conn: AsyncConnection, intake_id: object, now: datetime) -> Row | None:
    sql = _LIST_SQL.format(stage=STAGE_SQL, where="i.id = %(id)s")
    rows = await _all(conn, sql, {"now": now, "limit": 1, "id": intake_id})
    return rows[0] if rows else None


async def summary(conn: AsyncConnection, now: datetime, day_start: datetime) -> Row:
    return await _one(conn, _fill(_SUMMARY_SQL), {"now": now, "day_start": day_start})


async def simulated_stage_counts(conn: AsyncConnection, now: datetime) -> dict[str, int]:
    rows = await _all(conn, _fill(_SIMULATED_STAGES_SQL), {"now": now})
    return {row["stage"]: int(row["n"]) for row in rows}


async def open_real_case_ids(conn: AsyncConnection, now: datetime) -> list[object]:
    rows = await _all(conn, _fill(_OPEN_REAL_CASES_SQL), {"now": now})
    return [row["case_id"] for row in rows]


async def staff(conn: AsyncConnection, roles: list[str], now: datetime) -> list[Row]:
    return await _all(conn, _fill(_STAFF_SQL), {"roles": roles, "now": now})


async def lock_intake(conn: AsyncConnection, intake_id: object) -> Row | None:
    """접수 건을 잠그고 읽는다(직접 배정·이관이 겹치지 않게). 트랜잭션 안에서 부른다."""
    cur = await conn.execute(
        "select * from public.case_intakes where id = %s for update", (intake_id,)
    )
    return await cur.fetchone()  # type: ignore[return-value]


async def adjuster(conn: AsyncConnection, user_id: object) -> Row | None:
    """담당자 계정 하나(분야·지역). 담당자가 아니거나 정지·삭제된 계정이면 None."""
    cur = await conn.execute(
        "select u.id, u.raw_app_meta_data ->> 'coverage' as coverage,"
        " u.raw_app_meta_data ->> 'region' as region from auth.users u"
        " where u.id = %s and u.raw_app_meta_data ->> 'staff_role' = 'adjuster'"
        " and u.deleted_at is null and (u.banned_until is null or u.banned_until < now())",
        (user_id,),
    )
    return await cur.fetchone()  # type: ignore[return-value]


async def set_assignee(
    conn: AsyncConnection, intake: Row, coverage: str, assignee_id: str
) -> None:
    """이미 배정된 접수 건의 한 분야 담당자를 바꾼다. 실제 접수면 사건의 담당자도 함께 바꾼다."""
    column = ASSIGNEE_COLUMNS[coverage]
    note = ", unassigned_reason = null" if coverage == "injury" else ""
    await conn.execute(
        f"update public.case_intakes set {column} = %s{note} where id = %s",
        (assignee_id, intake["id"]),
    )
    if intake["case_id"] is not None:
        case_column = "assignee_id" if coverage == "property" else "injury_assignee_id"
        await conn.execute(
            f"update public.cases set {case_column} = %s where id = %s",
            (assignee_id, intake["case_id"]),
        )


async def assignments(conn: AsyncConnection, intake_id: object) -> list[Row]:
    """접수 건의 배정 이력(오래된 것부터)."""
    cur = await conn.execute(
        "select coverage, from_assignee_id, to_assignee_id, method, reason, decided_by, detail,"
        " created_at from public.case_assignments where intake_id = %s order by created_at, id",
        (intake_id,),
    )
    return list(await cur.fetchall())  # type: ignore[arg-type]


async def count_simulated(conn: AsyncConnection) -> int:
    cur = await conn.execute("select count(*) as n from public.case_intakes where simulated")
    return int((await cur.fetchone())["n"])  # type: ignore[index]


async def delete_simulated(conn: AsyncConnection) -> int:
    """가상 접수를 모두 지운다(배정 이력은 함께 지워진다). 실제 접수는 건드리지 않는다."""
    cur = await conn.execute("delete from public.case_intakes where simulated")
    return cur.rowcount
