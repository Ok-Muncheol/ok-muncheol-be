"""A: 사건 접수·조회(POST /cases, GET /cases/{id})와 업로드 서명 URL. /analysis 예정.

사건 목록(list_cases)은 서비스만 있다. FE ApiClient에 listCases가 생기면 GET /cases 경로를 연다.

사건 요약(CaseSummary)은 FE src/lib/api/types.ts의 모양이다. 단계(stage)는 최신 산출물로 정한다:
보고서(최신 판단의 것) → report, 판단(최신 카드 버전의 것) → judgment, 카드 → facts, 없으면 intake.
카드를 고쳐 새 버전이 생기면 이전 판단은 그 카드의 것이 아니므로 facts로 돌아간다.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app import db, storage
from app.api.errors import forbidden, not_found, unprocessable
from app.schemas.common import UploadKind
from app.workflow import runs

# 올릴 수 있는 형식 → 확장자. 버킷(case-files)도 PDF와 video/*만 받는다
UPLOAD_TYPES: dict[str, dict[str, str]] = {
    "estimate": {"application/pdf": "pdf"},
    "video": {
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "video/x-msvideo": "avi",
        "video/webm": "webm",
        "video/x-matroska": "mkv",
    },
}
UPLOAD_FOLDERS: dict[str, str] = {"estimate": "estimates", "video": "videos"}


INPUT_PATHS = ("video", "manual")
KST = timezone(timedelta(hours=9))

# 사건 + 최신 카드·판단(대체되지 않은 것)·보고서 + 남은 질문 수. where 절은 호출하는 쪽이 붙인다
SUMMARY_SQL = """
select c.id, c.assignee_id, c.accident_date, c.input_path, c.updated_at,
       fc.version as card_version, fc.updated_at as card_at,
       jr.judgment_version, jr.fact_card_version as run_card_version, jr.status as run_status,
       jr.judgment_status, jr.updated_at as run_at,
       (select count(*) from public.judgment_questions q
         where q.run_id = jr.id
           and not exists (select 1 from public.judgment_answers a where a.question_id = q.id)
       ) as open_questions,
       rr.report_version, rr.judgment_version as report_judgment_version,
       rr.approval_status, rr.updated_at as report_at
from public.cases c
left join lateral (
  select version, updated_at from public.fact_cards
  where case_id = c.id order by version desc limit 1) fc on true
left join lateral (
  select id, judgment_version, fact_card_version, status, judgment_status, updated_at
  from public.judgment_runs where case_id = c.id and superseded_at is null
  order by judgment_version desc limit 1) jr on true
left join lateral (
  select report_version, judgment_version, approval_status, updated_at from public.review_reports
  where case_id = c.id order by report_version desc limit 1) rr on true
"""


class PostgresCaseRepo:
    """public.cases와 사건 요약 조회. 연결 하나로 한 요청을 처리한다."""

    def __init__(self, conn: Any):
        self.conn = conn

    async def insert_case(
        self, actor_id: str, accident_date: datetime | None, input_path: str
    ) -> str:
        from psycopg import errors

        try:
            cur = await self.conn.execute(
                "insert into public.cases (assignee_id, accident_date, input_path)"
                " values (%s, %s, %s) returning id",
                (actor_id, accident_date, input_path),
            )
        except errors.ForeignKeyViolation as exc:  # 토큰의 사용자가 auth.users에 없다
            raise forbidden("담당자 계정을 찾을 수 없습니다.") from exc
        return str((await cur.fetchone())["id"])

    async def summaries(
        self, *, case_id: str | None = None, assignee_id: str | None = None
    ) -> list[dict]:
        if case_id is not None:
            cur = await self.conn.execute(SUMMARY_SQL + " where c.id = %s", (case_id,))
        else:
            cur = await self.conn.execute(
                SUMMARY_SQL + " where c.assignee_id = %s order by c.updated_at desc",
                (assignee_id,),
            )
        return list(await cur.fetchall())


@dataclass
class CaseServices:
    """테스트에서 바꿔 끼울 수 있게 DB 연결·서명 URL 발급을 모은다(judgments.py와 같은 방식)."""

    connect: Callable = db.connect
    sign_upload: Callable[[str], Awaitable[str]] = storage.create_signed_upload_url
    repo: Callable[[Any], PostgresCaseRepo] = PostgresCaseRepo


services = CaseServices()


def configure(**kw: object) -> CaseServices:
    global services
    services = CaseServices(**{**services.__dict__, **kw})  # type: ignore[arg-type]
    return services


def _case_uuid(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise not_found("사건을 찾을 수 없습니다.") from exc


async def _require_assignee(actor_id: str, case_id: str) -> None:
    """담당 사건인지 확인한다. 서버 키는 RLS를 건너뛰므로 Storage 작업 전에 반드시 부른다."""
    async with services.connect() as conn:
        assignee = await runs.case_assignee(conn, case_id)
    if assignee is None:
        raise not_found("사건을 찾을 수 없습니다.")
    if assignee != actor_id:
        raise forbidden()


def _iso(value: object) -> str | None:
    """응답 시각은 한국 시간(+09:00)으로 준다(DB는 UTC로 돌려준다)."""
    return value.astimezone(KST).isoformat() if isinstance(value, datetime) else None


def _accident_date(value: object) -> datetime | None:
    """'2026-11-08' 또는 '2026-11-08T08:40:00+09:00'. 시간대가 없으면 한국 시간으로 본다."""
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        message = "accident_date는 ISO 날짜(예: 2026-11-08)여야 합니다."
        raise unprocessable("invalid_case", message) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)


def summarize(row: dict) -> dict[str, object]:
    """사건 요약 한 행 → CaseSummary.

    이전 카드 버전의 판단, 이전 판단의 보고서는 지금 단계로 치지 않는다.
    """
    case_id = str(row["id"])
    card_version = row.get("card_version") or 0
    run_version = row.get("judgment_version")
    run_now = run_version is not None and row.get("run_card_version") == card_version
    report_now = (
        run_now
        and row.get("report_version") is not None
        and row.get("report_judgment_version") == run_version
    )
    if report_now:
        stage, task_status = "report", None
    elif run_now:
        stage, task_status = "judgment", row.get("run_status")
    elif card_version:
        stage, task_status = "facts", None
    else:
        stage, task_status = "intake", None
    times = [row.get(k) for k in ("updated_at", "card_at", "run_at", "report_at")]
    return {
        "case_id": case_id,
        "accident_date": _iso(row.get("accident_date")),
        "input_path": row["input_path"],
        "stage": stage,
        "task_status": task_status,
        "judgment_status": row.get("judgment_status") if run_now else None,
        "approval_status": row.get("approval_status") if report_now else None,
        "open_questions": int(row.get("open_questions") or 0) if run_now else 0,
        "versions": {
            "case_id": case_id,
            "fact_card_version": card_version,
            "judgment_version": run_version if run_now else None,
            "report_version": row.get("report_version") if report_now else None,
        },
        "updated_at": _iso(max((t for t in times if isinstance(t, datetime)), default=None)),
    }


async def create_case(actor_id: str, payload: dict[str, object]) -> dict[str, object]:
    """사고 범위·당사자 수 검사 후 로그인한 담당자의 사건을 만든다. 응답은 CaseSummary.

    payload: {accident_date?, input_path?: "video"|"manual", accident_type?, party_count?}.
    범위는 차대차 2대(docs/contracts/mvp-chart-scope.md). 범위 밖은 422 case_out_of_scope로
    접수하지 않는다.
    """
    accident_type = payload.get("accident_type", "car_to_car")
    party_count = payload.get("party_count", 2)
    if accident_type != "car_to_car" or party_count != 2 or isinstance(party_count, bool):
        raise unprocessable(
            "case_out_of_scope",
            "지금은 차대차 2대 사고만 접수합니다. "
            "차대보행자·이륜차·단독·3대 이상 사고는 범위 밖입니다.",
        )
    input_path = payload.get("input_path", "video")
    if input_path not in INPUT_PATHS:
        raise unprocessable("invalid_case", "input_path는 video 또는 manual이어야 합니다.")
    accident_date = _accident_date(payload.get("accident_date"))
    async with services.connect() as conn:
        repo = services.repo(conn)
        case_id = await repo.insert_case(actor_id, accident_date, str(input_path))
        rows = await repo.summaries(case_id=case_id)
    return summarize(rows[0])


async def list_cases(actor_id: str) -> list[dict[str, object]]:
    """로그인한 담당자의 사건 요약 목록(최근 바뀐 순)."""
    async with services.connect() as conn:
        rows = await services.repo(conn).summaries(assignee_id=actor_id)
    return [summarize(r) for r in rows]


async def request_upload(
    actor_id: str,
    case_id: str,
    kind: UploadKind | None = None,
    content_type: str | None = None,
) -> dict[str, object]:
    """담당 사건에 한해 Storage 직접 업로드용 서명 URL을 발급한다.

    순서: 사건 권한 확인 → 형식 확인 → 경로 만들기 → 서명 URL 발급(바꾸지 않는다).
    경로: `case-files/{case_id}/videos/{uuid}.{확장자}`,
    `case-files/{case_id}/estimates/{uuid}.pdf`.
    파일 이름은 uuid로 만든다(Supabase가 한글 이름을 400 InvalidKey로 거절한다).
    응답의 `storage_path`는 FE가 PUT 한 뒤 받는 `Key`와 같고, 분석 접수·견적서 경로에 그대로 넘긴다.
    """
    case_id = _case_uuid(case_id)
    await _require_assignee(actor_id, case_id)
    types = UPLOAD_TYPES.get(str(kind))
    if types is None:
        raise unprocessable("invalid_upload_type", "kind는 video 또는 estimate여야 합니다.")
    ext = types.get((content_type or "").split(";")[0].strip().lower())
    if ext is None:
        allowed = ", ".join(types)
        message = f"{kind}는 다음 형식만 올릴 수 있습니다: {allowed}"
        raise unprocessable("invalid_upload_type", message)
    object_key = f"{case_id}/{UPLOAD_FOLDERS[str(kind)]}/{uuid.uuid4()}.{ext}"
    upload_url = await services.sign_upload(object_key)
    return {
        "storage_path": storage.storage_path_of(object_key),
        "upload_url": upload_url,
        "expires_in": storage.UPLOAD_URL_EXPIRES_SECONDS,
    }


async def start_analysis(
    actor_id: str, case_id: str, storage_path: str, idempotency_key: str
) -> dict[str, object]:
    """소유권·업로드 완료 확인 후 비전 작업 큐에 접수."""
    raise NotImplementedError("A: 작업 접수·멱등성·상태 복원")


async def get_case(actor_id: str, case_id: str) -> dict[str, object]:
    """현재 단계·카드·판단·질문·보고서 버전을 권한 검사 후 조회(새로고침·재접속 복원용)."""
    case_id = _case_uuid(case_id)
    async with services.connect() as conn:
        rows = await services.repo(conn).summaries(case_id=case_id)
    if not rows:
        raise not_found("사건을 찾을 수 없습니다.")
    if str(rows[0]["assignee_id"]) != actor_id:
        raise forbidden()
    return summarize(rows[0])
