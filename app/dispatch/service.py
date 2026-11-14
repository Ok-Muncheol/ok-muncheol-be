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
"""

import hashlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from psycopg import AsyncConnection

from app.dispatch import regions, repository, simulation

Row = repository.Row
COVERAGE_LABELS = {"property": "대물", "injury": "대인"}


@dataclass(frozen=True)
class NewIntake:
    location: str
    description: str
    injured_count: int = 0
    accident_at: datetime | None = None
    received_at: datetime | None = None  # 없으면 지금. 가상 접수를 미리 만들 때 넘긴다
    registered_by: str | None = None
    simulated: bool = False


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


async def pick_by_load(
    conn: AsyncConnection, intake: Row, region: str, coverage: str, at: datetime
) -> Pick | None:
    """그 지역·분야에서 업무량이 가장 적은 담당자. 후보가 없으면 None."""
    found = await repository.candidates(conn, region, coverage, at)
    if not found:
        return None
    chosen = least_loaded(found, str(intake["id"]))
    label = COVERAGE_LABELS[coverage]
    reason = (
        f"{region} 지역 {label} 담당자 {len(found)}명 가운데 진행 중인 사건이 가장 적습니다"
        f"({int(chosen['open_cases'])}건)."
    )
    detail = {
        "region": region, "candidates": len(found), "open_cases": int(chosen["open_cases"]),
        "loads": sorted(int(c["open_cases"]) for c in found),
    }
    return Pick(str(chosen["id"]), reason, detail)


async def decide_by_rule(conn: AsyncConnection, intake: Row, at: datetime) -> Decision:
    """규칙 배정. 지역은 사고 장소 글에서 찾고, 담당자는 업무량으로 고른다."""
    region = regions.detect(intake["location"])
    if region is None:
        return Decision("rule", None, None,
                        unassigned_reason="사고 장소에서 지역을 판별하지 못했습니다.")
    property_pick = await pick_by_load(conn, intake, region, "property", at)
    if property_pick is None:
        return Decision("rule", region, None,
                        unassigned_reason=f"{region} 지역에 배정할 대물 담당자가 없습니다.")
    if int(intake["injured_count"]) <= 0:
        return Decision("rule", region, property_pick)
    injury_pick = await pick_by_load(conn, intake, region, "injury", at)
    note = None if injury_pick else f"{region} 지역에 배정할 대인 담당자가 없습니다."
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
    if new.simulated:
        plan = dict(simulation.plan(received_at, rng or random.Random()))
    async with conn.transaction():
        intake = await repository.insert_intake(conn, {
            "received_at": received_at, "accident_at": new.accident_at,
            "location": new.location, "description": new.description,
            "injured_count": new.injured_count, "registered_by": new.registered_by,
            "simulated": new.simulated, **plan,
        })
        decision = await decide(conn, intake, received_at)
        return await apply(conn, intake, decision, received_at)
