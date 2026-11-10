"""D: 보고서 저장소(PostgresReportRepository)를 실제 Postgres(마이그레이션 적용)로 검사한다.

`MIGRATION_TEST_DATABASE_URL`(버려도 되는 로컬·CI Postgres)과 psql이 있을 때만 돈다.
임시 데이터베이스에 supabase_stub.sql → 모든 마이그레이션을 적용하고, 사건·판단 행은 SQL로 넣는다.
요청마다 메모리가 사라지는 Vercel처럼 단계마다 새 저장소 객체로 다시 불러온다.
"""

import asyncio
import copy
import json
import shutil
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import db
from app.api import reports as api
from app.api.errors import ApiException
from app.calculation.rules import RuleSet
from app.reports.repository import PostgresReportRepository, VersionTaken
from tests import fixtures_d as fx
from tests.migrations.test_migrations_apply import ADMIN_URL, HERE, MIGRATIONS, _psql

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or shutil.which("psql") is None,
    reason="MIGRATION_TEST_DATABASE_URL과 psql이 있어야 한다(supabase/migrations/README.md)",
)

ACTOR = str(uuid.uuid4())
OTHER = str(uuid.uuid4())
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}
D_MIGRATION = "20261111090000_review_reports.sql"


def _temp_database(prefix: str, migrations) -> tuple[str, str]:
    parts = urlsplit(ADMIN_URL)
    if "supabase" in (parts.hostname or ""):
        pytest.fail("버려도 되는 로컬 Postgres 주소만 쓴다(Supabase 주소 금지)")
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    created = _psql(ADMIN_URL, "-c", f'create database "{name}"')
    assert created.returncode == 0, created.stderr
    url = urlunsplit(parts._replace(path=f"/{name}"))
    assert _psql(url, "-f", str(HERE / "supabase_stub.sql")).returncode == 0
    for migration in migrations:
        applied = _psql(url, "--single-transaction", "-f", str(migration))
        assert applied.returncode == 0, f"{migration.name}\n{applied.stderr}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("insert into auth.users (id) values (%s), (%s)", (ACTOR, OTHER))
    return name, url


def _drop(name: str) -> None:
    _psql(ADMIN_URL, "-c", f'drop database if exists "{name}" with (force)')


@pytest.fixture(scope="module")
def database_url():
    name, url = _temp_database("okm_reports", MIGRATIONS)
    try:
        yield url
    finally:
        _drop(name)


@pytest.fixture
def store(database_url):
    """단계마다 새 저장소 객체를 쓰게 하는 함수. 서비스는 마지막으로 바꾼 저장소를 쓴다."""

    def fresh() -> PostgresReportRepository:
        repo = PostgresReportRepository(lambda: db.connect(database_url))
        api.configure(repo=repo)
        return repo

    api.configure(rules=RuleSet(version="reviewed-test"), fetch_clauses=None)
    fresh()
    yield fresh
    api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)


def run(coro):
    return asyncio.run(coro)


def rows(url: str, sql: str, *params: object) -> list[dict]:
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def new_case(url: str, assignee: str = ACTOR) -> str:
    case_id = str(uuid.uuid4())
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.cases (id, assignee_id) values (%s, %s)", (case_id, assignee)
        )
    return case_id


def judged(url: str, case_id: str, status: str = "draft_ready", version: int = 1):
    """B가 끝낸 판단 실행 1건(judgment_runs)을 넣고, 같은 버전의 (카드, 판단 결과)를 돌려준다."""
    card = {**fx.card(), "case_id": case_id}
    result = fx.judgment(status, judgment_version=version)
    result["versions"] = {**result["versions"], "case_id": case_id}
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.judgment_runs (case_id, judgment_version, fact_card_version,"
            " criteria_version, rule_version, status, stage, result, requested_by, completed_at)"
            " values (%s, %s, %s, 'synthetic-criteria', 'synthetic-rule', 'completed',"
            " 'build_judgment_result', %s, %s, now())",
            (case_id, version, card["version"], Jsonb(result), ACTOR),
        )
    return card, result


def api_error(call) -> tuple[int, str]:
    with pytest.raises(ApiException) as caught:
        run(call())
    return caught.value.status_code, caught.value.error["code"]


def test_full_flow_survives_new_repository_each_step(database_url, store):
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)

    v1 = run(api.submit_judgment(ACTOR, card, result))
    assert v1["versions"]["report_version"] == 1
    assert v1["calculation"]["status"] == "needs_documents"

    store()  # 새 요청: 메모리 없이 DB에서 다시 불러온다
    docs = [fx.contract(), fx.estimate(), *fx.evidence()]
    v2 = run(api.add_documents(ACTOR, case_id, docs, NO_FLAGS))
    assert v2["versions"]["report_version"] == 2
    assert v2["calculation"]["insurance_payment"] == 9_500_000

    store()
    same = run(api.add_documents(ACTOR, case_id, docs, NO_FLAGS))  # 같은 서류 재전송 → 그대로
    assert same["versions"]["report_version"] == 2

    store()
    confirmed = run(api.confirm_report(ACTOR, case_id, 2, counterparty=fx.counterparty()))
    assert confirmed["package"]["report_version"] == 2
    assert confirmed["report"]["approval_status"] == "approved"

    store()
    claim = run(api.get_claim(ACTOR, case_id, 2))
    assert claim["package"] == confirmed["package"]
    assert claim["html"] == confirmed["html"]
    report = run(api.get_report(ACTOR, case_id, 2))
    assert (report["approval_status"], report["approved_by"]) == ("approved", ACTOR)
    # 키 순서까지 그대로(jsonb면 순서가 바뀌어 보고서 본문·청구서 HTML 항목 순서가 달라진다)
    assert json.dumps(report, ensure_ascii=False) == json.dumps(
        confirmed["report"], ensure_ascii=False
    )
    assert json.dumps(claim["package"], ensure_ascii=False) == json.dumps(
        confirmed["package"], ensure_ascii=False
    )

    store()
    again = run(api.confirm_report(ACTOR, case_id, 2, counterparty=fx.counterparty()))
    assert again["package"] == confirmed["package"]  # 같은 입력 → 같은 청구서, 새 행 없음

    counts = rows(
        database_url,
        "select (select count(*) from public.review_reports where case_id = %s) as reports,"
        " (select count(*) from public.case_documents where case_id = %s) as documents,"
        " (select count(*) from public.report_case_inputs where case_id = %s) as inputs,"
        " (select count(*) from public.claim_packages where case_id = %s) as packages",
        case_id, case_id, case_id, case_id,
    )[0]
    assert counts == {"reports": 2, "documents": len(docs), "inputs": 1, "packages": 1}
    saved = rows(
        database_url,
        "select case_facts, fault_agreement from public.report_case_inputs where case_id = %s",
        case_id,
    )[0]
    assert saved == {"case_facts": NO_FLAGS, "fault_agreement": {}}


def test_agreed_fault_and_document_order_persist(database_url, store):
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))
    store()
    run(api.add_documents(ACTOR, case_id, [fx.contract(), fx.estimate()], NO_FLAGS))
    store()
    v3 = run(api.set_agreed_fault(
        ACTOR, case_id, agreed_counterparty_fault=60, fault_change_reason="합의서 반영"
    ))
    assert v3["recovery"]["agreed_counterparty_fault"] == 60

    rec = run(store().get(ACTOR, case_id))
    assert rec.fault_agreement == {
        "agreed_counterparty_fault": 60, "fault_change_reason": "합의서 반영"
    }
    assert [d["kind"] for d in rec.documents] == ["own_damage_contract", "repair_estimate"]
    assert rec.card == card  # 재산정에 쓰는 카드도 다시 불러온다
    assert [r["versions"]["report_version"] for r in rec.reports] == [1, 2, 3]


def test_access_checks_match_judgment_service(database_url, store):
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))

    assert api_error(lambda: api.get_report(OTHER, case_id, 1)) == (403, "forbidden")
    assert api_error(lambda: api.submit_judgment(OTHER, card, result)) == (403, "forbidden")
    assert api_error(lambda: api.get_report(ACTOR, str(uuid.uuid4()), 1)) == (404, "not_found")
    assert api_error(lambda: api.get_report(ACTOR, "not-a-uuid", 1)) == (404, "not_found")
    assert api_error(lambda: api.get_report(ACTOR, case_id, 9)) == (404, "not_found")
    # 사건은 A가 만든다. 없는 사건에 판단을 넘기면 만들지 않고 404
    ghost = {**card, "case_id": str(uuid.uuid4())}
    assert api_error(lambda: api.submit_judgment(ACTOR, ghost, result)) == (404, "not_found")


def test_stale_and_concurrent_versions_are_409(database_url, store):
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))
    run(api.add_documents(ACTOR, case_id, [fx.contract(), fx.estimate()], NO_FLAGS))
    assert api_error(
        lambda: api.confirm_report(ACTOR, case_id, 1)
    ) == (409, "version_conflict")

    # 두 요청이 같은 v3을 동시에 만들면 늦은 쪽은 아무것도 쓰지 않고 409
    repo = store()
    first, second = run(repo.get(ACTOR, case_id)), run(repo.get(ACTOR, case_id))
    for rec in (first, second):
        v3 = copy.deepcopy(rec.latest)
        v3["versions"]["report_version"] = 3
        rec.reports.append(v3)
    run(repo.save(first, ACTOR))
    second.documents.append({**fx.estimate(), "document_id": "synthetic-late-doc"})
    with pytest.raises(VersionTaken):
        run(repo.save(second, ACTOR))
    docs = rows(
        database_url,
        "select document_id from public.case_documents where case_id = %s", case_id,
    )
    assert "synthetic-late-doc" not in {d["document_id"] for d in docs}  # 트랜잭션 전체 취소


def test_invalid_documents_are_422_and_not_saved(database_url, store):
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))
    bad = [{"document_id": "x-1", "kind": "payment_record"}]
    assert api_error(
        lambda: api.add_documents(ACTOR, case_id, bad)
    ) == (422, "invalid_documents")
    assert rows(
        database_url, "select 1 from public.case_documents where case_id = %s", case_id
    ) == []


def test_browser_roles_read_only_own_cases(database_url, store):
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))
    run(api.add_documents(ACTOR, case_id, [fx.contract(), fx.estimate()], NO_FLAGS))
    run(api.confirm_report(ACTOR, case_id, 2, counterparty=fx.counterparty()))

    tables = ("review_reports", "case_documents", "report_case_inputs", "claim_packages")

    def as_role(role: str, sub: str | None, sql: str):
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            claims = "" if sub is None else f'{{"sub": "{sub}", "role": "{role}"}}'
            conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
            conn.execute(f"set local role {role}")
            return conn.execute(sql).fetchall()

    for table in tables:
        mine = as_role(
            "authenticated", ACTOR,
            f"select count(*) as n from public.{table} where case_id = '{case_id}'",
        )
        others = as_role(
            "authenticated", OTHER,
            f"select count(*) as n from public.{table} where case_id = '{case_id}'",
        )
        assert mine[0]["n"] >= 1, table
        assert others[0]["n"] == 0, table
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_role("anon", None, f"select count(*) from public.{table}")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_role(
            "authenticated", ACTOR,
            f"update public.review_reports set report = report where case_id = '{case_id}'",
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_role(
            "authenticated", ACTOR,
            f"delete from public.claim_packages where case_id = '{case_id}'",
        )


def test_tables_missing_is_503_reports_db_not_ready():
    """마이그레이션 적용 전(판단 테이블까지만) 공용 DB를 흉내 낸다."""
    before = [m for m in MIGRATIONS if m.name < D_MIGRATION]
    name, url = _temp_database("okm_reports_old", before)
    try:
        case_id = new_case(url)
        api.configure(repo=PostgresReportRepository(lambda: db.connect(url)))
        status, code = api_error(lambda: api.get_report(ACTOR, case_id, 1))
        assert (status, code) == (503, "reports_db_not_ready")
    finally:
        api.configure(repo=api.InMemoryReportRepository())
        _drop(name)
