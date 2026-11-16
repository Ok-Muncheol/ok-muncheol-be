"""시연용: 제휴 병원이 보내온 것으로 보이는 진료 정보(가데이터).

실제 병원 연동이 아니다. 제휴(MOU) 병원이 진료비·진단 정보를 보험사로 바로 보내는 흐름을 시연하려고,
사건 ID로 정해지는 값을 만들어 준다(같은 사건은 늘 같은 값, 사건마다 다른 값). 저장하지 않는다.

- 화면(검토 보고서 대인 입력)이 이 값을 미리 보여 주고, 담당자가 반영하면 평소의 대인 입력
  (`InjuredPersonDoc`, POST /cases/{id}/documents)으로 저장된다. 산정은 그 입력만 읽는다.
- 다친 사람 수는 접수의 `injured_count`다. 0인데 대인 담당자가 배정된 사건은 1명으로 본다
  (대인 담당자가 맡은 사건은 다친 사람이 있다고 본다).
- 병원 이름은 지어낸 이름이다. 급수·금액은 가벼운 부상(12~14급)이 많게 고른다.
"""

import hashlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from psycopg import AsyncConnection

from app import db
from app.api.errors import forbidden, not_found
from app.workflow import runs

KST = timezone(timedelta(hours=9))
SOURCE = "partner_hospital_demo"
# 지어낸 병원 이름(실제 기관이 아니다)
HOSPITALS = (
    "한빛정형외과의원", "새봄재활의학과의원", "늘푸른신경외과의원", "가온한방병원",
    "바른마디정형외과의원", "서울온누리병원", "맑은숲통증의학과의원", "두루미재활병원",
)
# (상해 급수, 치료관계비 하한, 상한). 가벼운 부상이 많게 여러 번 넣는다
_CASES = (
    (14, 250_000, 700_000), (14, 300_000, 900_000), (14, 200_000, 600_000),
    (13, 400_000, 1_100_000), (12, 600_000, 1_500_000), (12, 700_000, 1_800_000),
    (11, 1_000_000, 2_600_000), (9, 2_200_000, 5_200_000),
)


class PartnerMedicalRecord(TypedDict):
    """제휴 병원 수신 1건(다친 사람 1명). 대인 입력(InjuredPersonDoc)으로 그대로 옮길 수 있다."""

    person_id: str
    label: str
    hospital: str
    treated_from: str  # ISO 날짜
    treated_to: str
    grade: int
    treatment_cost: int
    is_driver: bool
    received_at: str  # ISO 8601
    source: str  # 늘 SOURCE(시연용 가데이터 표시)


def records(case_id: str, count: int, accident_at: datetime | None) -> list[PartnerMedicalRecord]:
    """사건 ID로 정해지는 수신 기록 `count`건. 같은 입력이면 늘 같은 값이다."""
    start = (accident_at or datetime(2026, 1, 1, tzinfo=KST)).astimezone(KST)
    out: list[PartnerMedicalRecord] = []
    for i in range(max(count, 0)):
        seed = hashlib.sha256(f"{case_id}:{i}".encode()).digest()
        grade, low, high = _CASES[seed[0] % len(_CASES)]
        cost = low + int.from_bytes(seed[1:5], "big") % (high - low + 1)
        days = 3 + seed[5] % 19
        end = start + timedelta(days=days)
        out.append({
            "person_id": f"partner-{i + 1}",
            "label": "상대 운전자" if i == 0 else f"상대 동승자 {i}",
            "hospital": HOSPITALS[seed[6] % len(HOSPITALS)],
            "treated_from": start.date().isoformat(), "treated_to": end.date().isoformat(),
            "grade": grade, "treatment_cost": cost // 100 * 100,
            "is_driver": i == 0,
            "received_at": end.replace(hour=18, minute=0, second=0, microsecond=0).isoformat(),
            "source": SOURCE,
        })
    return out


Connector = Callable[[], AbstractAsyncContextManager[AsyncConnection]]


@dataclass
class PartnerMedicalServices:
    connect: Connector = db.connect


services = PartnerMedicalServices()

_CASE_SQL = """
select c.assignee_id, c.injury_assignee_id, c.accident_date,
       (select i.injured_count from public.case_intakes i where i.case_id = c.id
         order by i.received_at desc limit 1) as injured_count
  from public.cases c where c.id = %s
"""


def injured_count(row: dict) -> int:
    """다친 사람 수. 접수 값이 0·없음이어도 대인 담당자가 있으면 1명으로 본다."""
    count = int(row.get("injured_count") or 0)
    return count if count > 0 else (1 if row.get("injury_assignee_id") else 0)


async def get_partner_medical(actor_id: str, case_id: str) -> list[PartnerMedicalRecord]:
    """담당 사건의 제휴 병원 수신 기록(가데이터). 다친 사람이 없으면 빈 목록."""
    try:
        case_id = str(uuid.UUID(str(case_id)))
    except ValueError as exc:
        raise not_found("사건을 찾을 수 없습니다.") from exc
    async with services.connect() as conn:
        cur = await conn.execute(_CASE_SQL, (case_id,))
        row = await cur.fetchone()
    if row is None:
        raise not_found("사건을 찾을 수 없습니다.")
    if actor_id not in runs.assignees_of(row):
        raise forbidden()
    return records(case_id, injured_count(row), row.get("accident_date"))
