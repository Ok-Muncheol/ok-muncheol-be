"""B: 판단 접수·질문 응답 서비스를 실제 Postgres(마이그레이션 + 체크포인트 테이블)로 검사한다.

`MIGRATION_TEST_DATABASE_URL`(버려도 되는 로컬·CI Postgres)이 있을 때만 돈다.
CI는 Postgres 서비스를 띄운다. 검색·반박 검토는 가짜로 바꾸고,
저장(실행·질문·응답·검증 로그·멱등성 키·체크포인트)은 진짜 SQL로 확인한다.
"""

import asyncio
import shutil
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg.rows import dict_row

from app import db
from app.api import judgments
from app.api.errors import ApiException
from app.workflow.graph import Dependencies, question_id
from app.workflow.runs import setup_checkpoints
from tests.migrations.test_migrations_apply import ADMIN_URL, HERE, MIGRATIONS, _psql
from tests.workflow.synthetic import candidates, card, field, verdict

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or shutil.which("psql") is None,
    reason="MIGRATION_TEST_DATABASE_URL과 psql이 있어야 한다(supabase/migrations/README.md)",
)

ACTOR = str(uuid.uuid4())
OTHER = str(uuid.uuid4())


@pytest.fixture(scope="module")
def database_url():
    parts = urlsplit(ADMIN_URL)
    if "supabase" in (parts.hostname or ""):
        pytest.fail("버려도 되는 로컬 Postgres 주소만 쓴다(Supabase 주소 금지)")
    name = f"okm_judgment_{uuid.uuid4().hex[:12]}"
    created = _psql(ADMIN_URL, "-c", f'create database "{name}"')
    assert created.returncode == 0, created.stderr
    url = urlunsplit(parts._replace(path=f"/{name}"))
    try:
        assert _psql(url, "-f", str(HERE / "supabase_stub.sql")).returncode == 0
        for migration in MIGRATIONS:
            applied = _psql(url, "--single-transaction", "-f", str(migration))
            assert applied.returncode == 0, f"{migration.name}\n{applied.stderr}"
        asyncio.run(_setup(url))
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("insert into auth.users (id) values (%s), (%s)", (ACTOR, OTHER))
        yield url
    finally:
        _psql(ADMIN_URL, "-c", f'drop database if exists "{name}" with (force)')


async def _setup(url: str) -> None:
    async with db.connect(url) as conn:
        await setup_checkpoints(conn)


@pytest.fixture
def service(database_url):
    fake = SimpleNamespace(
        url=database_url,
        retrieve=AsyncMock(return_value=candidates()),
        reviewer=AsyncMock(return_value=verdict()),
    )
    judgments.configure(
        deps=Dependencies(retrieve=fake.retrieve, reviewer=fake.reviewer),
        connect=lambda: db.connect(database_url),
    )
    yield fake
    judgments.configure(deps=Dependencies(), connect=db.connect)


def new_case(url: str, assignee: str = ACTOR) -> str:
    case_id = str(uuid.uuid4())
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.cases (id, assignee_id) values (%s, %s)", (case_id, assignee)
        )
    return case_id


def rows(url: str, sql: str, *params: object) -> list[dict]:
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def start(actor: str, card_, key: str) -> dict:
    return asyncio.run(judgments.start_judgment(actor, card_, key))


def answer(case_id: str, qid: str, value: str | None, version: int = 3) -> dict:
    body = {"question_id": qid, "expected_card_version": version, "value": value,
            "unknown": value is None}
    return asyncio.run(judgments.answer_question(ACTOR, case_id, body))


def api_error(call) -> tuple[int, str]:
    with pytest.raises(ApiException) as caught:
        call()
    return caught.value.status_code, caught.value.error["code"]


def unconfirmed(case_id: str, *keys: str):
    c = card(case_id=case_id)
    for key in keys:
        c["fields"][key] = field(c["fields"][key]["value"], confirmed=False)
    return c


def test_draft_is_stored_and_requests_are_idempotent(service):
    case_id = new_case(service.url)
    c = card(case_id=case_id)
    view = start(ACTOR, c, "key-0000001")
    assert view == {"task_id": view["task_id"], "case_id": case_id,
                    "stage": "build_judgment_result", "status": "completed", "input_version": 3}
    [run] = rows(service.url, "select * from public.judgment_runs where case_id = %s", case_id)
    stored = tuple(run[k] for k in ("status", "judgment_status", "chart_id", "fault_a", "fault_b"))
    assert stored == ("completed", "draft_ready", "S-1", 20, 80)
    assert (run["judgment_version"], run["fact_card_version"]) == (1, 3)
    assert run["result"]["versions"]["judgment_version"] == 1
    checks = rows(service.url, "select attempt, layer, passed from public.validation_checks"
                  " where run_id = %s order by layer", run["id"])
    assert [(c["attempt"], c["layer"], c["passed"]) for c in checks] == [
        (0, "evidence", True), (0, "numerical", True), (0, "rebuttal", True)]
    saved = rows(service.url, "select count(*) as n from langgraph.checkpoints"
                 " where thread_id = %s", view["task_id"])
    assert saved[0]["n"] > 0
    # 같은 키 = 저장한 응답, 새 키·같은 입력 = 기존 실행(다시 판단하지 않음)
    assert start(ACTOR, c, "key-0000001") == view
    assert start(ACTOR, c, "key-0000002")["task_id"] == view["task_id"]
    assert service.retrieve.await_count == 1
    assert api_error(lambda: start(ACTOR, {**c, "version": 4}, "key-0000001")) \
        == (422, "idempotency_key_reused")


def test_unknown_answer_resumes_the_same_run_to_deferred(service):
    case_id = new_case(service.url)
    view = start(ACTOR, unconfirmed(case_id, "movement_B"), "key-0000003")
    assert (view["status"], view["stage"]) == ("waiting_for_confirmation", "ask_or_resume")
    [q] = rows(service.url, "select id, field_key, options from public.judgment_questions"
               " where run_id = %s", view["task_id"])
    assert str(q["id"]) == question_id(view["task_id"], "movement_B")
    assert "left_turn" in q["options"]
    out = answer(case_id, str(q["id"]), None)
    assert (out["task_id"], out["status"], out["next_action"]) \
        == (view["task_id"], "completed", None)
    [run] = rows(service.url, "select status, judgment_status, result from public.judgment_runs"
                 " where id = %s", view["task_id"])
    assert (run["status"], run["judgment_status"]) == ("completed", "deferred")
    assert run["result"]["reasons"] == ["도표 선택 칸 미확인: movement_B"]
    [saved] = rows(service.url, "select unknown, value, answered_by from public.judgment_answers")
    assert (saved["unknown"], saved["value"], str(saved["answered_by"])) == (True, None, ACTOR)


def test_value_answer_goes_to_card_update_and_new_version_supersedes(service):
    case_id = new_case(service.url)
    first = start(ACTOR, unconfirmed(case_id, "movement_B"), "key-0000004")
    [q] = rows(service.url, "select id from public.judgment_questions where run_id = %s",
               first["task_id"])
    out = answer(case_id, str(q["id"]), "left_turn")
    assert (out["status"], out["next_action"], out["field_values"]) \
        == ("waiting_for_confirmation", "update_fact_card", {"movement_B": "left_turn"})
    # A의 카드 수정·재확정으로 버전 4가 생겼다고 보고 새 판단을 요청한다
    second = start(ACTOR, card(case_id=case_id, version=4), "key-0000005")
    assert (second["status"], second["input_version"]) == ("completed", 4)
    old, new = rows(service.url, "select id, superseded_at, resumed_from_run_id, judgment_version"
                    " from public.judgment_runs where case_id = %s order by judgment_version",
                    case_id)
    assert old["superseded_at"] is not None and new["resumed_from_run_id"] == old["id"]
    assert new["judgment_version"] == 2
    [saved] = rows(service.url, "select resulting_card_version from public.judgment_answers"
                   " where question_id = %s", q["id"])
    assert saved["resulting_card_version"] == 4
    assert api_error(lambda: answer(case_id, str(q["id"]), "left_turn")) \
        == (422, "question_closed")


def test_wrong_chart_retry_is_logged_with_attempts(service):
    c = card(case_id=new_case(service.url))
    c["fields"]["movement_B"] = field("straight")
    cands = candidates()
    cands[0]["attributes"]["movement_B"] = "straight"
    service.retrieve.return_value = cands
    service.reviewer.side_effect = [verdict(True, "wrong_chart", "S-2"), verdict()]
    view = start(ACTOR, c, "key-0000006")
    [run] = rows(service.url, "select chart_id, rebuttal_retry_count from public.judgment_runs"
                 " where id = %s", view["task_id"])
    assert (run["chart_id"], run["rebuttal_retry_count"]) == ("S-2", 1)
    checks = rows(service.url, "select attempt, layer, passed from public.validation_checks"
                  " where run_id = %s order by attempt, layer", view["task_id"])
    assert [(c["attempt"], c["layer"], c["passed"]) for c in checks] == [
        (0, "evidence", True), (0, "rebuttal", False),
        (1, "evidence", True), (1, "numerical", True), (1, "rebuttal", True)]


def test_access_and_answer_checks(service):
    case_id = new_case(service.url)
    assert api_error(lambda: start(OTHER, card(case_id=case_id), "key-0000007")) \
        == (403, "forbidden")
    assert api_error(lambda: start(ACTOR, card(case_id=str(uuid.uuid4())), "key-0000008")) \
        == (404, "not_found")
    view = start(ACTOR, unconfirmed(case_id, "movement_B", "signal_B"), "key-0000009")
    qs = {r["field_key"]: str(r["id"]) for r in rows(
        service.url, "select id, field_key from public.judgment_questions where run_id = %s",
        view["task_id"])}
    assert set(qs) == {"movement_B", "signal_B"}
    assert api_error(lambda: answer(case_id, qs["signal_B"], "purple")) == (422, "invalid_answer")
    assert api_error(lambda: answer(case_id, qs["signal_B"], "red", version=2)) \
        == (409, "version_conflict")
    assert api_error(lambda: answer(str(uuid.uuid4()), qs["signal_B"], "red")) \
        == (404, "not_found")
    out = answer(case_id, qs["signal_B"], "red")
    assert (out["next_action"], out["pending_question_ids"]) == ("answer_remaining",
                                                                 [qs["movement_B"]])
    assert api_error(lambda: answer(case_id, qs["signal_B"], "red")) == (409, "already_answered")


def test_missing_dependency_fails_the_run_and_allows_a_new_one(service):
    case_id = new_case(service.url)
    service.retrieve.side_effect = NotImplementedError("D: 색인 미연결")
    view = start(ACTOR, card(case_id=case_id), "key-0000010")
    assert (view["status"], view["stage"]) == ("failed", "retrieve_chart_candidates")
    [run] = rows(service.url, "select status, error, completed_at from public.judgment_runs"
                 " where id = %s", view["task_id"])
    assert run["status"] == "failed" and run["completed_at"] is not None
    assert run["error"]["code"] == "dependency_not_ready" and run["error"]["retryable"] is False
    # 같은 키는 같은 실패 응답, 연결 뒤 새 키는 새 실행
    assert start(ACTOR, card(case_id=case_id), "key-0000010") == view
    service.retrieve.side_effect = None
    again = start(ACTOR, card(case_id=case_id), "key-0000011")
    assert again["status"] == "completed" and again["task_id"] != view["task_id"]


def test_running_run_blocks_until_it_is_stale(service):
    case_id = new_case(service.url)
    with psycopg.connect(service.url, autocommit=True) as conn:
        conn.execute(
            "insert into public.judgment_runs (case_id, judgment_version, fact_card_version,"
            " criteria_version, rule_version, status, requested_by, updated_at)"
            " values (%s, 1, 2, 'c', 'r', 'running', %s, now())", (case_id, ACTOR))
    assert api_error(lambda: start(ACTOR, card(case_id=case_id), "key-0000012")) \
        == (409, "judgment_in_progress")
    with psycopg.connect(service.url, autocommit=True) as conn:
        # updated_at 트리거를 피하려고 행을 다시 넣는다(insert는 트리거 대상이 아니다)
        conn.execute("delete from public.judgment_runs where case_id = %s", (case_id,))
        conn.execute(
            "insert into public.judgment_runs (case_id, judgment_version, fact_card_version,"
            " criteria_version, rule_version, status, requested_by, updated_at)"
            " values (%s, 1, 2, 'c', 'r', 'running', %s, now() - interval '11 minutes')",
            (case_id, ACTOR))
    view = start(ACTOR, card(case_id=case_id), "key-0000013")
    assert view["status"] == "completed"
    stale, new = rows(service.url, "select status, error from public.judgment_runs"
                      " where case_id = %s order by judgment_version", case_id)
    assert (stale["status"], stale["error"]["code"]) == ("failed", "run_interrupted")
    assert new["status"] == "completed"


def test_http_routes_store_and_serialize(service, monkeypatch):
    """라우트 → 서비스 → DB. A의 카드 조회만 가짜로 바꾸고 로컬 인증 우회로 부른다."""
    from fastapi.testclient import TestClient

    from app.api import facts
    from app.config import Settings, get_settings
    from app.main import app

    case_id = new_case(service.url)
    monkeypatch.setattr(facts, "get_fact_card",
                        AsyncMock(return_value=unconfirmed(case_id, "movement_B")))
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="local", on_vercel=False, supabase_url=None, supabase_jwt_secret=None,
        jwt_audience="authenticated", cors_allow_origins=(), dev_actor_id=ACTOR)
    try:
        client = TestClient(app)
        res = client.post(f"/cases/{case_id}/judgments", json={"card_version": 3},
                          headers={"Idempotency-Key": "http-0000001"})
        assert res.status_code == 202, res.text
        assert res.json()["status"] == "waiting_for_confirmation"
        [q] = rows(service.url, "select id from public.judgment_questions where run_id = %s",
                   res.json()["task_id"])
        body = {"question_id": str(q["id"]), "expected_card_version": 3, "value": None,
                "unknown": True}
        res = client.post(f"/cases/{case_id}/questions/{q['id']}/answer", json=body)
        assert res.status_code == 200, res.text
        assert (res.json()["status"], res.json()["next_action"]) == ("completed", None)
        res = client.post(f"/cases/{case_id}/questions/{q['id']}/answer", json=body)
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "question_closed"
    finally:
        app.dependency_overrides.clear()
