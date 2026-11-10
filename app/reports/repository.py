"""D: 보고서·산정·구상 버전 저장소.

인터페이스: `await repo.open(case_id, owner_id)`·`await repo.get(actor_id, case_id)`로
사건 기록(CaseRecord)을 불러오고, 서비스가 기록을 고친 뒤
`await repo.save(rec, actor_id)`로 저장한다.

- `InMemoryReportRepository`: 테스트·데모용(DATABASE_URL 없음). 사건이 없으면 open이 만든다.
  save는 할 일이 없다.
- `PostgresReportRepository`: 운영용(DATABASE_URL 있음). 테이블은
  `supabase/migrations/20261111090000_review_reports.sql`. 사건은 A가 만들므로 open이 만들지 않는다.
  사건 접근 검사는 판단 서비스(app/api/judgments.py `_require_assignee`)와 같다:
  uuid가 아니거나 사건이 없으면 RecordNotFound(404), 담당자가 아니면 NotCaseOwner(403).
  연결의 search_path 첫 자리는 LangGraph 스키마라(app/db.py) 모든 SQL에 `public.`을 붙인다.
  원본은 json 칼럼에 Json으로 쓴다(jsonb는 키 순서를 바꿔 보고서·청구서 항목 순서가 달라진다).
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from app.schemas.facts import FactCard
from app.schemas.reports import ClaimPackage, ReviewReport


class RecordNotFound(LookupError):
    """사건·보고서·청구서가 없음. API 경계에서 404로 바뀐다(KeyError 등 내부 오류와 구분)."""


class NotCaseOwner(PermissionError):
    """담당 사건이 아님. API 경계에서 403으로 바뀐다(파일 권한 오류와 구분)."""


class VersionTaken(ValueError):
    """같은 보고서·청구서 버전이 이미 저장됨(동시 요청). API 경계에서 409로 바뀐다."""


class ReportsDbNotReady(RuntimeError):
    """보고서 테이블이 아직 없음(마이그레이션 적용 전). API 경계에서 503으로 바뀐다."""


@dataclass
class _Saved:
    """DB에서 불러온 시점의 상태. save가 새로 생긴 것·바뀐 것만 쓴다."""

    reports: int = 0
    documents: int = 0
    packages: int = 0
    approvals: list[str | None] = field(default_factory=list)
    inputs: tuple[dict[str, object], dict[str, object]] | None = None


@dataclass
class CaseRecord:
    case_id: str
    owner_id: str
    card: FactCard | None = None
    reports: list[ReviewReport] = field(default_factory=list)
    documents: list[dict[str, object]] = field(default_factory=list)
    case_facts: dict[str, object] = field(
        default_factory=dict
    )  # 면책 판단용 사실(무면허·음주·고의 등)
    packages: list[ClaimPackage] = field(default_factory=list)
    # 협의 과실: {"agreed_counterparty_fault": int | None, "fault_change_reason": str}. None 값 =
    # 협의 해제 기록
    fault_agreement: dict[str, object] = field(default_factory=dict)
    saved: _Saved = field(default_factory=_Saved, repr=False, compare=False)

    @property
    def latest(self) -> ReviewReport | None:
        return self.reports[-1] if self.reports else None

    def report(self, version: int) -> ReviewReport:
        for r in self.reports:
            if r["versions"]["report_version"] == version:
                return r
        raise RecordNotFound(f"보고서 v{version}이 없습니다")

    def latest_ready(self) -> ReviewReport | None:
        """최신 보고서가 산정 가능한 판단(draft_ready)이면 그 보고서. 추가 확인·보류면 None."""
        r = self.latest
        return r if r is not None and r["judgment"]["status"] == "draft_ready" else None


class InMemoryReportRepository:
    def __init__(self) -> None:
        self._cases: dict[str, CaseRecord] = {}

    async def open(self, case_id: str, owner_id: str) -> CaseRecord:
        return self._cases.setdefault(case_id, CaseRecord(case_id=case_id, owner_id=owner_id))

    async def get(self, actor_id: str, case_id: str) -> CaseRecord:
        rec = self._cases.get(case_id)
        if rec is None:
            raise RecordNotFound("사건이 없습니다")
        if rec.owner_id != actor_id:
            raise NotCaseOwner("담당 사건이 아닙니다")
        return rec

    async def save(self, rec: CaseRecord, actor_id: str) -> None:
        """메모리 기록은 이미 고쳐져 있다."""


def _case_uuid(case_id: str) -> str:
    try:
        return str(uuid.UUID(str(case_id)))
    except ValueError as exc:
        raise RecordNotFound("사건이 없습니다") from exc


def _inputs(rec: CaseRecord) -> tuple[dict[str, object], dict[str, object]]:
    return dict(rec.case_facts), dict(rec.fault_agreement)


class PostgresReportRepository:
    """Supabase(Postgres) 저장소. 호출마다 연결 하나(app/db.py connect), 저장은 한 트랜잭션."""

    def __init__(
        self, connect: Callable[[], AbstractAsyncContextManager[Any]] | None = None
    ) -> None:
        if connect is None:
            from app.db import connect as db_connect

            connect = db_connect
        self._connect = connect

    async def open(self, case_id: str, owner_id: str) -> CaseRecord:
        """사건은 A가 만든다. 없으면 404, 담당자가 아니면 403."""
        return await self.get(owner_id, case_id)

    async def get(self, actor_id: str, case_id: str) -> CaseRecord:
        import psycopg

        from app.workflow import runs

        case_id = _case_uuid(case_id)
        try:
            async with self._connect() as conn:
                assignee = await runs.case_assignee(conn, case_id)
                if assignee is None:
                    raise RecordNotFound("사건이 없습니다")
                if assignee != actor_id:
                    raise NotCaseOwner("담당 사건이 아닙니다")
                reports = await (await conn.execute(
                    "select report, fact_card from public.review_reports"
                    " where case_id = %s order by report_version",
                    (case_id,),
                )).fetchall()
                documents = await (await conn.execute(
                    "select body from public.case_documents where case_id = %s order by seq",
                    (case_id,),
                )).fetchall()
                inputs = await (await conn.execute(
                    "select case_facts, fault_agreement from public.report_case_inputs"
                    " where case_id = %s",
                    (case_id,),
                )).fetchone()
                packages = await (await conn.execute(
                    "select package from public.claim_packages"
                    " where case_id = %s order by package_version",
                    (case_id,),
                )).fetchall()
        except psycopg.errors.UndefinedTable as exc:
            raise ReportsDbNotReady(str(exc)) from exc
        rec = CaseRecord(
            case_id=case_id,
            owner_id=assignee,
            card=reports[-1]["fact_card"] if reports else None,
            reports=[row["report"] for row in reports],
            documents=[row["body"] for row in documents],
            case_facts=dict(inputs["case_facts"]) if inputs else {},
            packages=[row["package"] for row in packages],
            fault_agreement=dict(inputs["fault_agreement"]) if inputs else {},
        )
        rec.saved = _Saved(
            reports=len(rec.reports),
            documents=len(rec.documents),
            packages=len(rec.packages),
            approvals=[r["approval_status"] for r in rec.reports],
            inputs=_inputs(rec) if inputs else None,
        )
        return rec

    async def save(self, rec: CaseRecord, actor_id: str) -> None:
        """불러온 뒤 새로 생긴 보고서·서류·청구서는 넣고, 확인 완료·입력 변경은 고친다.

        같은 버전을 다른 요청이 먼저 저장했으면 VersionTaken(409)이고 아무것도 쓰지 않는다.
        """
        import psycopg
        from psycopg.types.json import Json, Jsonb

        saved = rec.saved
        try:
            async with self._connect() as conn, conn.transaction():
                for report in rec.reports[saved.reports:]:
                    v = report["versions"]
                    await conn.execute(
                        "insert into public.review_reports (case_id, report_version,"
                        " judgment_version, fact_card_version, report, fact_card, created_by)"
                        " values (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            rec.case_id, v["report_version"], v["judgment_version"],
                            v["fact_card_version"], Json(report),
                            Json(rec.card) if rec.card is not None else None, actor_id,
                        ),
                    )
                for i, report in enumerate(rec.reports[: saved.reports]):
                    if report["approval_status"] != saved.approvals[i]:
                        await conn.execute(
                            "update public.review_reports set report = %s"
                            " where case_id = %s and report_version = %s",
                            (Json(report), rec.case_id, report["versions"]["report_version"]),
                        )
                for doc in rec.documents[saved.documents:]:
                    await conn.execute(
                        "insert into public.case_documents (case_id, document_id, kind, body,"
                        " added_by) values (%s, %s, %s, %s, %s) on conflict do nothing",
                        (rec.case_id, str(doc["document_id"]), doc["kind"], Json(doc), actor_id),
                    )
                if _inputs(rec) != saved.inputs and (rec.case_facts or rec.fault_agreement):
                    await conn.execute(
                        "insert into public.report_case_inputs"
                        " (case_id, case_facts, fault_agreement, updated_by)"
                        " values (%s, %s, %s, %s) on conflict (case_id) do update set"
                        " case_facts = excluded.case_facts,"
                        " fault_agreement = excluded.fault_agreement,"
                        " updated_by = excluded.updated_by",
                        (rec.case_id, Jsonb(rec.case_facts), Jsonb(rec.fault_agreement), actor_id),
                    )
                for pkg in rec.packages[saved.packages:]:
                    await conn.execute(
                        "insert into public.claim_packages (case_id, report_version,"
                        " package_version, idempotency_key, package, created_by)"
                        " values (%s, %s, %s, %s, %s, %s)",
                        (
                            rec.case_id, pkg["report_version"], pkg["package_version"],
                            pkg["idempotency_key"], Json(pkg), actor_id,
                        ),
                    )
        except psycopg.errors.UniqueViolation as exc:
            raise VersionTaken(
                "다른 요청이 같은 버전을 먼저 저장했습니다. 다시 조회하세요."
            ) from exc
        except psycopg.errors.UndefinedTable as exc:
            raise ReportsDbNotReady(str(exc)) from exc
        rec.saved = _Saved(
            reports=len(rec.reports),
            documents=len(rec.documents),
            packages=len(rec.packages),
            approvals=[r["approval_status"] for r in rec.reports],
            inputs=_inputs(rec),
        )


def default_repository() -> InMemoryReportRepository | PostgresReportRepository:
    """DATABASE_URL이 있으면 Postgres 저장소, 없으면 메모리 저장소(테스트·데모)."""
    from app.config import get_settings

    if get_settings().database_url:
        return PostgresReportRepository()
    return InMemoryReportRepository()
