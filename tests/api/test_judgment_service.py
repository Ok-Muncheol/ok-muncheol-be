"""B: 판단 접수·질문 응답 서비스를 실제 Postgres(마이그레이션 + 체크포인트 테이블)로 검사한다.

`MIGRATION_TEST_DATABASE_URL`(버려도 되는 로컬·CI Postgres)이 있을 때만 돈다.
CI는 Postgres 서비스를 띄운다. 검색·반박 검토는 가짜로 바꾸고,
저장(실행·질문·응답·검증 로그·멱등성 키·체크포인트)은 진짜 SQL로 확인한다.
"""

import asyncio
import json
import shutil
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app import db
from app.agents import qa as qa_agent
from app.api import judgments, reports
from app.api.errors import ApiException
from app.reports.repository import InMemoryReportRepository, PostgresReportRepository
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
    # 판정 뒤 만드는 보고서(D submit_judgment)도 같은 테스트 DB에 저장한다
    reports.configure(repo=PostgresReportRepository(connect=lambda: db.connect(database_url)))
    yield fake
    judgments.configure(deps=Dependencies(), connect=db.connect, qa=qa_agent.QaDependencies())
    reports.configure(repo=InMemoryReportRepository())


# 사건마다 미리 넣는 카드 버전. 판단·응답·보고서는 실제 카드 버전만 가리킨다(카드 버전 외래키).
# 합성 카드는 기본 v3이고, 카드를 고친 뒤의 새 판단은 v4, SQL로 직접 넣는 실행은 v2를 쓴다
CARD_VERSIONS = (1, 2, 3, 4)


def new_case(url: str, assignee: str = ACTOR) -> str:
    case_id = str(uuid.uuid4())
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.cases (id, assignee_id) values (%s, %s)", (case_id, assignee)
        )
        for version in CARD_VERSIONS:
            conn.execute(
                "insert into public.fact_cards (case_id, version, card, origin, created_by)"
                " values (%s, %s, %s, 'manual', %s)",
                (case_id, version, Json(card(case_id=case_id, version=version)), assignee),
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


def report_count(url: str, case_id: str) -> int:
    return rows(url, "select count(*) as n from public.review_reports where case_id = %s",
                case_id)[0]["n"]


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
                    "stage": "build_judgment_result", "status": "completed", "input_version": 3,
                    "report_version": 1, "report_error": None}
    # 판정 뒤 보고서 v1이 그 판단 버전으로 만들어진다(D submit_judgment)
    [report] = rows(service.url, "select report_version, judgment_version, fact_card_version"
                    " from public.review_reports where case_id = %s", case_id)
    assert (report["report_version"], report["judgment_version"], report["fact_card_version"]) \
        == (1, 1, 3)
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
    again = start(ACTOR, c, "key-0000002")
    assert (again["task_id"], again["report_version"]) == (view["task_id"], 1)
    assert service.retrieve.await_count == 1
    assert report_count(service.url, case_id) == 1  # 같은 판단으로 보고서를 다시 만들지 않는다
    assert api_error(lambda: start(ACTOR, {**c, "version": 4}, "key-0000001")) \
        == (422, "idempotency_key_reused")


def test_unknown_answer_resumes_the_same_run_to_deferred(service):
    case_id = new_case(service.url)
    view = start(ACTOR, unconfirmed(case_id, "movement_B"), "key-0000003")
    assert (view["status"], view["stage"]) == ("waiting_for_confirmation", "ask_or_resume")
    assert (view["report_version"], report_count(service.url, case_id)) == (None, 0)
    [q] = rows(service.url, "select id, field_key, options from public.judgment_questions"
               " where run_id = %s", view["task_id"])
    assert str(q["id"]) == question_id(view["task_id"], "movement_B")
    assert "left_turn" in q["options"]
    out = answer(case_id, str(q["id"]), None)
    assert (out["task_id"], out["status"], out["next_action"]) \
        == (view["task_id"], "completed", None)
    # 보류도 판정이라 보고서를 만든다(수치는 비운 채로)
    assert (out["report_version"], out["report_error"]) == (1, None)
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


def view(case_id: str, actor: str = ACTOR) -> dict | None:
    return asyncio.run(judgments.get_judgment(actor, case_id))  # type: ignore[return-value]


def test_judgment_view_restores_the_latest_run_with_questions(service):
    case_id = new_case(service.url)
    assert view(case_id) is None  # 판단 전
    first = start(ACTOR, unconfirmed(case_id, "movement_B", "signal_B"), "key-0000041")
    waiting = view(case_id)
    assert waiting is not None
    assert (waiting["task_id"], waiting["status"], waiting["stage"], waiting["judgment_version"],
            waiting["fact_card_version"]) \
        == (first["task_id"], "waiting_for_confirmation", "ask_or_resume", 1, 3)
    # 확인 대기 실행은 result가 비어 있고 질문은 questions에서 읽는다
    assert (waiting["result"], waiting["error"], waiting["report_version"]) == (None, None, None)
    assert [q["field_key"] for q in waiting["questions"]] == ["movement_B", "signal_B"]
    assert all(q["answer"] is None and q["options"] and q["text"] for q in waiting["questions"])
    signal = next(q for q in waiting["questions"] if q["field_key"] == "signal_B")
    answer(case_id, signal["question_id"], "red")
    answers = {q["field_key"]: q["answer"] for q in view(case_id)["questions"]}  # type: ignore[index]
    assert answers["movement_B"] is None
    assert {k: answers["signal_B"][k] for k in ("value", "unknown", "expected_card_version",
                                                "resulting_card_version")} \
        == {"value": "red", "unknown": False, "expected_card_version": 3,
            "resulting_card_version": None}
    assert answers["signal_B"]["answered_at"]
    # 새 카드 버전의 판단이 확인 대기 실행을 대체하면 그 새 실행이 최신이다
    second = start(ACTOR, card(case_id=case_id, version=4), "key-0000042")
    done = view(case_id)
    assert done is not None
    assert (done["task_id"], done["status"], done["judgment_version"], done["fact_card_version"]) \
        == (second["task_id"], "completed", 2, 4)
    assert (done["result"]["status"], done["questions"]) == ("draft_ready", [])  # type: ignore[index]
    assert done["report_version"] == second["report_version"] is not None
    assert api_error(lambda: view(case_id, OTHER)) == (403, "forbidden")
    assert api_error(lambda: view(str(uuid.uuid4()))) == (404, "not_found")
    assert api_error(lambda: view("not-a-uuid")) == (404, "not_found")


def test_missing_dependency_fails_the_run_and_allows_a_new_one(service):
    case_id = new_case(service.url)
    service.retrieve.side_effect = NotImplementedError("D: 색인 미연결")
    view = start(ACTOR, card(case_id=case_id), "key-0000010")
    assert (view["status"], view["stage"]) == ("failed", "retrieve_chart_candidates")
    [run] = rows(service.url, "select status, error, completed_at from public.judgment_runs"
                 " where id = %s", view["task_id"])
    assert run["status"] == "failed" and run["completed_at"] is not None
    assert (view["report_version"], report_count(service.url, case_id)) == (None, 0)
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
        # 판단 조회: 시각은 ISO 문자열로, 답한 질문은 답과 함께 직렬화된다
        res = client.get(f"/cases/{case_id}/judgment")
        assert res.status_code == 200, res.text
        got = res.json()
        assert (got["status"], got["result"]["status"]) == ("completed", "deferred")
        assert isinstance(got["updated_at"], str)
        [question] = got["questions"]
        assert (question["question_id"], question["answer"]["unknown"]) == (str(q["id"]), True)
    finally:
        app.dependency_overrides.clear()


def test_report_failure_keeps_judgment_and_retries_on_same_input(service, monkeypatch):
    case_id = new_case(service.url)
    c = card(case_id=case_id)
    failing = AsyncMock(side_effect=ApiException(
        503, "reports_db_not_ready", "보고서 테이블이 아직 없습니다.", True))
    monkeypatch.setattr(reports, "submit_judgment", failing)
    view = start(ACTOR, c, "key-0000020")
    # 판단은 완료로 저장하고 보고서 실패만 알린다
    assert (view["status"], view["report_version"]) == ("completed", None)
    assert view["report_error"]["code"] == "reports_db_not_ready"
    [run] = rows(service.url, "select status, judgment_status from public.judgment_runs"
                 " where id = %s", view["task_id"])
    assert (run["status"], run["judgment_status"]) == ("completed", "draft_ready")
    monkeypatch.undo()
    # 같은 입력을 새 키로 다시 보내면 판단은 다시 하지 않고 보고서만 만든다
    again = start(ACTOR, c, "key-0000021")
    assert (again["task_id"], again["report_version"], again["report_error"]) \
        == (view["task_id"], 1, None)
    assert service.retrieve.await_count == 1 and report_count(service.url, case_id) == 1


def test_unexpected_report_error_is_reported_without_details(service, monkeypatch):
    case_id = new_case(service.url)
    monkeypatch.setattr(reports, "submit_judgment", AsyncMock(side_effect=KeyError("secret")))
    view = start(ACTOR, card(case_id=case_id), "key-0000022")
    assert view["status"] == "completed"
    assert view["report_error"] == judgments.REPORT_FAILED


# ── 근거 질의응답(ask_question) ───────────────────────────────────────────
def scripted_llm(*replies: dict):
    queue = list(replies)
    seen: list[list[dict]] = []

    async def llm(messages, tools, tool_choice):
        seen.append([dict(m) for m in messages])
        return queue.pop(0)

    llm.seen = seen  # type: ignore[attr-defined]
    return llm


def tool_call(name: str, args: dict, id_: str) -> dict:
    return {"id": id_, "name": name, "arguments": json.dumps(args)}


def stored_counts(url: str, case_id: str) -> tuple[int, int, int]:
    runs_n = rows(url, "select count(*) as n from public.judgment_runs where case_id = %s",
                  case_id)[0]["n"]
    checkpoints_n = rows(url, "select count(*) as n from langgraph.checkpoints")[0]["n"]
    return runs_n, report_count(url, case_id), checkpoints_n


def ask(actor: str, case_id: str, text: str = "왜 이렇게 판단했나요?") -> dict:
    return asyncio.run(judgments.ask_question(actor, case_id, text))


def test_qa_reads_the_official_judgment_and_simulates_without_saving(service):
    case_id = new_case(service.url)
    assert start(ACTOR, card(case_id=case_id), "key-0000091")["status"] == "completed"
    before = stored_counts(service.url, case_id)
    llm = scripted_llm(
        {"content": None, "tool_calls": [
            tool_call("get_judgment_log", {}, "c1"),
            tool_call("simulate", {"changes": {"movement_B": "straight"}}, "c2")]},
        {"content": "공식은 A 20 : B 80입니다 [E1]. 가정하면 A 50 : B 50입니다 [E2].",
         "tool_calls": []},
    )
    judgments.configure(qa=qa_agent.QaDependencies(llm=llm))
    out = ask(ACTOR, case_id, "B가 직진이었다면 어떻게 되나요?")
    assert (out["case_id"], out["judgment_version"]) == (case_id, 1)
    [hypo] = out["hypotheticals"]
    assert (hypo["status"], hypo["judgment_status"], hypo["chart_id"]) \
        == ("completed", "draft_ready", "S-2")
    assert (hypo["fault_A"], hypo["fault_B"], hypo["changes"]) \
        == (50, 50, {"movement_B": "straight"})
    assert [c["parent_id"] for c in out["citations"]] \
        == ["synthetic-standards:S-1", "synthetic-standards:S-2"]
    # 공식 판정의 근거와 가정 실행이 고른 도표의 근거를 출처로 나눈다
    assert [c["source"] for c in out["citations"]] == ["official", "hypothesis"]
    assert out["fact_change_hint"] == qa_agent.FACT_CHANGE_HINT
    assert out["tool_calls"] == [{"name": "get_judgment_log", "ok": True},
                                 {"name": "simulate", "ok": True}]
    # 판단 로그는 공식 판정과 체크포인트에 남은 카드의 확인한 사실을 싣는다
    log = json.loads(next(m["content"] for m in llm.seen[-1] if m["role"] == "tool"))
    assert (log["chart_id"], log["fault_A"], log["fault_B"]) == ("S-1", 20, 80)
    assert log["confirmed_facts"]["movement_B"] == "left_turn"
    # 가정 실행은 실행·보고서·체크포인트를 만들지 않고 공식 판정을 바꾸지 않는다
    assert stored_counts(service.url, case_id) == before
    [run] = rows(service.url, "select chart_id, fault_a, fault_b from public.judgment_runs"
                 " where case_id = %s", case_id)
    assert (run["chart_id"], run["fault_a"], run["fault_b"]) == ("S-1", 20, 80)


def test_qa_needs_a_finished_judgment_the_assignee_and_a_model(service, monkeypatch):
    case_id = new_case(service.url)
    assert api_error(lambda: ask(ACTOR, case_id)) == (409, "judgment_required")
    assert api_error(lambda: ask(OTHER, case_id)) == (403, "forbidden")
    assert api_error(lambda: ask(ACTOR, str(uuid.uuid4()))) == (404, "not_found")
    assert api_error(lambda: ask(ACTOR, "not-a-uuid")) == (404, "not_found")
    waiting = start(ACTOR, unconfirmed(case_id, "movement_B"), "key-0000092")
    assert waiting["status"] == "waiting_for_confirmation"
    assert api_error(lambda: ask(ACTOR, case_id)) == (409, "judgment_required")

    done = new_case(service.url)
    start(ACTOR, card(case_id=done), "key-0000093")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    judgments.configure(qa=qa_agent.QaDependencies())  # 기본 OpenAI 모델, 키 없음
    assert api_error(lambda: ask(ACTOR, done)) == (503, "dependency_not_ready")

    async def broken(messages, tools, tool_choice):
        raise RuntimeError("upstream down")

    judgments.configure(qa=qa_agent.QaDependencies(llm=broken))
    assert api_error(lambda: ask(ACTOR, done)) == (502, "qa_failed")

    async def slow(messages, tools, tool_choice):
        await asyncio.sleep(1)
        return {"content": "늦은 답", "tool_calls": []}

    # 질문 하나의 LLM 호출 시간 합계를 넘으면 504 qa_timeout(다시 질문 가능)
    judgments.configure(qa=qa_agent.QaDependencies(llm=slow, llm_budget_seconds=0.05))
    assert api_error(lambda: ask(ACTOR, done)) == (504, "qa_timeout")


def test_qa_http_route_serializes_the_answer(service):
    from fastapi.testclient import TestClient

    from app.config import Settings, get_settings
    from app.main import app

    case_id = new_case(service.url)
    start(ACTOR, card(case_id=case_id), "key-0000094")
    judgments.configure(qa=qa_agent.QaDependencies(llm=scripted_llm(
        {"content": None, "tool_calls": [tool_call("get_judgment_log", {}, "c1")]},
        {"content": "공식 판정은 A 20 : B 80입니다 [E1].", "tool_calls": []},
    )))
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="local", on_vercel=False, supabase_url=None, supabase_jwt_secret=None,
        jwt_audience="authenticated", cors_allow_origins=(), dev_actor_id=ACTOR)
    try:
        client = TestClient(app)
        res = client.post(f"/cases/{case_id}/qa", json={"text": "왜 B가 80인가요?"})
        assert res.status_code == 200, res.text
        got = res.json()
        assert got["answer"] == "공식 판정은 A 20 : B 80입니다 [E1]."
        assert got["citations"][0]["ref"] == "E1"
        assert (got["hypotheticals"], got["fact_change_hint"]) == ([], None)
        assert client.post(f"/cases/{case_id}/qa", json={"text": ""}).status_code == 422
    finally:
        app.dependency_overrides.clear()
