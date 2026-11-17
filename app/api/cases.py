"""A: 사건 접수·조회(POST /cases, GET /cases/{id}), 업로드 서명 URL, 분석 접수(→ analysis.py).

사건 목록(list_cases)은 서비스만 있다. FE ApiClient에 listCases가 생기면 GET /cases 경로를 연다.

사건 요약(CaseSummary)은 FE src/lib/api/types.ts의 모양이다. 단계(stage)는 최신 산출물로 정한다:
보고서(최신 판단의 것, 판정이 초안 작성 가능일 때) → report, 판단(최신 카드 버전의 것) → judgment,
끝나지 않았거나 실패한 최신 분석 → analysis, 카드 → facts, 없으면 intake.
카드를 고쳐 새 버전이 생기면 이전 판단은 그 카드의 것이 아니므로 facts로 돌아간다.
판단·보고서까지 간 사건에 영상을 다시 올리면 분석이 대기·진행 중인 동안은 analysis가 먼저다
(접수부터 다시). 끝나면 분석 카드가 새 버전이라 facts, 실패하면 이전 단계 그대로다.
추가 확인 필요·보류 판정도 보고서를 만들지만(judgments.py) 승인할 수 없어 판단 단계에 둔다.

단계 되돌리기(restart_case, POST /cases/{id}/restart): 담당자가 사건을 접수·판단 단계의 처음으로
되돌려 저장한다. 산출물은 지우지 않는다 — 접수는 cases.restarted_at 앞의 카드·분석을, 둘 다
그때까지의 판단 실행을 지금 것에서 빼서, 위 규칙이 그대로 접수·사실 카드 단계를 낸다.
"""

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app import db, storage
from app.api import facts
from app.api.errors import ApiException, forbidden, not_found, unprocessable, version_conflict
from app.schemas.common import UploadKind
from app.workflow import runs

# 담당자가 올릴 수 있는 형식 → 확장자. 버킷(case-files)은 PDF·video/*·image/jpeg를 받는다
# (image/jpeg는 근거 사진용으로 Modal 워커가 올리고, 대인 증빙 사진도 담당자가 올린다).
# medical_document는 대인 보상 증빙(진단서·진료비 영수증 — 스캔 PDF나 휴대폰 사진)이다.
# 첨부만 하고 읽지 않는다.
UPLOAD_TYPES: dict[str, dict[str, str]] = {
    "estimate": {"application/pdf": "pdf"},
    "video": {
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "video/x-msvideo": "avi",
        "video/webm": "webm",
        "video/x-matroska": "mkv",
    },
    "medical_document": {"application/pdf": "pdf", "image/jpeg": "jpg"},
}
UPLOAD_FOLDERS: dict[str, str] = {
    "estimate": "estimates",
    "video": "videos",
    "medical_document": "medical",
}


KST = timezone(timedelta(hours=9))

# 사건 + 최신 카드·판단(대체되지 않은 것)·보고서 + 남은 질문 수. where 절은 호출하는 쪽이 붙인다.
# 카드·분석은 사건을 접수 단계로 되돌린 시각(runs.RESTARTED_AT_SQL) 뒤에 만든 것만 본다
SUMMARY_SQL = f"""
select c.id, c.assignee_id, c.injury_assignee_id, c.accident_date,
       c.created_at, c.updated_at,
       fc.version as card_version, fc.updated_at as card_at,
       jr.judgment_version, jr.fact_card_version as run_card_version, jr.status as run_status,
       jr.judgment_status, jr.updated_at as run_at,
       (select count(*) from public.judgment_questions q
         where q.run_id = jr.id
           and not exists (select 1 from public.judgment_answers a where a.question_id = q.id)
       ) as open_questions,
       rr.report_version, rr.judgment_version as report_judgment_version,
       rr.approval_status, rr.updated_at as report_at,
       at.status as analysis_status, at.created_at as analysis_at,
       at.updated_at as analysis_updated_at
from public.cases c
left join lateral (
  select version, updated_at from public.fact_cards
  where case_id = c.id and created_at >= {runs.RESTARTED_AT_SQL}
  order by version desc limit 1) fc on true
left join lateral (
  select id, judgment_version, fact_card_version, status, judgment_status, updated_at
  from public.judgment_runs where case_id = c.id and superseded_at is null
  order by judgment_version desc limit 1) jr on true
left join lateral (
  select report_version, judgment_version, approval_status, updated_at from public.review_reports
  where case_id = c.id order by report_version desc limit 1) rr on true
left join lateral (
  select status, created_at, updated_at from public.analysis_tasks
  where case_id = c.id and created_at >= {runs.RESTARTED_AT_SQL}
  order by created_at desc limit 1) at on true
"""


class PostgresCaseRepo:
    """public.cases와 사건 요약 조회. 연결 하나로 한 요청을 처리한다."""

    def __init__(self, conn: Any):
        self.conn = conn

    async def insert_case(self, actor_id: str, accident_date: datetime | None) -> str:
        from psycopg import errors

        try:
            cur = await self.conn.execute(
                "insert into public.cases (assignee_id, accident_date)"
                " values (%s, %s) returning id",
                (actor_id, accident_date),
            )
        except errors.ForeignKeyViolation as exc:  # 토큰의 사용자가 auth.users에 없다
            raise forbidden("담당자 계정을 찾을 수 없습니다.") from exc
        return str((await cur.fetchone())["id"])

    async def expire_analysis(self, case_id: str) -> None:
        """콜백이 15분 넘게 안 온 분석을 실패(analysis_timeout)로 바꾼다."""
        from psycopg.types.json import Jsonb

        from app.api.analysis import EXPIRE_SQL, TIMEOUT_ERROR

        await self.conn.execute(EXPIRE_SQL, (Jsonb(TIMEOUT_ERROR), case_id))

    async def lock(self, case_id: str) -> None:
        """사건 행을 잠근다(같은 사건의 되돌리기·판단 접수가 겹치지 않게)."""
        await self.conn.execute("select id from public.cases where id = %s for update", (case_id,))

    async def mark_restarted(self, case_id: str) -> None:
        """접수 단계로 되돌린 시각을 적는다. 그 전의 카드·분석은 지금 것이 아니게 된다."""
        await self.conn.execute(
            "update public.cases set restarted_at = now() where id = %s", (case_id,)
        )

    async def clear_inputs(self, case_id: str) -> None:
        """산정·청구 입력(서류, 면책 체크·협의 과실)을 지운다. 사건을 되돌릴 때만 부른다.

        보고서 버전이 아니라 사건에 하나뿐인 입력이다(D review_reports 마이그레이션). 보고서
        테이블이 아직 없는 DB에서는 지울 것이 없다.
        """
        from psycopg import errors

        try:
            async with self.conn.transaction():
                await self.conn.execute(
                    "delete from public.case_documents where case_id = %s", (case_id,)
                )
                await self.conn.execute(
                    "delete from public.report_case_inputs where case_id = %s", (case_id,)
                )
        except errors.UndefinedTable:
            pass

    async def summaries(
        self, *, case_id: str | None = None, assignee_id: str | None = None
    ) -> list[dict]:
        """사건 하나(case_id) 또는 그 사람이 대물·대인 담당자인 사건 모두(assignee_id)."""
        if case_id is not None:
            cur = await self.conn.execute(SUMMARY_SQL + " where c.id = %s", (case_id,))
        else:
            cur = await self.conn.execute(
                SUMMARY_SQL
                + " where %s in (c.assignee_id, c.injury_assignee_id) order by c.updated_at desc",
                (assignee_id,),
            )
        return list(await cur.fetchall())


@dataclass
class CaseServices:
    """테스트에서 바꿔 끼울 수 있게 DB 연결·서명 URL 발급을 모은다(judgments.py와 같은 방식)."""

    connect: Callable = db.connect
    sign_upload: Callable[[str], Awaitable[str]] = storage.create_signed_upload_url
    list_objects: Callable[[str], Awaitable[list[dict[str, object]]]] = storage.list_objects
    copy_object: Callable[[str, str], Awaitable[None]] = storage.copy_object
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
    """담당 사건인지 확인한다(대물·대인 담당자 모두 통과).

    서버 키는 RLS를 건너뛰므로 Storage 작업 전에 반드시 부른다.
    """
    async with services.connect() as conn:
        assignees = await runs.case_assignees(conn, case_id)
    if assignees is None:
        raise not_found("사건을 찾을 수 없습니다.")
    if actor_id not in assignees:
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
    보고서 단계는 최신 판정이 초안 작성 가능(draft_ready)일 때만이다. 추가 확인 필요·보류
    판정은 보고서가 있어도 승인할 수 없으므로(reports.confirm_report) 판단 단계에 둔다 —
    담당자가 카드를 고쳐 다시 판단해야 다음 단계로 간다.
    """
    case_id = str(row["id"])
    card_version = row.get("card_version") or 0
    run_version = row.get("judgment_version")
    run_now = run_version is not None and row.get("run_card_version") == card_version
    report_now = (
        run_now
        and row.get("judgment_status") == "draft_ready"
        and row.get("report_version") is not None
        and row.get("report_judgment_version") == run_version
    )
    # 최신 분석이 끝나지 않았거나 실패했고, 그 뒤에 만든 카드가 없으면 분석 단계다.
    # 판단·보고서까지 간 사건에 영상을 새로 올렸으면(영상 다시 올리기·영상 없이 진행한 사건)
    # 분석이 대기·진행 중인 동안만 분석 단계다. 끝나면 분석 카드가 새 버전이라 사실 카드로
    # 돌아가고, 실패하면 이전 판단·보고서가 그대로다
    analysis = row.get("analysis_status")
    card_at, analysis_at = row.get("card_at"), row.get("analysis_at")
    analysis_now = (
        analysis in ("queued", "running", "failed")
        and (not run_now or analysis in ("queued", "running"))
        and (not card_version or (card_at is not None and analysis_at > card_at))
    )
    # 새 분석을 보고서보다 먼저 본다 — 보고서(승인한 것 포함)가 있어도 분석 중이면 분석 단계다
    if analysis_now:
        stage, task_status = "analysis", analysis
    elif report_now:
        stage, task_status = "report", None
    elif run_now:
        stage, task_status = "judgment", row.get("run_status")
    elif card_version:
        stage, task_status = "facts", None
    else:
        stage, task_status = "intake", None
    keys = ("updated_at", "card_at", "run_at", "report_at", "analysis_updated_at")
    times = [row.get(k) for k in keys]
    return {
        "case_id": case_id,
        "accident_date": _iso(row.get("accident_date")),
        # 배정(사건 생성) 시각 — 보드의 배정일 · 배정 순 정렬
        "created_at": _iso(row.get("created_at")),
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

    payload: {accident_date?, accident_type?, party_count?}.
    범위는 차대차 2대(docs/contracts/mvp-chart-scope.md). 범위 밖은 422 case_out_of_scope로
    접수하지 않는다. 사건은 모두 영상 분석으로 카드를 만든다 — 당사자 진술은 담당자가
    판단 화면에서 카드 칸을 고쳐 반영한다(PATCH /cases/{id}/fact-card).
    """
    accident_type = payload.get("accident_type", "car_to_car")
    party_count = payload.get("party_count", 2)
    if accident_type != "car_to_car" or party_count != 2 or isinstance(party_count, bool):
        raise unprocessable(
            "case_out_of_scope",
            "지금은 차대차 2대 사고만 접수합니다. "
            "차대보행자·이륜차·단독·3대 이상 사고는 범위 밖입니다.",
        )
    accident_date = _accident_date(payload.get("accident_date"))
    async with services.connect() as conn:
        repo = services.repo(conn)
        case_id = await repo.insert_case(actor_id, accident_date)
        rows = await repo.summaries(case_id=case_id)
    return summarize(rows[0])


RESTART_STAGES = ("intake", "judgment")


def _blocked(code: str, message: str) -> ApiException:
    return ApiException(409, code, message, True)


async def restart_case(actor_id: str, case_id: str, stage: str) -> dict[str, object]:
    """사건을 그 단계의 처음으로 되돌려 저장한다. 응답은 되돌린 뒤의 CaseSummary.

    접수 화면의 「영상 다시 올리기」(intake)와 판단 화면의 「초기화」(judgment)가 부른다.
    산출물은 지우지 않는다. 그때까지의 카드·판단·보고서는 버전으로 남고 지금 것에서만 빠진다.
    - intake: 되돌린 시각을 적는다(cases.restarted_at). 그 전의 카드·분석은 지금 것이 아니라
      접수 단계가 된다. 다음 카드는 이어지는 버전 번호로 들어온다.
    - judgment: 원본 카드(영상 분석 카드·빈 카드)를 확인 전 상태의 새 버전으로 다시 만든다
      (facts.pristine_card). 지금 카드가 이미 그 원본 그대로면 만들지 않는다. 사실 카드 단계가 된다.
    둘 다 그때까지의 판단 실행을 대체된 것으로 찍는다 — 판단 조회는 null이고 보고서도 지금 것이
    아니다. 서류(견적서·증빙·대물·대인 입력)와 면책 체크·협의 과실은 지운다(clear_inputs) — 버전이
    아니라 사건에 하나뿐인 입력이라 남기면 다음 보고서가 그대로 이어 쓴다. 이미 만든 보고서와
    청구서는 그때 내용을 담고 있어 그대로 읽힌다. Storage의 파일은 지우지 않는다.
    영상 분석이 대기·진행 중이면 409 analysis_in_progress, 판단이 실행 중이면 409
    judgment_in_progress, judgment인데 카드가 없으면 409 fact_card_required다.
    되돌리기 칼럼이 아직 없으면(마이그레이션 적용 전) intake는 503 restart_db_not_ready다.
    """
    from psycopg import errors

    if stage not in RESTART_STAGES:
        raise unprocessable("invalid_restart_stage", "stage는 intake 또는 judgment여야 합니다.")
    case_id = _case_uuid(case_id)
    await _require_assignee(actor_id, case_id)
    async with services.connect() as conn:
        repo = services.repo(conn)
        cards = facts.PostgresFactRepo(conn)
        try:
            async with conn.transaction():
                await repo.lock(case_id)
                if await cards.analysis_active(case_id):
                    raise _blocked(
                        "analysis_in_progress", "영상 분석이 진행 중입니다. 끝난 뒤 되돌리세요."
                    )
                if await runs.run_in_progress(conn, case_id):
                    raise _blocked(
                        "judgment_in_progress", "판단이 실행 중입니다. 끝난 뒤 되돌리세요."
                    )
                if stage == "intake":
                    await repo.mark_restarted(case_id)
                else:
                    await _restart_card(cards, case_id, actor_id)
                await runs.supersede_runs(conn, case_id)
                await repo.clear_inputs(case_id)
        except errors.UndefinedColumn as exc:
            raise ApiException(
                503, "restart_db_not_ready",
                "사건을 접수 단계로 되돌리는 DB 칼럼이 아직 없습니다. 마이그레이션을 적용하세요.",
                True,
            ) from exc
        rows = await repo.summaries(case_id=case_id)
    return summarize(rows[0])


async def _restart_card(cards: facts.PostgresFactRepo, case_id: str, actor_id: str) -> None:
    """판단 단계의 처음 카드로. 지금 카드가 원본 그대로(확정 전)면 아무것도 만들지 않는다."""
    current = await cards.latest(case_id)
    origin = await cards.origin(case_id)
    if current is None or origin is None:
        raise ApiException(
            409, "fact_card_required",
            "사실 카드가 없습니다. 영상 분석을 마치거나 영상 없이 진행한 뒤 되돌리세요.", False,
        )
    if current["version"] == origin["version"] and current["status"] == "draft":
        return
    new = facts.pristine_card(origin, await cards.next_version(case_id))
    if not await cards.insert(new, "edit", facts.RESTART_REASON, actor_id):
        raise version_conflict("사실 카드가 그 사이 바뀌었습니다. 다시 시도하세요.")


async def list_cases(actor_id: str) -> list[dict[str, object]]:
    """로그인한 담당자가 대물 또는 대인 담당자인 사건의 요약 목록(최근 바뀐 순)."""
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
    `case-files/{case_id}/estimates/{uuid}.pdf`, `case-files/{case_id}/medical/{uuid}.pdf|jpg`.
    파일 이름은 uuid로 만든다(Supabase가 한글 이름을 400 InvalidKey로 거절한다).
    응답의 `storage_path`는 FE가 PUT 한 뒤 받는 `Key`와 같고, 분석 접수·견적서 경로에 그대로 넘긴다.
    """
    case_id = _case_uuid(case_id)
    await _require_assignee(actor_id, case_id)
    types = UPLOAD_TYPES.get(str(kind))
    if types is None:
        raise unprocessable(
            "invalid_upload_type", "kind는 video, estimate, medical_document 중 하나여야 합니다."
        )
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


# ── 시연 파일 ─────────────────────────────────────────────────────
# 시연용으로 미리 올려 둔 파일 폴더: case-files/demo/{videos,estimates,medical}/.
# 담당자가 업로드 칸에서 골라 쓰면 그 사건 폴더로 복사한다(파일 이름은 uuid로 새로 만든다).
# 그 뒤 흐름은 직접 올린 파일과
# 같아서 사건 폴더 밖 파일을 막는 경로 검사(분석 접수·견적서·증빙)가 그대로 지켜진다.
# 폴더가 비어 있으면 목록이 비고 화면은 고르기를 보이지 않는다.
DEMO_PREFIX = "demo"
_DEMO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def _upload_types(kind: object) -> dict[str, str]:
    types = UPLOAD_TYPES.get(str(kind))
    if types is None:
        raise unprocessable(
            "invalid_upload_type", "kind는 video, estimate, medical_document 중 하나여야 합니다."
        )
    return types


async def list_demo_files(
    actor_id: str, kind: UploadKind | None = None
) -> list[dict[str, object]]:
    """시연 폴더의 파일 목록(이름순). 그 kind로 올릴 수 있는 확장자만 준다.

    로그인한 담당자면 본다."""
    extensions = set(_upload_types(kind).values())
    rows = await services.list_objects(f"{DEMO_PREFIX}/{UPLOAD_FOLDERS[str(kind)]}")
    return [
        r for r in rows
        if _DEMO_NAME.fullmatch(str(r["name"])) and str(r["name"]).rsplit(".", 1)[-1] in extensions
    ]


async def use_demo_file(
    actor_id: str, case_id: str, kind: UploadKind | None = None, name: str | None = None
) -> dict[str, object]:
    """시연 파일 하나를 담당 사건 폴더로 복사하고 그 `storage_path`를 준다.

    직접 올린 파일과 같은 값이다.

    순서: 사건 권한 확인 → kind·파일 이름 확인 → 복사. 이름은 영문·숫자·`._-`만 받는다
    (경로 조각·인코딩 우회를 막는다).
    """
    case_id = _case_uuid(case_id)
    await _require_assignee(actor_id, case_id)
    extensions = set(_upload_types(kind).values())
    name = str(name or "")
    ext = name.rsplit(".", 1)[-1]
    if not _DEMO_NAME.fullmatch(name) or "." not in name or ext not in extensions:
        raise unprocessable("invalid_demo_file", "시연 파일 이름이 올바르지 않습니다.")
    folder = UPLOAD_FOLDERS[str(kind)]
    object_key = f"{case_id}/{folder}/{uuid.uuid4()}.{ext}"
    try:
        await services.copy_object(f"{DEMO_PREFIX}/{folder}/{name}", object_key)
    except storage.StorageObjectNotFound as exc:
        raise not_found("시연 파일을 찾을 수 없습니다.") from exc
    return {"storage_path": storage.storage_path_of(object_key)}


async def start_analysis(
    actor_id: str, case_id: str, storage_path: str, idempotency_key: str
) -> dict[str, object]:
    """소유권·업로드 완료 확인 후 영상 분석 작업을 만들고 Modal에 넘긴다(app/api/analysis.py)."""
    from app.api import analysis

    return await analysis.start_analysis(actor_id, case_id, storage_path, idempotency_key)


async def get_case(actor_id: str, case_id: str) -> dict[str, object]:
    """현재 단계·카드·판단·질문·보고서 버전을 권한 검사 후 조회(새로고침·재접속 복원용)."""
    case_id = _case_uuid(case_id)
    async with services.connect() as conn:
        repo = services.repo(conn)
        await repo.expire_analysis(case_id)
        rows = await repo.summaries(case_id=case_id)
    if not rows:
        raise not_found("사건을 찾을 수 없습니다.")
    if actor_id not in runs.assignees_of(rows[0]):
        raise forbidden()
    return summarize(rows[0])
