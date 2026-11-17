"""B(통합): 접수 등록과 자동 배정.

순서: 접수 건을 만든다 → 누구에게 줄지 정한다(Decider) → 한 트랜잭션에서 적용한다.
적용은 접수 건의 담당자·상태, 배정 이력, 그리고 실제 접수면 사건(cases) 행이다.

누구에게 줄지 정하는 일(Decider)은 바꿔 끼울 수 있다. 여기 있는 `decide_by_rule`은 에이전트 없이
규칙만 쓴다 — 사고 장소 글에서 지역을 찾고(regions.detect), 그 지역·분야 담당자 가운데 진행 중
사건이 가장 적은 사람을 고른다. 배정 에이전트는 같은 모양의 Decider로 이 위에 얹는다.

- 대물 담당자는 늘 배정한다(차대차 사고에는 늘 대물이 있다).
  다친 사람이 있으면 대인 담당자도 배정한다.
- 대물 담당자를 구하지 못하면 미배정(unassigned)으로 남기고 까닭을 적는다. 관리자가 직접 배정한다.
- 대인 담당자만 구하지 못하면 대물 담당자는 배정하고 까닭을 접수 건에 적어 둔다.
- 자동 배정에서 뺀 계정(app_metadata.auto_assign = false — 시연·테스트 계정)은 후보가 아니다.
- 부재인 담당자(app_metadata.away)는 후보가 아니다.
- 고액 사건은 처리 권한(app_metadata.high_value)이 있는 담당자만 후보다. 대물은 예상 손해액이
  HIGH_VALUE_DAMAGE 이상일 때, 대인은 다친 정도가 중상·사망일 때다. 권한 있는 사람이 그 지역에
  없으면 배정하지 않고 까닭을 적는다(관리자가 직접 배정한다).
이 세 가지는 규칙 배정과 배정 에이전트가 똑같이 지킨다(eligible). 그 안에서 규칙은 업무량만 보고,
에이전트는 전문 분야·경력·계약자 이력까지 본다.
"""

import hashlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from psycopg import AsyncConnection

from app.dispatch import regions, repository, simulation

Row = repository.Row
COVERAGE_LABELS = {"property": "대물", "injury": "대인"}
HIGH_VALUE_DAMAGE = 30_000_000  # 예상 손해액이 이 값(원) 이상이면 고액 대물 사건이다
SEVERE_INJURIES = ("serious", "fatal")
MULTI_VEHICLE_COUNT = 3  # 관련 차량이 이 수 이상이면 다중 추돌이다
COMPLAINTS_MANY, ACCIDENTS_MANY = 2, 3  # 계약자 민원·최근 3년 사고가 이 횟수 이상이면 까다롭다
NEAR_LIMIT = 0.8  # 예상 손해액이 대물 한도의 이 비율 이상이면 한도에 가깝다


class IntakeNotFound(Exception):  # noqa: N818 — API가 404로 바꾼다
    """그 접수 건이 없다."""


class IntakeClosed(Exception):  # noqa: N818 — API가 409로 바꾼다
    """종결된 접수 건은 담당자를 바꾸지 못한다."""


class InvalidAssignment(Exception):  # noqa: N818 — API가 422로 바꾼다
    """그 사람에게 배정할 수 없다(담당자가 아님·분야가 다름·이미 그 사람 등)."""


@dataclass(frozen=True)
class NewIntake:
    location: str
    description: str
    injured_count: int = 0
    accident_at: datetime | None = None
    received_at: datetime | None = None  # 없으면 지금. 가상 접수를 미리 만들 때 넘긴다
    registered_by: str | None = None
    simulated: bool = False
    details: dict = field(default_factory=dict)  # 배정 참고 정보(schemas.admin.IntakeDetails)
    # 가상 접수가 접수 시각부터 사는 시간(시연 접수). 그동안 접수 단계이고, 끝이 종결 시각이다
    lifetime: timedelta | None = None


@dataclass(frozen=True)
class Pick:
    """한 분야의 담당자로 고른 사람과 그 까닭."""

    user_id: str
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    method: str  # rule · agent
    region: str | None
    property: Pick | None
    injury: Pick | None = None
    unassigned_reason: str | None = None  # 대물 담당자를 구하지 못한 까닭
    injury_note: str | None = None  # 대인 담당자만 구하지 못한 까닭


Decider = Callable[[AsyncConnection, Row, datetime], Awaitable[Decision]]


def least_loaded(candidates: list[Row], tie_break: str) -> Row:
    """진행 중 사건이 가장 적은 후보. 같으면 접수 건마다 다르게(그러나 늘 같은 답으로) 고른다."""
    fewest = min(int(c["open_cases"]) for c in candidates)
    tied = [c for c in candidates if int(c["open_cases"]) == fewest]
    return min(tied, key=lambda c: hashlib.sha256(f"{tie_break}:{c['id']}".encode()).digest())


def needs_authority(intake: Row, coverage: str) -> bool:
    """고액 사건인가 — 처리 권한이 있는 담당자만 맡을 수 있다."""
    details = intake.get("details") or {}
    if coverage == "property":
        return int(details.get("estimated_damage") or 0) >= HIGH_VALUE_DAMAGE
    return details.get("injury_severity") in SEVERE_INJURIES


def needed_specialties(intake: Row, coverage: str) -> list[str]:
    """이 사건에 맞는 전문 분야(schemas.admin.Specialty). 없으면 빈 목록."""
    details = intake.get("details") or {}
    if coverage == "injury":
        return ["serious_injury"] if details.get("injury_severity") in SEVERE_INJURIES else []
    needed = ["imported"] if details.get("vehicle_type") == "imported" else []
    if int(details.get("vehicle_count") or 0) >= MULTI_VEHICLE_COUNT:
        needed.append("multi_vehicle")
    return needed


def complications(intake: Row) -> list[str]:
    """까다로운 사건으로 볼 까닭(경력이 긴 담당자가 맡는 편이 좋다). 없으면 빈 목록."""
    details = intake.get("details") or {}
    holder, policy = details.get("policyholder") or {}, details.get("policy") or {}
    found = []
    if int(holder.get("complaints") or 0) >= COMPLAINTS_MANY:
        found.append(f"계약자 민원 {int(holder['complaints'])}회")
    if int(holder.get("accidents_3y") or 0) >= ACCIDENTS_MANY:
        found.append(f"최근 3년 사고 {int(holder['accidents_3y'])}회")
    if int(intake["injured_count"]) > 0 and policy.get("bodily_injury_ii") is False:
        found.append("다친 사람이 있는데 대인배상Ⅱ 미가입")
    damage, limit = details.get("estimated_damage"), policy.get("property_limit")
    if damage and limit and int(damage) >= int(limit) * NEAR_LIMIT:
        found.append("예상 손해액이 대물 한도에 가깝다")
    return found


@dataclass(frozen=True)
class Eligible:
    """한 지역·분야에서 이 접수를 맡을 수 있는 후보."""

    rows: list[Row]
    region: str
    coverage: str
    total: int  # 권한으로 거르기 전의 후보 수(부재·자동 배정 제외 계정은 이미 빠졌다)
    restricted: bool  # 고액 사건이라 권한 있는 사람만 남겼다

    def none_reason(self) -> str:
        label = COVERAGE_LABELS[self.coverage]
        if self.restricted and self.total:
            return f"{self.region} 지역에 고액 사건 처리 권한이 있는 {label} 담당자가 없습니다."
        return f"{self.region} 지역에 배정할 {label} 담당자가 없습니다."


async def eligible(
    conn: AsyncConnection, intake: Row, region: str, coverage: str, at: datetime
) -> Eligible:
    found = await repository.candidates(conn, region, coverage, at)
    restricted = needs_authority(intake, coverage)
    rows = [c for c in found if c["high_value"]] if restricted else found
    return Eligible(rows, region, coverage, len(found), restricted)


async def pick_by_load(
    conn: AsyncConnection, intake: Row, region: str, coverage: str, at: datetime
) -> tuple[Pick | None, Eligible]:
    """그 지역·분야에서 맡을 수 있는 담당자 가운데 업무량이 가장 적은 사람. 없으면 None."""
    pool = await eligible(conn, intake, region, coverage, at)
    if not pool.rows:
        return None, pool
    chosen = least_loaded(pool.rows, str(intake["id"]))
    label = COVERAGE_LABELS[coverage]
    who = f"고액 사건 처리 권한이 있는 {label} 담당자" if pool.restricted else f"{label} 담당자"
    reason = (
        f"{region} 지역 {who} {len(pool.rows)}명 가운데 진행 중인 사건이 가장 적습니다"
        f"({int(chosen['open_cases'])}건)."
    )
    detail = {
        "region": region, "candidates": len(pool.rows), "open_cases": int(chosen["open_cases"]),
        "loads": sorted(int(c["open_cases"]) for c in pool.rows),
        "high_value_only": pool.restricted,
    }
    return Pick(str(chosen["id"]), reason, detail), pool


async def decide_by_rule(conn: AsyncConnection, intake: Row, at: datetime) -> Decision:
    """규칙 배정. 지역은 사고 장소 글에서 찾고, 담당자는 업무량으로 고른다."""
    region = regions.detect(intake["location"])
    if region is None:
        return Decision("rule", None, None,
                        unassigned_reason="사고 장소에서 지역을 판별하지 못했습니다.")
    property_pick, pool = await pick_by_load(conn, intake, region, "property", at)
    if property_pick is None:
        return Decision("rule", region, None, unassigned_reason=pool.none_reason())
    if int(intake["injured_count"]) <= 0:
        return Decision("rule", region, property_pick)
    injury_pick, pool = await pick_by_load(conn, intake, region, "injury", at)
    note = None if injury_pick else pool.none_reason()
    return Decision("rule", region, property_pick, injury_pick, injury_note=note)


async def apply(conn: AsyncConnection, intake: Row, decision: Decision, at: datetime) -> Row:
    """결정을 적용한다. 호출하는 쪽이 트랜잭션을 연다."""
    if decision.property is None:
        reason = decision.unassigned_reason or "배정할 담당자를 찾지 못했습니다."
        return await repository.mark_unassigned(conn, intake["id"], decision.region, reason)
    assert decision.region is not None
    injury_id = decision.injury.user_id if decision.injury else None
    case_id = None
    if not intake["simulated"]:
        case_id = await repository.insert_case(
            conn, decision.property.user_id, injury_id, intake["accident_at"]
        )
    updated = await repository.mark_assigned(
        conn, intake["id"], region=decision.region, property_id=decision.property.user_id,
        injury_id=injury_id, case_id=case_id, note=decision.injury_note,
    )
    for coverage, pick in (("property", decision.property), ("injury", decision.injury)):
        if pick is not None:
            await repository.insert_assignment(
                conn, intake_id=intake["id"], coverage=coverage, to_assignee_id=pick.user_id,
                method=decision.method, reason=pick.reason, detail=pick.detail, created_at=at,
            )
    return updated


async def register(
    conn: AsyncConnection, new: NewIntake, *, now: datetime,
    decide: Decider = decide_by_rule, rng: random.Random | None = None,
) -> Row:
    """접수 건을 만들고 바로 배정한다. 배정 시각과 업무량 기준은 접수 시각이다."""
    received_at = new.received_at or now
    plan: dict[str, datetime | None] = dict.fromkeys(
        ("facts_at", "judgment_at", "report_at", "closes_at")
    )
    if new.simulated and new.lifetime is not None:
        plan = dict.fromkeys(plan, received_at + new.lifetime)
    elif new.simulated:
        plan = dict(simulation.plan(received_at, rng or random.Random()))
    async with conn.transaction():
        intake = await repository.insert_intake(conn, {
            "received_at": received_at, "accident_at": new.accident_at,
            "location": new.location, "description": new.description,
            "injured_count": new.injured_count, "details": new.details,
            "registered_by": new.registered_by, "simulated": new.simulated, **plan,
        })
        decision = await decide(conn, intake, received_at)
        return await apply(conn, intake, decision, received_at)


async def assign_by_admin(
    conn: AsyncConnection, *, intake_id: object, coverage: str, assignee_id: str, reason: str,
    admin_id: str, now: datetime,
) -> Row:
    """관리자가 한 분야의 담당자를 직접 정한다. 담당자가 없던 분야면 직접 배정, 있었으면 이관이다.

    자동 배정에서 뺀 계정(시연 계정)에게도 줄 수 있다 — 빼는 것은 자동 배정뿐이다.
    실제 접수면 사건의 담당자도 함께 바뀌어, 이전 담당자는 바로 접근이 끊기고 새 담당자가 본다.
    """
    async with conn.transaction():
        intake = await repository.lock_intake(conn, intake_id)
        if intake is None or intake["received_at"] > now:  # 아직 오지 않은 가상 접수는 없는 것이다
            raise IntakeNotFound("접수 건을 찾을 수 없습니다.")
        if intake["status"] == "closed" or (intake["simulated"] and intake["closes_at"] <= now):
            raise IntakeClosed("종결된 접수 건은 담당자를 바꿀 수 없습니다.")
        person = await repository.adjuster(conn, assignee_id)
        if person is None:
            raise InvalidAssignment("담당자 계정이 아닙니다.")
        label = COVERAGE_LABELS[coverage]
        if person["coverage"] != coverage:
            raise InvalidAssignment(f"{label} 담당자가 아닙니다.")
        current = intake[repository.ASSIGNEE_COLUMNS[coverage]]
        if current is not None and str(current) == str(person["id"]):
            raise InvalidAssignment(f"이미 그 사람이 {label} 담당자입니다.")
        waiting = intake["status"] in ("received", "unassigned")
        if waiting and coverage == "injury":
            raise InvalidAssignment("대물 담당자를 먼저 배정해야 합니다.")

        if waiting:  # 미배정 건의 첫 배정: 실제 접수면 사건을 만든다
            case_id = None
            if not intake["simulated"]:
                case_id = await repository.insert_case(
                    conn, str(person["id"]), None, intake["accident_at"]
                )
            await repository.mark_assigned(
                conn, intake["id"], region=intake["region"] or person["region"],
                property_id=str(person["id"]), injury_id=None, case_id=case_id, note=None,
            )
        else:
            await repository.set_assignee(conn, intake, coverage, str(person["id"]))
        await repository.insert_assignment(
            conn, intake_id=intake["id"], coverage=coverage, to_assignee_id=str(person["id"]),
            method="transfer" if current is not None else "manual", reason=reason, detail=None,
            created_at=now, from_assignee_id=str(current) if current is not None else None,
            decided_by=admin_id,
        )
    return intake
