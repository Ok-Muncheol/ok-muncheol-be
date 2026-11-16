"""B(통합): 접수·배정의 규칙과 가상 접수의 단계 일정 — DB 없이 도는 부분.

SQL과 서비스 흐름은 tests/api/test_admin_db.py가 실제 Postgres로 본다.
관리자만 부를 수 있는지는 경로가 건다(tests/api/test_app.py).
- 지역 판별(regions.detect): 시·도 이름과 잘 알려진 시 이름, 낱말 앞머리로만 맞춘다
- 후보 고르기(service.least_loaded): 진행 중 사건이 가장 적은 사람, 같으면 접수 건마다 고르게
- 단계 일정(simulation.plan·stage_at·is_open): 순서와 평균 기간, 지금 단계
- 접수 건 응답(admin.intake_view): 미배정·가상·실제 접수의 단계와 종결 시각
"""

import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from app.api import admin
from app.dispatch import regions, service, simulation

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("location", "region"),
    [
        ("서울 강남구 테헤란로 152", "서울"),
        ("서울특별시 관악구 서울대입구역 앞", "서울"),
        ("부산 해운대구 우동 교차로", "부산"),
        ("해운대구 우동 교차로", None),  # '대구'로 읽지 않는다
        ("대구광역시 수성구 범어네거리", "대구"),
        ("경기도 광주시 오포읍", "경기"),  # 도 이름이 먼저다
        ("광주시 오포읍 사거리", "경기"),
        ("광주 서구 상무대로", "광주"),
        ("광주광역시 북구", "광주"),
        ("수원시 팔달구 인계동", "경기"),  # 시 이름만
        ("창원시 성산구", "경남"),
        ("전북특별자치도 전주시 완산구", "전북"),
        ("강원특별자치도 춘천시", "강원"),
        ("충청남도 천안시 서북구", "충남"),
        ("제주 서귀포시 중문", "제주"),
        ("세종특별자치시 한누리대로", "세종"),
        ("집 앞 사거리", None),
        ("", None),
    ],
)
def test_detect_region_from_location_text(location, region):
    assert regions.detect(location) == region


def test_regions_are_the_seventeen_provinces():
    assert len(regions.REGIONS) == len(set(regions.REGIONS)) == 17


def candidate(user_id: str, open_cases: int) -> dict:
    return {"id": user_id, "name": user_id, "open_cases": open_cases}


def test_least_loaded_picks_the_fewest_open_cases():
    people = [candidate("a", 4), candidate("b", 2), candidate("c", 7)]
    assert service.least_loaded(people, "intake-1")["id"] == "b"


def test_least_loaded_spreads_ties_but_is_stable_per_intake():
    people = [candidate(name, 3) for name in "abcd"]
    picks = Counter(service.least_loaded(people, f"intake-{n}")["id"] for n in range(400))
    assert set(picks) == set("abcd") and min(picks.values()) > 60  # 한 사람에게 몰리지 않는다
    again = service.least_loaded(people[::-1], "intake-7")  # 후보 순서가 달라도 같은 답
    assert service.least_loaded(people, "intake-7") == again


def test_plan_orders_stages_and_averages_three_days():
    rng = random.Random(7)
    plans = [simulation.plan(T0, rng) for _ in range(3000)]
    for p in plans:
        assert T0 <= p["facts_at"] <= p["judgment_at"] <= p["report_at"] <= p["closes_at"]
    days = [(p["closes_at"] - T0) / timedelta(days=1) for p in plans]
    assert 2.8 < sum(days) / len(days) < 3.2
    assert simulation.MIN_DAYS <= min(days) and max(days) <= simulation.MAX_DAYS


def test_stage_follows_the_plan_without_any_write():
    row = {"status": "assigned", "simulated": True,
           "facts_at": T0 + timedelta(hours=6), "judgment_at": T0 + timedelta(hours=30),
           "report_at": T0 + timedelta(hours=48), "closes_at": T0 + timedelta(hours=72)}
    seen = [simulation.stage_at(row, T0 + timedelta(hours=h)) for h in (0, 6, 29, 30, 50, 72, 99)]
    assert seen == ["intake", "facts", "facts", "judgment", "report", "done", "done"]
    assert simulation.is_open(row, T0 + timedelta(hours=71))
    assert not simulation.is_open(row, T0 + timedelta(hours=72))  # 종결 시각이 지나면 끝난 것이다
    assert not simulation.is_open({**row, "status": "unassigned"}, T0)
    # 실제 접수는 종결 처리(status)로만 끝난다
    assert simulation.is_open({"status": "assigned", "simulated": False}, T0 + timedelta(days=99))


def intake_row(**kw) -> dict:
    """repository._LIST_SQL 한 행. 기본은 배정된 실제 접수."""
    base = {
        "id": "aaaaaaaa-0000-0000-0000-000000000001", "received_at": T0, "accident_at": None,
        "location": "서울 강남구", "description": "교차로 접촉", "injured_count": 0,
        "region": "서울", "status": "assigned",
        "property_assignee_id": "11111111-1111-1111-1111-111111111111",
        "injury_assignee_id": None, "unassigned_reason": None,
        "case_id": "cccccccc-0000-0000-0000-000000000001", "simulated": False,
        "closes_at": None, "closed_at": None, "simulated_stage": None,
        "assignment_method": "rule", "assignment_reason": "업무량이 가장 적습니다.",
        "assignment_at": T0,
    }
    return {**base, **kw}


def test_view_of_a_real_intake_uses_the_case_stage():
    view = admin.intake_view(intake_row(), {"cccccccc-0000-0000-0000-000000000001": "judgment"})
    assert (view["stage"], view["status"], view["simulated"]) == ("judgment", "assigned", False)
    assert view["received_at"] == "2026-11-13T18:00:00+09:00"  # 한국 시간으로 준다
    assert view["assignment"] == {"method": "rule", "reason": "업무량이 가장 적습니다.",
                                  "created_at": "2026-11-13T18:00:00+09:00"}
    # 사건 요약을 못 읽었으면 접수 단계로 본다
    assert admin.intake_view(intake_row(), {})["stage"] == "intake"
    closed = intake_row(status="closed", closed_at=T0 + timedelta(days=2))
    assert admin.intake_view(closed, {})["stage"] == "done"


def test_view_of_unassigned_and_simulated_intakes():
    waiting = intake_row(status="unassigned", property_assignee_id=None, case_id=None,
                         unassigned_reason="서울 지역에 배정할 대물 담당자가 없습니다.",
                         assignment_method=None, assignment_reason=None, assignment_at=None)
    view = admin.intake_view(waiting, {})
    assert (view["stage"], view["assignment"]) == ("unassigned", None)
    assert view["unassigned_reason"].startswith("서울 지역")

    closes = T0 + timedelta(days=3)
    running = intake_row(simulated=True, case_id=None, closes_at=closes, simulated_stage="report")
    assert admin.intake_view(running, {})["stage"] == "report"
    assert admin.intake_view(running, {})["closed_at"] is None
    # 종결 시각이 지난 가상 접수는 행을 고치지 않아도 종결로 보이고 종결 시각이 붙는다
    done = admin.intake_view({**running, "simulated_stage": "done"}, {})
    assert (done["stage"], done["closed_at"]) == ("done", "2026-11-16T18:00:00+09:00")
