"""B(통합): 관리자(관제) API — 현황(`GET /admin/overview`)과 접수 건(`/admin/intakes`).

관리자가 접수를 등록하면 배정 에이전트가 바로 배정하고(app/agents/dispatch.py — 못 정하면 규칙),
담당자별 업무량과 접수 건의 진행 단계를 본다. 필요하면 관리자가 직접 배정하거나 이관한다.

- 권한은 경로가 건다(deps.require_admin — 토큰 app_metadata.staff_role이 admin).
  서버 연결은 RLS를 건너뛰어 모든 접수 건이 보이므로, 이 모듈의 함수는 관리자 확인 뒤에만 부른다.
- 담당자의 원천은 Supabase Auth다. auth.users의 app_metadata를 읽고 담당자 표를 따로 두지 않는다.
- 접수 건의 단계: 실제 접수는 담당자 화면과 같은 판정(cases.SUMMARY_SQL·cases.summarize)을 쓰고,
  가상 접수는 배정할 때 정해 둔 일정과 지금 시각을 비교한다(app/dispatch/simulation.py).
  접수 시각이 아직 오지 않은 가상 접수(미리 만들어 둔 것)는 어디에도 보이지 않는다.
- 조회하기 전에 실제 사건과 접수 건을 맞춘다(repository.sync_real_cases) — 접수 없이 생긴 사건,
  이 모듈 밖에서 바뀐 담당자, 확인 완료된 사건의 종결.
- 접수·배정 표가 아직 없으면(마이그레이션 적용 전) 503 `dispatch_db_not_ready`다.
"""

import typing
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from psycopg.errors import UndefinedColumn, UndefinedTable

from app import db
from app.agents import dispatch as dispatch_agent
from app.api import cases
from app.api.errors import ApiException, not_found, unprocessable
from app.dispatch import repository, service
from app.dispatch.regions import REGIONS
from app.schemas.admin import (
    AdminOverview,
    Coverage,
    IntakeDetail,
    IntakeView,
    StaffMember,
    StaffRole,
)

STAFF_ROLES: tuple[str, ...] = typing.get_args(StaffRole)
COVERAGES: tuple[str, ...] = typing.get_args(Coverage)
INTAKE_STATES = ("open", "unassigned", "closed")  # 목록 거르기(GET /admin/intakes?state=)
MAX_LIMIT = 200
KST = timezone(timedelta(hours=9))


@dataclass
class AdminServices:
    """테스트에서 바꿔 끼울 수 있게 DB 연결·시계·배정 방식을 모은다(cases.py와 같은 방식)."""

    connect: Callable = db.connect
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    decide: service.Decider = dispatch_agent.decide


services = AdminServices()


def configure(**kw: object) -> AdminServices:
    global services
    services = AdminServices(**{**services.__dict__, **kw})  # type: ignore[arg-type]
    return services


@asynccontextmanager
async def _connection() -> AsyncIterator[Any]:
    try:
        async with services.connect() as conn:
            yield conn
    except (UndefinedTable, UndefinedColumn) as exc:
        raise ApiException(
            503, "dispatch_db_not_ready",
            "접수·배정 표가 아직 없습니다. 마이그레이션을 적용한 뒤 다시 시도하세요.", True,
        ) from exc


def _iso(value: object) -> str | None:
    """응답 시각은 한국 시간(+09:00)으로 준다(DB는 UTC로 돌려준다)."""
    return value.astimezone(KST).isoformat() if isinstance(value, datetime) else None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _id(value: object) -> str | None:
    return str(value) if value is not None else None


def _day_start(now: datetime) -> datetime:
    """오늘(한국 시간) 0시."""
    return now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)


async def _real_stages(conn: Any, case_ids: list[object]) -> dict[str, str]:
    """실제 사건의 지금 단계(담당자 화면과 같은 판정). 확인 완료된 보고서는 done이다."""
    if not case_ids:
        return {}
    cur = await conn.execute(cases.SUMMARY_SQL + " where c.id = any(%s)", (list(case_ids),))
    stages: dict[str, str] = {}
    for row in await cur.fetchall():
        summary = cases.summarize(row)
        done = summary["stage"] == "report" and summary["approval_status"] == "approved"
        stages[str(row["id"])] = "done" if done else str(summary["stage"])
    return stages


def _stage(row: dict, real_stages: dict[str, str]) -> str:
    if row["simulated"] and row["simulated_stage"] == "done":
        return "done"  # 종결 시각이 지난 가상 접수(배정되지 못한 건도 대기열에서 빠진다)
    if row["status"] in ("received", "unassigned"):
        return "unassigned"
    if row["simulated"]:
        return row["simulated_stage"]
    if row["status"] == "closed":
        return "done"
    return real_stages.get(str(row["case_id"]), "intake")


def intake_view(row: dict, real_stages: dict[str, str]) -> IntakeView:
    note = None
    if row.get("assignment_method"):
        note = {"method": row["assignment_method"], "reason": row["assignment_reason"],
                "created_at": _iso(row["assignment_at"])}
    closed_at = row["closed_at"]
    if row["simulated"] and row["simulated_stage"] == "done":
        closed_at = row["closes_at"]
    return {
        "intake_id": str(row["id"]),
        "received_at": _iso(row["received_at"]),  # type: ignore[typeddict-item]
        "accident_at": _iso(row["accident_at"]),
        "location": row["location"],
        "description": row["description"],
        "injured_count": int(row["injured_count"]),
        "region": row["region"],
        "status": row["status"],
        "stage": _stage(row, real_stages),  # type: ignore[typeddict-item]
        "property_assignee_id": _id(row["property_assignee_id"]),
        "injury_assignee_id": _id(row["injury_assignee_id"]),
        "unassigned_reason": row["unassigned_reason"],
        "case_id": _id(row["case_id"]),
        "simulated": bool(row["simulated"]),
        "closed_at": _iso(closed_at),
        "assignment": note,  # type: ignore[typeddict-item]
    }


async def _views(conn: Any, rows: list[dict]) -> list[IntakeView]:
    real = [r["case_id"] for r in rows
            if not r["simulated"] and r["status"] == "assigned" and r["case_id"] is not None]
    stages = await _real_stages(conn, real)
    return [intake_view(row, stages) for row in rows]


async def get_overview(admin_id: str) -> AdminOverview:
    """담당자 목록(업무량 포함)과 전체 현황 숫자. 관리자 확인(require_admin) 뒤에만 부른다."""
    now = services.now()
    async with _connection() as conn:
        await repository.sync_real_cases(conn)
        staff_rows = await repository.staff(conn, list(STAFF_ROLES), now)
        totals = await repository.summary(conn, now, _day_start(now))
        by_stage = Counter(await repository.simulated_stage_counts(conn, now))
        real_stages = await _real_stages(conn, await repository.open_real_case_ids(conn, now))
    by_stage.update(stage for stage in real_stages.values() if stage != "done")

    staff: list[StaffMember] = [{
        "user_id": str(row["id"]),
        "email": _text(row["email"]),
        "name": _text(row["name"]),
        "staff_role": row["staff_role"],
        "coverage": row["coverage"] if row["coverage"] in COVERAGES else None,
        "region": row["region"] if row["region"] in REGIONS else None,
        "auto_assign": bool(row["auto_assign"]),
        "cases": {"open": int(row["open_cases"]), "closed": int(row["closed_cases"])},
    } for row in staff_rows]
    return {
        "staff": staff,
        "summary": {
            "open": int(totals["open"]),
            "unassigned": int(totals["unassigned"]),
            "received_today": int(totals["received_today"]),
            "closed_today": int(totals["closed_today"]),
            "by_stage": {stage: by_stage[stage] for stage in sorted(by_stage)},
        },
        "generated_at": _iso(now),  # type: ignore[typeddict-item]
    }


async def list_intakes(
    admin_id: str, *, state: str | None = None, assignee_id: str | None = None,
    region: str | None = None, before: datetime | None = None, limit: int = 50,
) -> list[IntakeView]:
    """접수 건 목록(최근 접수 순). state는 open·unassigned·closed, before는 그 시각보다 앞선 건."""
    if state is not None and state not in INTAKE_STATES:
        raise unprocessable(
            "invalid_intake_filter", "state는 open·unassigned·closed 중 하나입니다."
        )
    if region is not None and region not in REGIONS:
        raise unprocessable("invalid_intake_filter", "region은 17개 시·도 이름 중 하나입니다.")
    if assignee_id is not None:
        try:
            assignee_id = str(uuid.UUID(assignee_id))
        except ValueError as exc:
            raise unprocessable("invalid_intake_filter", "assignee_id는 계정 ID입니다.") from exc
    limit = max(1, min(int(limit), MAX_LIMIT))
    now = services.now()
    async with _connection() as conn:
        await repository.sync_real_cases(conn)
        rows = await repository.list_intakes(
            conn, now=now, limit=limit, state=state, assignee_id=assignee_id, region=region,
            before=before,
        )
        return await _views(conn, rows)


async def register_intake(
    admin_id: str, *, location: str, description: str, injured_count: int = 0,
    accident_at: datetime | None = None,
) -> IntakeView:
    """관리자가 접수를 등록한다. 바로 배정하고 결과(배정 또는 미배정과 그 까닭)를 돌려준다."""
    now = services.now()
    if accident_at is not None and accident_at.tzinfo is None:
        accident_at = accident_at.replace(tzinfo=KST)
    if accident_at is not None and accident_at > now + timedelta(minutes=5):
        raise unprocessable("invalid_intake", "사고 일시가 지금보다 뒤입니다.")
    new = service.NewIntake(
        location=location.strip(), description=description.strip(),
        injured_count=injured_count, accident_at=accident_at, registered_by=admin_id,
    )
    async with _connection() as conn:
        created = await service.register(conn, new, now=now, decide=services.decide)
        row = await repository.get_intake(conn, created["id"], now)
        assert row is not None
        return (await _views(conn, [row]))[0]


def _intake_id(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise not_found("접수 건을 찾을 수 없습니다.") from exc


async def _detail(conn: Any, intake_id: str, now: datetime) -> IntakeDetail:
    row = await repository.get_intake(conn, intake_id, now)
    if row is None or row["received_at"] > now:  # 아직 오지 않은 가상 접수는 없는 것이다
        raise not_found("접수 건을 찾을 수 없습니다.")
    view = (await _views(conn, [row]))[0]
    history = [{
        "coverage": h["coverage"], "from_assignee_id": _id(h["from_assignee_id"]),
        "to_assignee_id": str(h["to_assignee_id"]), "method": h["method"], "reason": h["reason"],
        "decided_by": _id(h["decided_by"]), "created_at": _iso(h["created_at"]),
    } for h in await repository.assignments(conn, intake_id) if h["created_at"] <= now]
    return {**view, "assignments": history}  # type: ignore[typeddict-item]


async def get_intake(admin_id: str, intake_id: str) -> IntakeDetail:
    """접수 건 하나와 배정 이력."""
    intake_id = _intake_id(intake_id)
    now = services.now()
    async with _connection() as conn:
        await repository.sync_real_cases(conn)
        return await _detail(conn, intake_id, now)


async def assign_intake(
    admin_id: str, intake_id: str, *, coverage: str, assignee_id: str, reason: str
) -> IntakeDetail:
    """관리자가 한 분야의 담당자를 직접 정한다(담당자가 없었으면 직접 배정, 있었으면 이관)."""
    intake_id = _intake_id(intake_id)
    if coverage not in COVERAGES:
        raise unprocessable("invalid_assignment", "coverage는 property 또는 injury입니다.")
    try:
        assignee_id = str(uuid.UUID(assignee_id))
    except ValueError as exc:
        raise unprocessable("invalid_assignment", "assignee_id는 담당자 계정 ID입니다.") from exc
    now = services.now()
    async with _connection() as conn:
        await repository.sync_real_cases(conn)
        try:
            await service.assign_by_admin(
                conn, intake_id=intake_id, coverage=coverage, assignee_id=assignee_id,
                reason=reason.strip(), admin_id=admin_id, now=now,
            )
        except service.IntakeNotFound as exc:
            raise not_found(str(exc)) from exc
        except service.IntakeClosed as exc:
            raise ApiException(409, "intake_closed", str(exc), False) from exc
        except service.InvalidAssignment as exc:
            raise unprocessable("invalid_assignment", str(exc)) from exc
        return await _detail(conn, intake_id, now)
