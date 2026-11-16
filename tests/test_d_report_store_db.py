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
from psycopg.types.json import Json, Jsonb

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
# 사건 접근 검사가 읽는 칼럼(대인 담당자). D 표와 무관하게 cases만 고친다
CASE_ACCESS_MIGRATION = "20261113190000_case_injury_assignee.sql"


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
    """B가 끝낸 판단 실행 1건(judgment_runs)을 넣고, 같은 버전의 (카드, 판단 결과)를 돌려준다.

    판단·보고서는 실제 카드 버전만 가리키므로(카드 버전 외래키) A의 카드 버전 행을 먼저 넣는다.
    """
    card = {**fx.card(), "case_id": case_id}
    result = fx.judgment(status, judgment_version=version)
    result["versions"] = {**result["versions"], "case_id": case_id}
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "insert into public.fact_cards (case_id, version, card, origin, created_by)"
            " values (%s, %s, %s, 'manual', %s) on conflict (case_id, version) do nothing",
            (case_id, card["version"], Json(card), ACTOR),
        )
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


def test_claim_c1_reused_across_report_versions_persists(database_url, store):
    """#194: 대물·대인 입력만 바뀐 v3을 확인해도 C1(v2 기준)을 쓰고, 그 기록이 DB에 남는다.

    확인 상태가 그대로인 채 쓰는 청구서만 바뀌어도(C1 → C2 → C1) 보고서 행을 다시 쓴다.
    """
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))
    store()
    run(api.add_documents(ACTOR, case_id, [fx.contract(), fx.estimate(), *fx.evidence()], NO_FLAGS))
    store()
    c1 = run(api.confirm_report(ACTOR, case_id, 2, counterparty=fx.counterparty()))["package"]
    person = {
        "kind": "injured_person", "document_id": "synthetic-inj-1", "person_id": "synthetic-p",
        "grade": 7, "treatment_cost": 3_000_000, "received_at": "2026-10-26T10:00:00+09:00",
    }
    store()
    v3 = run(api.add_documents(ACTOR, case_id, [person]))
    assert v3["versions"]["report_version"] == 3

    def confirm3(counterparty):
        store()
        return run(api.confirm_report(ACTOR, case_id, 3, counterparty=counterparty))

    def claim3():
        store()
        return run(api.get_claim(ACTOR, case_id, 3))["package"]

    def packages():
        return rows(
            database_url,
            "select package_version, report_version from public.claim_packages"
            " where case_id = %s order by package_version",
            case_id,
        )

    def saved_ref():
        return rows(
            database_url,
            "select approval_status, report->'claim_package' as ref from public.review_reports"
            " where case_id = %s and report_version = 3",
            case_id,
        )[0]

    out = confirm3(fx.counterparty())
    assert (out["claim_reused"], out["claim_reused_from"]) == (
        True, {"package_version": 1, "report_version": 2},
    )
    assert claim3() == c1
    assert packages() == [{"package_version": 1, "report_version": 2}]
    assert saved_ref() == {
        "approval_status": "approved", "ref": {"package_version": 1, "report_version": 2},
    }

    out = confirm3({**fx.counterparty(), "claim_no": "synthetic-cp-999"})  # 상대 정보 바뀜 → C2
    assert (out["package"]["package_version"], out["claim_reused"]) == (2, False)
    assert claim3()["package_version"] == 2
    assert saved_ref()["ref"] == {"package_version": 2, "report_version": 3}

    out = confirm3(fx.counterparty())  # 다시 C1과 같은 내용 → C1
    assert out["package"] == c1 and out["claim_reused"] is True
    assert claim3() == c1
    assert saved_ref()["ref"] == {"package_version": 1, "report_version": 2}
    assert packages() == [
        {"package_version": 1, "report_version": 2},
        {"package_version": 2, "report_version": 3},
    ]

    # HTTP 경로로도: GET /cases/{id}/reports/3/claim → 200, C1(v2 기준). DB에서 다시 불러온 값
    from fastapi.testclient import TestClient

    from app.config import Settings, get_settings
    from app.main import app

    store()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="local", on_vercel=False, supabase_url=None, supabase_jwt_secret=None,
        jwt_audience="authenticated", cors_allow_origins=(), dev_actor_id=ACTOR,
    )
    try:
        res = TestClient(app, raise_server_exceptions=False).get(
            f"/cases/{case_id}/reports/3/claim"
        )
    finally:
        app.dependency_overrides.clear()
    assert res.status_code == 200, res.text
    assert res.json()["package"] == json.loads(json.dumps(c1, ensure_ascii=False))
    assert (res.json()["package"]["package_version"], res.json()["package"]["report_version"]) == (
        1, 2,
    )


def test_claim_evidence_excludes_counterparty_estimate_and_medical(database_url, store):
    """#198: DB에서 다시 불러와도 청구서 증빙에 상대 차 견적서(자동 증빙)·진단서가 없고 C1 유지."""
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))
    store()
    run(api.add_documents(ACTOR, case_id, [fx.contract(), fx.estimate(), *fx.evidence()], NO_FLAGS))
    store()
    c1 = run(api.confirm_report(ACTOR, case_id, 2, counterparty=fx.counterparty()))["package"]
    cp_estimate = fx.estimate(3_456_789, doc_id="synthetic-cp-est", party_id="P2")
    cp_auto = {  # upload_estimate가 자동 등록하는 모양: 당사자 칸 없음
        "kind": "evidence", "document_id": "ev-synthetic-cp-est", "doc_type": "repair_estimate",
        "storage_path": "synthetic/synthetic-cp-est.pdf",
    }
    dx = {
        "kind": "evidence", "document_id": "synthetic-dx", "doc_type": "medical_certificate",
        "storage_path": f"case-files/{case_id}/medical/synthetic-dx.pdf",
    }
    store()
    v3 = run(api.add_documents(ACTOR, case_id, [cp_estimate, cp_auto, dx]))
    assert v3["versions"]["report_version"] == 3  # 상대 견적 → 대물 판정이 붙어 새 버전
    assert v3["liability"]["property"]["repair_cost"] == 3_456_789
    store()
    out = run(api.confirm_report(ACTOR, case_id, 3, counterparty=fx.counterparty()))
    assert out["package"] == c1 and out["claim_reused"] is True
    store()
    claim = run(api.get_claim(ACTOR, case_id, 3))["package"]
    files = [e["파일"] for e in claim["evidence"]]
    assert "ev-synthetic-cp-est" not in files and "synthetic-dx" not in files
    assert claim["rows"][0]["evidence_count"] == len(fx.evidence())
    stored = {d["document_id"] for d in run(store().get(ACTOR, case_id)).documents}
    assert {"synthetic-cp-est", "ev-synthetic-cp-est", "synthetic-dx"} <= stored  # 사건 서류엔 남음


def test_liability_inputs_persist(database_url, store):
    """C: 대물·대인 입력(injured_person·property_damage_input)이 kind 제약을 지나 저장·복원된다."""
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))
    person = {
        "kind": "injured_person", "document_id": "synthetic-inj-1", "person_id": "synthetic-p",
        "label": "상대 운전자", "grade": 7, "treatment_cost": 3_000_000, "is_driver": True,
        "received_at": "2026-10-26T10:00:00+09:00",
    }
    limit = {
        "kind": "property_damage_input", "document_id": "synthetic-pd-1",
        "property_damage_limit": 200_000_000, "received_at": "2026-10-26T10:00:00+09:00",
    }
    run(api.add_documents(ACTOR, case_id, [fx.contract(), fx.estimate(), person, limit], NO_FLAGS))
    rec = run(store().get(ACTOR, case_id))
    kinds = [d["kind"] for d in rec.documents]
    assert kinds == ["own_damage_contract", "repair_estimate", "injured_person",
                     "property_damage_input"]
    assert rec.documents[2] == person
    assert rec.latest["liability"]["injuries"][0]["label"] == "상대 운전자"


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


def test_injury_assignee_has_the_same_access(database_url, store):
    """대인 담당자(cases.injury_assignee_id)도 보고서를 읽고 서류·대인 입력을 더한다."""
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))
    assert api_error(lambda: api.get_report(OTHER, case_id, 1)) == (403, "forbidden")
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "update public.cases set injury_assignee_id = %s where id = %s", (OTHER, case_id)
        )

    assert run(api.get_report(OTHER, case_id, 1))["versions"]["report_version"] == 1
    person = {
        "kind": "injured_person", "document_id": "synthetic-inj-2", "person_id": "synthetic-p",
        "label": "상대 운전자", "grade": 7, "treatment_cost": 3_000_000, "is_driver": True,
        "received_at": "2026-10-26T10:00:00+09:00",
    }
    run(api.add_documents(OTHER, case_id, [fx.contract(), fx.estimate(), person], NO_FLAGS))
    # 대물 담당자가 대인 담당자의 입력과 다시 산정한 보고서를 본다
    rec = run(store().get(ACTOR, case_id))
    assert rec.documents[-1] == person
    assert rec.latest["liability"]["injuries"][0]["label"] == "상대 운전자"
    stranger = str(uuid.uuid4())
    assert api_error(lambda: api.get_report(stranger, case_id, 1)) == (403, "forbidden")


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


def test_identity_sequence_is_closed_to_browser_roles(database_url):
    """Supabase 기본 권한이 새 시퀀스에 주는 anon·authenticated 권한을 회수했는지."""
    seq = "public.case_documents_seq_seq"
    for role in ("anon", "authenticated"):
        for privilege in ("USAGE", "SELECT", "UPDATE"):
            granted = rows(
                database_url, "select has_sequence_privilege(%s, %s, %s) as ok",
                role, seq, privilege,
            )[0]["ok"]
            assert granted is False, (role, privilege)
    assert rows(
        database_url, "select has_sequence_privilege('service_role', %s, 'USAGE') as ok", seq
    )[0]["ok"] is True
    # 이 마이그레이션의 시퀀스는 이것 하나(나머지 키는 uuid)
    owned = rows(
        database_url,
        "select c.relname from pg_class c join pg_depend d on d.objid = c.oid"
        " join pg_class t on t.oid = d.refobjid where c.relkind = 'S' and t.relname = any(%s)",
        ["review_reports", "case_documents", "report_case_inputs", "claim_packages"],
    )
    assert [r["relname"] for r in owned] == ["case_documents_seq_seq"]


def test_claim_package_must_match_an_existing_report_version(database_url, store):
    case_id = new_case(database_url)
    card, result = judged(database_url, case_id)
    run(api.submit_judgment(ACTOR, card, result))
    run(api.add_documents(ACTOR, case_id, [fx.contract(), fx.estimate()], NO_FLAGS))  # v2
    package = run(api.confirm_report(ACTOR, case_id, 2, counterparty=fx.counterparty()))["package"]

    def insert(report_version: int, body: dict, case: str = case_id) -> None:
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute(
                "insert into public.claim_packages (case_id, report_version, package_version,"
                " idempotency_key, package, created_by) values (%s, %s, %s, %s, %s, %s)",
                (case, report_version, body["package_version"], body["idempotency_key"],
                 Json(body), ACTOR),
            )

    # 없는 보고서 버전(v9)을 가리키면 외래키 위반
    v9 = {**package, "report_version": 9, "package_version": 2, "idempotency_key": "k-v9"}
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert(9, v9)
    # 칼럼은 v1, 본문은 v2 → 본문 일치 검사 위반
    v1 = {**package, "package_version": 3, "idempotency_key": "k-v1"}
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(1, v1)
    # 다른 사건의 보고서 버전은 가리킬 수 없다(case_id도 외래키에 들어 있음)
    other = new_case(database_url)
    moved = {**package, "case_id": other, "package_version": 4, "idempotency_key": "k-other"}
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert(2, moved, case=other)
    assert rows(
        database_url, "select count(*) as n from public.claim_packages where case_id in (%s, %s)",
        case_id, other,
    )[0]["n"] == 1


def test_tables_missing_is_503_reports_db_not_ready():
    """D 마이그레이션 적용 전(판단 테이블까지만) 공용 DB를 흉내 낸다."""
    before = [m for m in MIGRATIONS if m.name < D_MIGRATION or m.name == CASE_ACCESS_MIGRATION]
    name, url = _temp_database("okm_reports_old", before)
    try:
        case_id = new_case(url)
        api.configure(repo=PostgresReportRepository(lambda: db.connect(url)))
        status, code = api_error(lambda: api.get_report(ACTOR, case_id, 1))
        assert (status, code) == (503, "reports_db_not_ready")
    finally:
        api.configure(repo=api.InMemoryReportRepository())
        _drop(name)
