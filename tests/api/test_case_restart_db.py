"""B(통합): 사건 단계 되돌리기(POST /cases/{id}/restart)를 실제 Postgres로 검사한다.

`MIGRATION_TEST_DATABASE_URL`(버려도 되는 로컬·CI Postgres)과 psql이 있을 때만 돈다.
되돌리기는 산출물을 지우지 않고 지금 것에서만 뺀다.
그 판정이 모두 SQL이라 메모리 저장소로는 못 본다.
- intake: 접수 단계가 되고 카드 조회 404·판단 조회 null.
  행은 그대로 남고 다음 카드는 이어지는 버전이다
- judgment: 원본 카드를 확인 전 상태의 새 버전으로 다시 만들어 사실 카드 단계가 된다
- 분석·판단이 진행 중이면 409, 담당자가 아니면 403
- 되돌리기 칼럼이 없는 DB(마이그레이션 적용 전)에서도 조회는 돌고 intake만 503이다
"""

import asyncio
import shutil
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from psycopg.types.json import Json

from app import db
from app.api import analysis, cases, facts, judgments
from app.api.errors import ApiException
from app.workflow import runs
from tests.api.test_case_assignees_db import OTHER, PROPERTY, cases_on, temp_database
from tests.migrations.test_migrations_apply import ADMIN_URL, MIGRATIONS

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or shutil.which("psql") is None,
    reason="MIGRATION_TEST_DATABASE_URL과 psql이 있어야 한다(supabase/migrations/README.md)",
)

RESTART_MIGRATION = "20261117160000_case_restart.sql"


def run(coro):
    return asyncio.run(coro)


@contextmanager
def services_on(url: str):
    """사건·카드·분석·판단 서비스가 그 DB에 붙게 한다."""
    old = (facts.services, analysis.services, judgments.services)

    def connect():
        return db.connect(url)

    facts.configure(connect=connect)
    analysis.configure(connect=connect)
    judgments.configure(connect=connect)
    try:
        with cases_on(url):
            yield url
    finally:
        facts.services, analysis.services, judgments.services = old


@pytest.fixture(scope="module")
def database_url():
    with temp_database(MIGRATIONS) as url:
        yield url


@pytest.fixture
def service(database_url):
    with services_on(database_url) as url:
        yield url


def vision_fields() -> dict:
    def field(value, status="unverified"):
        return {"value": value, "model_value": value, "source": "vision", "model_confidence": 0.9,
                "verification_status": status, "evidence_frame_ids": [],
                "source_document_ids": []}

    return {"accident_type": field("car_to_car"), "night": field(None, "needs_review")}


def new_case(url: str) -> str:
    case_id = str(uuid.uuid4())
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.cases (id, assignee_id) values (%s, %s)", (case_id, PROPERTY)
        )
    return case_id


def add_card(url, case_id, version, *, origin="vision", status="draft", fields=None) -> None:
    card = {
        "case_id": case_id, "version": version, "accident_date": None,
        "fields": fields if fields is not None else vision_fields(),
        "party_mapping": {"video_ego_party_id": "P1", "insured_party_id": "P1",
                          "chart_party_A_id": "P2", "chart_party_B_id": "P1"},
        "parties": [], "evidence_frames": [], "status": status,
        "confirmed_by": PROPERTY if status == "confirmed" else None,
        "confirmed_at": "2026-11-17T00:00:00+00:00" if status == "confirmed" else None,
    }
    actor = None if origin == "vision" else PROPERTY
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.fact_cards (case_id, version, card, origin, reason, created_by)"
            " values (%s, %s, %s, %s, %s, %s)",
            (case_id, version, Json(card), origin, "수정" if origin == "edit" else None, actor),
        )


def add_run(url, case_id, judgment_version, card_version, status="completed") -> None:
    result = {
        "versions": {"case_id": case_id, "fact_card_version": card_version,
                     "judgment_version": judgment_version, "report_version": None},
        "status": "needs_confirmation", "chart_id": None, "edition": None, "fault_A": None,
        "fault_B": None, "calculation_log": [], "evidence": [], "checks": [], "questions": [],
        "reasons": [],
    }
    done = status == "completed"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.judgment_runs (case_id, judgment_version, fact_card_version,"
            " criteria_version, rule_version, status, result, requested_by, completed_at)"
            " values (%s, %s, %s, 'c', 'r', %s, %s, %s, case when %s then now() end)",
            (case_id, judgment_version, card_version, status,
             Json(result) if done else None, PROPERTY, done),
        )


def add_analysis(url, case_id, status="completed", card_version=None) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.analysis_tasks (case_id, storage_path, idempotency_key, status,"
            " callback_token_hash, fact_card_version, requested_by, completed_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, case when %s then now() end)",
            (case_id, f"case-files/{case_id}/videos/{uuid.uuid4()}.mp4", str(uuid.uuid4()), status,
             "a" * 64, card_version, PROPERTY, status == "completed"),
        )


def counts(url, case_id) -> tuple[int, int, int]:
    with psycopg.connect(url, autocommit=True) as conn:
        return tuple(  # type: ignore[return-value]
            conn.execute(f"select count(*) from public.{t} where case_id = %s", (case_id,))
            .fetchone()[0]
            for t in ("fact_cards", "judgment_runs", "analysis_tasks")
        )


def error(exc_info) -> tuple[int, str]:
    return exc_info.value.status_code, exc_info.value.error["code"]


def judged_case(url) -> str:
    """영상 분석 카드 v1 → 담당자 수정 v2(확정) → 판단까지 간 사건."""
    case_id = new_case(url)
    add_card(url, case_id, 1)
    add_analysis(url, case_id, card_version=1)
    edited = vision_fields()
    edited["night"] = {**edited["night"], "value": True, "source": "manual",
                       "verification_status": "human_confirmed"}
    edited["accident_type"]["verification_status"] = "human_confirmed"
    add_card(url, case_id, 2, origin="edit", status="confirmed", fields=edited)
    add_run(url, case_id, 1, 2)
    return case_id


def test_restart_intake_goes_back_to_intake_and_keeps_history(service):
    case_id = judged_case(service)
    assert run(cases.get_case(PROPERTY, case_id))["stage"] == "judgment"
    before = counts(service, case_id)

    out = run(cases.restart_case(PROPERTY, case_id, "intake"))

    assert (out["stage"], out["task_status"], out["judgment_status"]) == ("intake", None, None)
    assert out["versions"]["fact_card_version"] == 0
    assert out["versions"]["judgment_version"] is None
    assert run(cases.get_case(PROPERTY, case_id))["stage"] == "intake"
    assert [c["stage"] for c in run(cases.list_cases(PROPERTY)) if c["case_id"] == case_id] == [
        "intake"
    ]
    assert counts(service, case_id) == before, "되돌려도 행은 지우지 않는다"
    with pytest.raises(ApiException) as exc:
        run(facts.get_fact_card(PROPERTY, case_id))
    assert error(exc) == (404, "not_found")
    assert run(judgments.get_judgment(PROPERTY, case_id)) is None

    async def no_evidence():
        async with db.connect(service) as conn:
            return await analysis.PostgresAnalysisRepo(conn).latest_completed(case_id)

    assert run(no_evidence()) is None


def test_cards_after_restart_continue_the_version_numbers(service):
    case_id = judged_case(service)
    run(cases.restart_case(PROPERTY, case_id, "intake"))

    blank = run(facts.start_without_video(PROPERTY, case_id))

    assert (blank["version"], blank["status"]) == (3, "draft")
    assert run(facts.get_fact_card(PROPERTY, case_id))["version"] == 3
    assert run(cases.get_case(PROPERTY, case_id))["stage"] == "facts"

    async def store():
        async with db.connect(service) as conn:
            card = {"fields": vision_fields(), "party_mapping": {}, "evidence_frames": []}
            return await facts.store_vision_card(facts.PostgresFactRepo(conn), case_id, card)

    assert run(store())["version"] == 4


def test_restart_judgment_recreates_the_origin_card_as_a_new_draft(service):
    case_id = judged_case(service)

    out = run(cases.restart_case(PROPERTY, case_id, "judgment"))

    assert (out["stage"], out["versions"]["fact_card_version"]) == ("facts", 3)
    card = run(facts.get_fact_card(PROPERTY, case_id))
    assert (card["version"], card["status"], card["confirmed_by"]) == (3, "draft", None)
    assert card["fields"] == vision_fields(), "담당자가 고친 값이 아니라 분석이 만든 처음 그대로다"
    assert run(judgments.get_judgment(PROPERTY, case_id)) is None
    assert counts(service, case_id) == (3, 1, 1)
    with psycopg.connect(service, autocommit=True) as conn:
        row = conn.execute(
            "select origin, reason, created_by from public.fact_cards"
            " where case_id = %s and version = 3", (case_id,),
        ).fetchone()
    assert row == ("edit", facts.RESTART_REASON, uuid.UUID(PROPERTY))


def test_restart_judgment_undoes_a_confirmation_made_on_the_origin_card(service):
    """원본 카드를 고치지 않고 바로 확정했으면 그 행이 확정 상태다 — 확인 표시만 되돌린다."""
    case_id = new_case(service)
    confirmed = {"accident_type": {**vision_fields()["accident_type"],
                                   "verification_status": "human_confirmed"}}
    add_card(service, case_id, 1, status="confirmed", fields=confirmed)
    add_run(service, case_id, 1, 1)

    run(cases.restart_case(PROPERTY, case_id, "judgment"))

    card = run(facts.get_fact_card(PROPERTY, case_id))
    assert (card["version"], card["status"]) == (2, "draft")
    assert card["fields"]["accident_type"]["verification_status"] == "unverified"
    assert card["fields"]["accident_type"]["value"] == "car_to_car"


def test_restart_judgment_keeps_an_untouched_origin_card(service):
    case_id = new_case(service)
    add_card(service, case_id, 1)

    out = run(cases.restart_case(PROPERTY, case_id, "judgment"))

    assert (out["stage"], out["versions"]["fact_card_version"]) == ("facts", 1)
    assert counts(service, case_id)[0] == 1, "이미 처음 상태면 새 버전을 만들지 않는다"


def test_restart_judgment_needs_a_card(service):
    case_id = new_case(service)
    with pytest.raises(ApiException) as exc:
        run(cases.restart_case(PROPERTY, case_id, "judgment"))
    assert error(exc) == (409, "fact_card_required")
    done = judged_case(service)
    run(cases.restart_case(PROPERTY, done, "intake"))
    with pytest.raises(ApiException) as exc:
        run(cases.restart_case(PROPERTY, done, "judgment"))
    assert error(exc) == (409, "fact_card_required"), "접수로 되돌린 사건의 옛 카드는 쓰지 않는다"


@pytest.mark.parametrize("stage", ["intake", "judgment"])
def test_restart_waits_for_running_work_and_checks_the_assignee(service, stage):
    analyzing = judged_case(service)
    add_analysis(service, analyzing, status="running")
    with pytest.raises(ApiException) as exc:
        run(cases.restart_case(PROPERTY, analyzing, stage))
    assert error(exc) == (409, "analysis_in_progress")

    judging = new_case(service)
    add_card(service, judging, 1, status="confirmed", fields={})
    add_run(service, judging, 1, 1, status="running")
    with pytest.raises(ApiException) as exc:
        run(cases.restart_case(PROPERTY, judging, stage))
    assert error(exc) == (409, "judgment_in_progress")
    assert run(cases.get_case(PROPERTY, judging))["stage"] == "judgment", "막히면 바꾸지 않는다"

    with pytest.raises(ApiException) as exc:
        run(cases.restart_case(OTHER, judged_case(service), stage))
    assert error(exc) == (403, "forbidden")
    with pytest.raises(ApiException) as exc:
        run(cases.restart_case(PROPERTY, judged_case(service), "report"))
    assert error(exc) == (422, "invalid_restart_stage")


def test_latest_run_skips_superseded_runs_only_after_restart(service):
    """되돌리기 전에는 판단 조회가 그대로다 — 최신 실행을 돌려준다."""
    case_id = judged_case(service)
    assert run(judgments.get_judgment(PROPERTY, case_id))["judgment_version"] == 1

    async def latest():
        async with db.connect(service) as conn:
            return await runs.latest_run(conn, case_id)

    run(cases.restart_case(PROPERTY, case_id, "judgment"))
    assert run(latest()) is None


def test_reads_work_before_the_restart_migration_is_applied():
    """코드가 마이그레이션보다 먼저 배포돼도 조회는 돌고, 접수로 되돌리기만 503이다."""
    older = [m for m in MIGRATIONS if m.name != RESTART_MIGRATION]
    assert len(older) == len(MIGRATIONS) - 1
    with temp_database(older) as url, services_on(url):
        case_id = judged_case(url)
        assert run(cases.get_case(PROPERTY, case_id))["stage"] == "judgment"
        assert run(facts.get_fact_card(PROPERTY, case_id))["version"] == 2
        with pytest.raises(ApiException) as exc:
            run(cases.restart_case(PROPERTY, case_id, "intake"))
        assert error(exc) == (503, "restart_db_not_ready")
        assert run(cases.get_case(PROPERTY, case_id))["stage"] == "judgment"
        assert run(cases.restart_case(PROPERTY, case_id, "judgment"))["stage"] == "facts"
