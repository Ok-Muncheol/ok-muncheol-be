"""B(통합): 접수·배정과 관리자 조회를 실제 Postgres로 검사한다.

`MIGRATION_TEST_DATABASE_URL`(버려도 되는 로컬·CI Postgres)과 psql이 있을 때만 돈다.
- 접수 등록 → 규칙 배정: 지역·분야가 맞는 담당자 가운데 업무량이 적은 사람, 다친 사람이 있으면
  대인 담당자도, 자동 배정에서 뺀 계정·정지 계정은 후보가 아니다. 못 구하면 미배정과 그 까닭
- 실제 접수는 사건(cases) 행과 배정 이력을 만들고, 가상 접수는 일정만 갖고 사건을 만들지 않는다
- 현황(GET /admin/overview): 담당자 업무량, 진행 중·미배정·오늘 접수·오늘 종결, 단계별 수.
  접수 시각이 아직 오지 않은 가상 접수는 어디에도 보이지 않는다
- 목록(GET /admin/intakes): 거르기와 쪽 나눔
- 실제 사건과 맞추기: 접수 없이 생긴 사건, 밖에서 바뀐 담당자, 확인 완료된 사건의 종결
- 표가 아직 없으면 503
"""

import asyncio
import random
import shutil
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Json, Jsonb

from app import db
from app.api import admin
from app.api.errors import ApiException
from app.dispatch import repository, service
from tests.api.test_case_assignees_db import temp_database
from tests.migrations.test_migrations_apply import ADMIN_URL, MIGRATIONS

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or shutil.which("psql") is None,
    reason="MIGRATION_TEST_DATABASE_URL과 psql이 있어야 한다(supabase/migrations/README.md)",
)

INTAKE_MIGRATION = "20261113200000_case_intakes_assignments.sql"
NOW = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)  # 한국 시간 11/13 15:00
(ADMIN, SEOUL_A, SEOUL_B, BUSAN, SEOUL_INJURY, DEMO, BANNED, NO_ROLE) = (
    str(uuid.uuid4()) for _ in range(8)
)


def profile(conn, user_id: str, app_meta: dict, *, name: str | None = None,
            extra: str = "") -> None:
    conn.execute("insert into auth.users (id) values (%s) on conflict do nothing", (user_id,))
    conn.execute(
        "update auth.users set email = %s, raw_app_meta_data = %s, raw_user_meta_data = %s"
        + extra + " where id = %s",
        (f"{user_id[:8]}@example.com", Jsonb({"provider": "email", **app_meta}),
         Jsonb({"name": name} if name else {}), user_id),
    )


def adjuster(coverage: str, region: str | None, **more) -> dict:
    meta = {"staff_role": "adjuster", "coverage": coverage, **more}
    return {**meta, "region": region} if region else meta


@pytest.fixture(scope="module")
def database_url():
    with temp_database(MIGRATIONS) as url:
        with psycopg.connect(url, autocommit=True) as conn:
            profile(conn, ADMIN, {"staff_role": "admin"})
            profile(conn, SEOUL_A, adjuster("property", "서울"), name="김대물")
            profile(conn, SEOUL_B, adjuster("property", "서울"))
            profile(conn, BUSAN, adjuster("property", "부산"))
            profile(conn, SEOUL_INJURY, adjuster("injury", "서울"))
            profile(conn, DEMO, adjuster("property", "서울", auto_assign=False))  # 시연 계정
            profile(conn, BANNED, adjuster("property", "서울"),
                    extra=", banned_until = now() + interval '1 day'")
            profile(conn, NO_ROLE, {})
        yield url


class Api:
    """테스트 DB에 붙은 관리자 서비스. 시계를 마음대로 옮긴다."""

    def __init__(self, url: str):
        self.url, self.now = url, NOW

    def register(self, location: str, **kw) -> dict:
        return asyncio.run(admin.register_intake(
            ADMIN, location=location, description=kw.pop("description", "교차로 접촉 사고"), **kw
        ))

    def simulated(self, location: str, received_at: datetime, plan_hours: tuple, **kw) -> dict:
        """가상 접수 하나. 일정은 접수 시각에서 (사실 카드, 판단, 보고서, 종결)까지의 시간이다."""
        async def create():
            async with db.connect(self.url) as conn:
                new = service.NewIntake(location=location, description="가상 접수",
                                        received_at=received_at, simulated=True, **kw)
                row = await service.register(conn, new, now=self.now, rng=random.Random(1))
                columns = ("facts_at", "judgment_at", "report_at", "closes_at")
                await conn.execute(
                    "update public.case_intakes set facts_at = %s, judgment_at = %s,"
                    " report_at = %s, closes_at = %s where id = %s",
                    (*(received_at + timedelta(hours=h) for h in plan_hours), row["id"]),
                )
                return {**row, **dict(zip(columns, plan_hours, strict=True))}

        return asyncio.run(create())

    def overview(self) -> dict:
        return asyncio.run(admin.get_overview(ADMIN))

    def intakes(self, **kw) -> list[dict]:
        return asyncio.run(admin.list_intakes(ADMIN, **kw))

    def rows(self, sql: str, *params: object) -> list[dict]:
        with psycopg.connect(self.url, autocommit=True, row_factory=dict_row) as conn:
            return conn.execute(sql, params).fetchall()

    def staff(self) -> dict[str, dict]:
        return {m["user_id"]: m for m in self.overview()["staff"]}


@pytest.fixture
def api(database_url):
    """테스트마다 접수·사건을 비우고 시작한다."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("truncate public.case_assignments, public.case_intakes, public.cases cascade")
    old, client = admin.services, Api(database_url)
    admin.configure(connect=lambda: db.connect(database_url), now=lambda: client.now)
    yield client
    admin.services = old


def error(call) -> tuple[int, str]:
    with pytest.raises(ApiException) as caught:
        call()
    return caught.value.status_code, caught.value.error["code"]


# ---------------------------------------------------------------- 접수 등록 → 배정


def test_register_assigns_the_least_loaded_property_adjuster_in_the_region(api):
    accident = datetime(2026, 11, 12, 23, 30, tzinfo=UTC)
    first = api.register("서울 강남구 테헤란로 152", accident_at=accident)
    second = api.register("서울특별시 마포구 월드컵로")
    third = api.register("서울 송파구 올림픽로")

    assert first["status"] == "assigned" and first["region"] == "서울"
    assert (first["stage"], first["simulated"], first["injury_assignee_id"]) \
        == ("intake", False, None)
    # 두 서울 대물 담당자가 번갈아 받는다. 시연 계정(자동 배정 제외)과 정지 계정은 받지 않는다
    picked = [v["property_assignee_id"] for v in (first, second, third)]
    assert set(picked[:2]) == {SEOUL_A, SEOUL_B} and picked[2] in (SEOUL_A, SEOUL_B)
    assert first["assignment"]["method"] == "rule"
    assert "서울 지역 대물 담당자 2명" in first["assignment"]["reason"]

    # 실제 접수는 담당자 화면이 읽는 사건을 만든다
    case = api.rows("select * from public.cases where id = %s", first["case_id"])[0]
    assert str(case["assignee_id"]) == first["property_assignee_id"]
    assert case["injury_assignee_id"] is None and case["accident_date"] == accident
    history = api.rows("select coverage, method, decided_by, from_assignee_id, detail"
                       " from public.case_assignments where intake_id = %s", first["intake_id"])
    assert [(h["coverage"], h["method"], h["decided_by"], h["from_assignee_id"])
            for h in history] == [("property", "rule", None, None)]
    assert history[0]["detail"]["candidates"] == 2 and history[0]["detail"]["region"] == "서울"
    registered = api.rows("select registered_by from public.case_intakes where id = %s",
                          first["intake_id"])[0]["registered_by"]
    assert str(registered) == ADMIN


def test_injured_people_add_an_injury_adjuster(api):
    view = api.register("서울 종로구 세종대로", injured_count=2)
    assert (view["property_assignee_id"] in (SEOUL_A, SEOUL_B), view["injury_assignee_id"]) \
        == (True, SEOUL_INJURY)
    case = api.rows("select * from public.cases where id = %s", view["case_id"])[0]
    assert str(case["injury_assignee_id"]) == SEOUL_INJURY
    coverages = api.rows("select coverage from public.case_assignments where intake_id = %s"
                         " order by coverage", view["intake_id"])
    assert [c["coverage"] for c in coverages] == ["injury", "property"]


def test_missing_injury_adjuster_still_assigns_property_and_says_why(api):
    view = api.register("부산 해운대구 우동", injured_count=1)
    assert (view["status"], view["property_assignee_id"], view["injury_assignee_id"]) \
        == ("assigned", BUSAN, None)
    assert view["unassigned_reason"] == "부산 지역에 배정할 대인 담당자가 없습니다."


@pytest.mark.parametrize(
    ("location", "region", "reason"),
    [("집 앞 사거리", None, "사고 장소에서 지역을 판별하지 못했습니다."),
     ("제주 서귀포시 중문관광로", "제주", "제주 지역에 배정할 대물 담당자가 없습니다.")],
)
def test_unassigned_when_no_adjuster_can_be_found(api, location, region, reason):
    view = api.register(location, injured_count=1)
    assert (view["status"], view["stage"], view["region"]) == ("unassigned", "unassigned", region)
    assert (view["unassigned_reason"], view["property_assignee_id"], view["case_id"]) \
        == (reason, None, None)
    assert api.rows("select count(*) as n from public.cases")[0]["n"] == 0
    assert api.rows("select count(*) as n from public.case_assignments")[0]["n"] == 0
    assert api.overview()["summary"]["unassigned"] == 1


def test_accident_time_cannot_be_in_the_future(api):
    later = api.now + timedelta(hours=2)
    assert error(lambda: api.register("서울 강남구", accident_at=later)) == (422, "invalid_intake")


# ---------------------------------------------------------------- 가상 접수와 현황


def test_simulated_intake_keeps_a_plan_and_never_touches_cases(api):
    row = api.simulated("서울 강남구", api.now - timedelta(hours=10), (2, 4, 6, 30))
    assert row["simulated"] and row["status"] == "assigned" and row["case_id"] is None
    assert str(row["property_assignee_id"]) in (SEOUL_A, SEOUL_B)
    assert api.rows("select count(*) as n from public.cases")[0]["n"] == 0

    listed = {v["intake_id"]: v for v in api.intakes()}
    assert listed[str(row["id"])]["stage"] == "report"  # 접수 뒤 10시간 — 보고서 단계
    api.now += timedelta(hours=25)  # 종결 시각이 지났다. 행은 그대로인데 종결로 보인다
    done = api.intakes()[0]
    assert (done["stage"], done["status"]) == ("done", "assigned")
    assert done["closed_at"] is not None
    assert api.staff()[done["property_assignee_id"]]["cases"] == {"open": 0, "closed": 1}


def test_future_simulated_intakes_are_invisible_until_their_time(api):
    """미리 만들어 둔 다음 접수는 접수 시각이 올 때까지 목록·숫자·업무량 어디에도 없다."""
    ahead = api.simulated("서울 강남구", api.now + timedelta(hours=3), (10, 20, 30, 40))
    assert api.intakes() == []
    summary = api.overview()["summary"]
    assert (summary["open"], summary["received_today"], summary["by_stage"]) == (0, 0, {})
    assert api.staff()[str(ahead["property_assignee_id"])]["cases"] == {"open": 0, "closed": 0}

    api.now += timedelta(hours=4)
    assert [v["intake_id"] for v in api.intakes()] == [str(ahead["id"])]
    assert api.overview()["summary"]["open"] == 1


def test_candidate_load_is_counted_at_the_intake_time(api):
    """가상 접수를 미리 만들 때는 그 접수가 들어오는 시각의 업무량으로 고른다."""
    early = api.simulated("서울 강남구", api.now - timedelta(hours=5), (1, 2, 3, 6))
    busy = str(early["property_assignee_id"])
    other = SEOUL_B if busy == SEOUL_A else SEOUL_A
    # 지금: busy가 1건을 맡고 있어 다른 사람에게 간다
    assert api.register("서울 중구")["property_assignee_id"] == other
    # 2시간 뒤: 앞 건이 종결돼(6시간째) busy 0건, other 1건 → busy에게 간다
    later = api.simulated("서울 용산구", api.now + timedelta(hours=2), (1, 2, 3, 6))
    assert str(later["property_assignee_id"]) == busy


def test_overview_counts_load_and_stages(api):
    api.register("서울 강남구")  # 실제 접수, 접수 단계
    api.register("제주 제주시")  # 미배정
    api.simulated("서울 서초구", api.now - timedelta(hours=30), (2, 20, 40, 60))  # 판단 단계
    api.simulated("부산 사상구", api.now - timedelta(hours=50), (2, 20, 40, 49))  # 1시간 전 종결
    api.simulated("부산 동래구", api.now - timedelta(days=5), (2, 20, 40, 60))  # 며칠 전 종결
    api.simulated("서울 은평구", api.now + timedelta(hours=1), (2, 20, 40, 60))  # 아직 안 옴

    out = api.overview()
    assert out["generated_at"] == "2026-11-13T15:00:00+09:00"
    assert out["summary"] == {
        "open": 2, "unassigned": 1, "received_today": 2, "closed_today": 1,
        "by_stage": {"intake": 1, "judgment": 1},
    }
    staff = {m["user_id"]: m for m in out["staff"]}
    assert set(staff) == {ADMIN, SEOUL_A, SEOUL_B, BUSAN, SEOUL_INJURY, DEMO}  # 정지·역할 없음 제외
    assert staff[BUSAN]["cases"] == {"open": 0, "closed": 2}
    assert staff[SEOUL_A]["cases"]["open"] + staff[SEOUL_B]["cases"]["open"] == 2
    assert staff[SEOUL_A] | {"cases": None} == {
        "user_id": SEOUL_A, "email": f"{SEOUL_A[:8]}@example.com", "name": "김대물",
        "staff_role": "adjuster", "coverage": "property", "region": "서울", "auto_assign": True,
        "cases": None,
    }
    assert (staff[DEMO]["auto_assign"], staff[DEMO]["cases"]) == (False, {"open": 0, "closed": 0})
    assert (staff[ADMIN]["staff_role"], staff[ADMIN]["coverage"], staff[ADMIN]["region"]) \
        == ("admin", None, None)


def test_list_filters_and_pages(api):
    api.now -= timedelta(hours=3)
    oldest = api.register("서울 강남구")
    api.now += timedelta(hours=1)
    waiting = api.register("제주 제주시")
    api.now += timedelta(hours=1)
    busan = api.register("부산 사상구")
    api.now += timedelta(hours=1)
    closed = api.simulated("부산 동래구", api.now - timedelta(hours=50), (1, 2, 3, 10))

    ids = lambda **kw: [v["intake_id"] for v in api.intakes(**kw)]  # noqa: E731
    assert ids() == [busan["intake_id"], waiting["intake_id"], oldest["intake_id"],
                     str(closed["id"])]  # 최근 접수 순
    assert ids(state="open") == [busan["intake_id"], oldest["intake_id"]]
    assert ids(state="unassigned") == [waiting["intake_id"]]
    assert ids(state="closed") == [str(closed["id"])]
    assert ids(region="부산") == [busan["intake_id"], str(closed["id"])]
    assert ids(assignee_id=BUSAN, state="open") == [busan["intake_id"]]
    assert ids(limit=2) == [busan["intake_id"], waiting["intake_id"]]
    page_two = datetime.fromisoformat(waiting["received_at"])
    assert ids(before=page_two) == [oldest["intake_id"], str(closed["id"])]

    for bad in ({"state": "done"}, {"region": "한양"}, {"assignee_id": "someone"}):
        assert error(lambda bad=bad: api.intakes(**bad)) == (422, "invalid_intake_filter")


# ---------------------------------------------------------------- 실제 사건과 맞추기


def approve_latest_report(api: Api, case_id: str, actor: str) -> None:
    """그 사건에 확인 완료된 보고서 v1을 넣는다(카드 v1 → 판단 v1 → 보고서 v1)."""
    card = {"case_id": case_id, "version": 1, "status": "draft", "fields": {},
            "confirmed_by": None, "confirmed_at": None}
    versions = {"case_id": case_id, "report_version": 1, "judgment_version": 1,
                "fact_card_version": 1}
    report = {"versions": versions, "approval_status": "approved", "approved_by": actor,
              "approved_at": "2026-11-13T05:00:00Z"}
    with psycopg.connect(api.url, autocommit=True) as conn:
        conn.execute("insert into public.fact_cards (case_id, version, card, origin)"
                     " values (%s, 1, %s, 'vision')", (case_id, Json(card)))
        conn.execute(
            "insert into public.judgment_runs (case_id, judgment_version, fact_card_version,"
            " criteria_version, rule_version, requested_by) values (%s, 1, 1, 'c', 'r', %s)",
            (case_id, actor))
        conn.execute(
            "insert into public.review_reports (case_id, report_version, judgment_version,"
            " fact_card_version, report, created_by) values (%s, 1, 1, 1, %s, %s)",
            (case_id, Json(report), actor))


def test_cases_made_outside_intake_show_up_and_follow_the_case(api):
    case_id = str(uuid.uuid4())
    with psycopg.connect(api.url, autocommit=True) as conn:
        # 테스트 시계(NOW)보다 앞서 만든 사건으로 둔다 — 접수 시각은 사건을 만든 시각이다
        conn.execute("insert into public.cases (id, assignee_id, created_at) values (%s, %s, %s)",
                     (case_id, DEMO, NOW - timedelta(hours=1)))
    # 접수 없이 생긴 사건도 관제 화면에 나온다
    only = api.intakes()
    assert [(v["case_id"], v["property_assignee_id"], v["location"], v["stage"]) for v in only] \
        == [(case_id, DEMO, "기록 없음", "intake")]
    assert api.intakes() == only  # 다시 읽어도 하나다

    # 이 모듈 밖에서 사건의 담당자가 바뀌면 접수 건이 따라간다
    with psycopg.connect(api.url, autocommit=True) as conn:
        conn.execute("update public.cases set injury_assignee_id = %s where id = %s",
                     (SEOUL_INJURY, case_id))
    assert api.intakes()[0]["injury_assignee_id"] == SEOUL_INJURY
    assert api.staff()[SEOUL_INJURY]["cases"] == {"open": 1, "closed": 0}

    # 확인 완료된 사건은 종결된다
    approve_latest_report(api, case_id, DEMO)
    done = api.intakes()[0]
    assert (done["status"], done["stage"]) == ("closed", "done") and done["closed_at"]
    assert api.staff()[DEMO]["cases"] == {"open": 0, "closed": 1}
    assert api.overview()["summary"]["open"] == 0


def test_sync_is_idempotent(api):
    async def sync_twice():
        async with db.connect(api.url) as conn:
            return [dict(await repository.sync_real_cases(conn)) for _ in range(2)]

    with psycopg.connect(api.url, autocommit=True) as conn:
        conn.execute("insert into public.cases (assignee_id) values (%s)", (DEMO,))
    first, second = asyncio.run(sync_twice())
    assert first == {"added": 1, "synced": 0, "closed": 0}
    assert second == {"added": 0, "synced": 0, "closed": 0}


def test_tables_missing_is_503():
    """마이그레이션 적용 전 공용 DB를 흉내 낸다."""
    before = [m for m in MIGRATIONS if m.name < INTAKE_MIGRATION]
    assert len(before) == len(MIGRATIONS) - 1
    old = admin.services
    with temp_database(before) as url:
        admin.configure(connect=lambda: db.connect(url), now=lambda: NOW)
        try:
            assert error(lambda: asyncio.run(admin.get_overview(ADMIN))) \
                == (503, "dispatch_db_not_ready")
            assert error(lambda: asyncio.run(admin.list_intakes(ADMIN))) \
                == (503, "dispatch_db_not_ready")
            assert error(lambda: asyncio.run(admin.register_intake(
                ADMIN, location="서울 강남구", description="접촉"))) \
                == (503, "dispatch_db_not_ready")
        finally:
            admin.services = old
