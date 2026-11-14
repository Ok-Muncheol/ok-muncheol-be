"""B(통합): HTTP 경로 → 담당 서비스 함수. FE `src/lib/api/client.ts`의 `ApiClient`와 1:1로 맞춘다.

경로는 README 「API 계약 제안」 표가 기준이다.
tests/api/test_app.py가 그 표·계약 문서와 서버 OpenAPI를 대조한다.

이 파일은 경로·인증·요청 모양만 정한다. 권한·상태·버전 판단은 각 담당 서비스 함수가 한다.
`/admin` 경로만 예외로 경로가 관리자 여부를 건다(`AdminId` — 토큰의 app_metadata.staff_role).
서비스는 모듈 속성으로 호출해(`cases.create_case`) 테스트에서 바꿔 끼울 수 있게 한다.
미구현 서비스는 `NotImplementedError` → 501 `not_implemented`로 응답한다(app/api/errors.py).
Modal 콜백(`/internal/analysis/{task_id}`)만 사용자 토큰 대신 작업별 일회용 토큰으로 확인하고
OpenAPI에서 뺀다.
"""

import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field, StringConstraints

from app.api import admin, analysis, cases, facts, judgments, reports
from app.api.deps import ActorId, AdminId, IdempotencyKey
from app.api.errors import ApiException, unprocessable, version_conflict
from app.schemas.admin import AdminOverview, IntakeView
from app.schemas.common import UploadKind, UploadTicket
from app.schemas.facts import FactCard, FactPatch
from app.schemas.judgment import QuestionAnswer
from app.schemas.reports import ClaimResult, ConfirmResult, CounterpartyInfo, ReviewReport

router = APIRouter()

# Modal 콜백 본문 한도: 카드 + 근거 사진(JPEG base64 12장). Vercel 함수 요청 본문 한도는 4.5MB
CALLBACK_MAX_BYTES = 3 * 1024 * 1024
# 접수 건 목록 한 쪽의 최대 건수(app/api/admin.py MAX_LIMIT와 같다)
INTAKE_PAGE_MAX = 200


class UploadRequest(BaseModel):
    """kind가 목록 밖이면 422 invalid_request. kind별 허용 형식 검사는 A 서비스가 한다."""

    kind: UploadKind
    content_type: str = Field(min_length=1, max_length=100)


class AnalysisRequest(BaseModel):
    storage_path: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    expected_version: int = Field(ge=1)


class JudgmentRequest(BaseModel):
    card_version: int = Field(ge=1)


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ReportConfirmRequest(BaseModel):
    counterparty: CounterpartyInfo | None = None


class DocumentsRequest(BaseModel):
    """서류(계약·견적·증빙)와 면책 체크(case_facts). 면책만 바꿀 때는 documents를 비운다."""

    documents: list[dict[str, object]] = Field(default_factory=list)
    case_facts: dict[str, object] | None = None


class EstimateRequest(BaseModel):
    """견적서 PDF는 FE가 서명 URL로 Storage에 올리고 그 경로만 보낸다.

    Vercel 요청 본문 한도(4.5MB) 때문에 파일을 API로 직접 받지 않는다.
    """

    storage_path: str = Field(min_length=1)
    party_id: str = Field(min_length=1)


class IntakeRequest(BaseModel):
    """관리자가 등록하는 접수. 사고 장소와 내용은 들은 대로 적는 글이다(지역은 배정이 판별한다)."""

    location: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    injured_count: int = Field(default=0, ge=0, le=20)
    accident_at: datetime | None = None


class AgreedFaultRequest(BaseModel):
    agreed_counterparty_fault: int | None = None
    fault_change_reason: str | None = None
    clear: bool = False


# ── A: 사건 접수·영상 ──────────────────────────────────────────────
@router.get("/cases", tags=["A cases"])
async def list_cases(actor_id: ActorId) -> list[dict[str, object]]:
    """로그인한 담당자에게 배정된 사건 요약 목록(최근 바뀐 순) — 담당 사건 보드가 읽는다."""
    return await cases.list_cases(actor_id)


@router.post("/cases", tags=["A cases"], status_code=201)
async def create_case(actor_id: ActorId, payload: dict[str, object]) -> dict[str, object]:
    return await cases.create_case(actor_id, payload)


@router.post("/cases/{case_id}/uploads", tags=["A cases"])
async def request_upload(actor_id: ActorId, case_id: str, body: UploadRequest) -> UploadTicket:
    return await cases.request_upload(actor_id, case_id, body.kind, body.content_type)


@router.post("/cases/{case_id}/analysis", tags=["A cases"], status_code=202)
async def start_analysis(
    actor_id: ActorId, case_id: str, body: AnalysisRequest, idempotency_key: IdempotencyKey
) -> dict[str, object]:
    return await cases.start_analysis(actor_id, case_id, body.storage_path, idempotency_key)


@router.post("/internal/analysis/{task_id}", include_in_schema=False)
async def receive_analysis_result(
    task_id: str, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, object]:
    """Modal 콜백(서버 간, B 인프라). 사용자 토큰이 아니라 작업별 일회용 토큰(Bearer)으로 확인한다.

    토큰 확인·카드 저장·한 번만 반영은 `analysis.receive_result`(A)가 한다. 이 경로는 본문 크기와
    JSON 모양만 본다. 3MB를 넘으면 413 `callback_too_large`,
    JSON 객체가 아니면 422 `invalid_callback`.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > CALLBACK_MAX_BYTES:
        raise ApiException(413, "callback_too_large", "콜백 본문이 3MB를 넘습니다.", False)
    raw = await request.body()
    if len(raw) > CALLBACK_MAX_BYTES:
        raise ApiException(413, "callback_too_large", "콜백 본문이 3MB를 넘습니다.", False)
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise unprocessable("invalid_callback", "콜백 본문은 JSON 객체여야 합니다.")
    scheme, _, token = (authorization or "").partition(" ")
    return await analysis.receive_result(
        task_id, token.strip() if scheme.lower() == "bearer" else None, body
    )


@router.get("/cases/{case_id}", tags=["A cases"])
async def get_case(actor_id: ActorId, case_id: str) -> dict[str, object]:
    return await cases.get_case(actor_id, case_id)


@router.get("/cases/{case_id}/evidence", tags=["A facts"])
async def get_evidence(actor_id: ActorId, case_id: str) -> dict[str, object]:
    """근거 사진·미리보기 영상의 보기용 서명 URL(1시간). 비공개 버킷이라 화면은 이 URL로만 연다."""
    return await analysis.get_evidence(actor_id, case_id)


# ── A: 사실 카드 ──────────────────────────────────────────────────
@router.get("/cases/{case_id}/fact-card", tags=["A facts"])
async def get_fact_card(actor_id: ActorId, case_id: str) -> FactCard:
    return await facts.get_fact_card(actor_id, case_id)


@router.patch("/cases/{case_id}/fact-card", tags=["A facts"])
async def patch_fact_card(actor_id: ActorId, case_id: str, patch: FactPatch) -> FactCard:
    return await facts.patch_fact_card(actor_id, case_id, patch)


@router.post("/cases/{case_id}/fact-card/confirm", tags=["A facts"])
async def confirm_fact_card(actor_id: ActorId, case_id: str, body: ConfirmRequest) -> FactCard:
    return await facts.confirm_fact_card(actor_id, case_id, body.expected_version)


# ── B: 판단·질문·질의응답 ─────────────────────────────────────────
@router.post("/cases/{case_id}/judgments", tags=["B judgments"], status_code=202)
async def start_judgment(
    actor_id: ActorId, case_id: str, body: JudgmentRequest, idempotency_key: IdempotencyKey
) -> dict[str, object]:
    """FE는 카드 버전만 보낸다. 서버가 카드를 다시 읽어 확정·버전을 확인한 뒤 판단에 넘긴다."""
    card = await facts.get_fact_card(actor_id, case_id)
    if card["version"] != body.card_version:
        raise version_conflict("사실 카드가 그 사이 바뀌었습니다. 최신 카드를 확인하세요.")
    if card["status"] != "confirmed":
        raise unprocessable("card_not_confirmed", "담당자가 확정한 사실 카드만 판단할 수 있습니다.")
    return await judgments.start_judgment(actor_id, card, idempotency_key)


@router.get("/cases/{case_id}/judgment", tags=["B judgments"])
async def get_judgment(actor_id: ActorId, case_id: str) -> dict[str, object] | None:
    """최신 판단 실행과 구분 질문·답(JudgmentRunView). 판단 전이면 null. 새로고침·재접속 복원용.

    응답 모델로 다시 검사하지 않는다 — 저장된 result는 필드가 늘기 전의 예전 모양일 수 있다.
    """
    return await judgments.get_judgment(actor_id, case_id)  # type: ignore[return-value]


@router.post("/cases/{case_id}/questions/{question_id}/answer", tags=["B judgments"])
async def answer_question(
    actor_id: ActorId, case_id: str, question_id: str, answer: QuestionAnswer
) -> dict[str, object]:
    """경로의 질문 ID가 기준이다. 본문 QuestionAnswer의 question_id가 다르면 거절한다."""
    if answer["question_id"] != question_id:
        raise unprocessable("question_mismatch", "경로와 본문의 질문 ID가 다릅니다.")
    return await judgments.answer_question(actor_id, case_id, answer)


@router.post("/cases/{case_id}/qa", tags=["B judgments"])
async def ask_question(actor_id: ActorId, case_id: str, body: AskRequest) -> dict[str, object]:
    """근거 질의응답·what-if(QaAnswer). 최신 판정 기준이고 공식 판단·보고서는 바꾸지 않는다."""
    return await judgments.ask_question(actor_id, case_id, body.text)  # type: ignore[return-value]


# ── D: 보고서·확인 완료·서류·구상(docs/d-claims-reports.md 「HTTP 경로」) ────────
@router.get("/cases/{case_id}/reports/{version}", tags=["D reports"])
async def get_report(actor_id: ActorId, case_id: str, version: int) -> ReviewReport:
    return await reports.get_report(actor_id, case_id, version)


@router.post("/cases/{case_id}/reports/{version}/confirm", tags=["D reports"])
async def confirm_report(
    actor_id: ActorId, case_id: str, version: int, body: ReportConfirmRequest | None = None
) -> ConfirmResult:
    """담당자 확인 완료 1회. 청구서 조건이 맞으면 구상 청구서를 함께 만든다."""
    counterparty = body.counterparty if body else None
    return await reports.confirm_report(actor_id, case_id, version, counterparty=counterparty)


@router.get("/cases/{case_id}/reports/{version}/claim", tags=["D reports"])
async def get_claim(actor_id: ActorId, case_id: str, version: int) -> ClaimResult:
    return await reports.get_claim(actor_id, case_id, version)


@router.post("/cases/{case_id}/documents", tags=["D reports"])
async def add_documents(
    actor_id: ActorId, case_id: str, body: DocumentsRequest
) -> ReviewReport | None:
    """서류 추가 → 자동 재산정. 추가 확인·보류 사건이면 서류만 저장하고 null."""
    return await reports.add_documents(actor_id, case_id, body.documents, body.case_facts)


@router.post("/cases/{case_id}/estimates", tags=["D reports"])
async def upload_estimate(
    actor_id: ActorId, case_id: str, body: EstimateRequest
) -> ReviewReport | None:
    """D: 올린 견적서 PDF를 Storage에서 내려받아 자동 추출·재산정한다.

    storage_path는 `POST /cases/{id}/uploads`(kind=estimate)가 준 버킷 포함 경로다.
    권한 확인 → 경로 확인 → 서버 키로 내려받기 순서는 reports.upload_estimate_from_storage가 지킨다.
    """
    return await reports.upload_estimate_from_storage(
        actor_id, case_id, body.storage_path, body.party_id
    )


@router.patch("/cases/{case_id}/agreed-fault", tags=["D reports"])
async def set_agreed_fault(
    actor_id: ActorId, case_id: str, body: AgreedFaultRequest
) -> ReviewReport:
    return await reports.set_agreed_fault(
        actor_id, case_id, agreed_counterparty_fault=body.agreed_counterparty_fault,
        fault_change_reason=body.fault_change_reason, clear=body.clear,
    )


# ── B: 관리자(관제) ────────────────────────────────────────────────
@router.get("/admin/overview", tags=["B admin"])
async def get_admin_overview(admin_id: AdminId) -> AdminOverview:
    """담당자 목록(업무량 포함)과 전체 현황 숫자. 관리자(토큰 app_metadata.staff_role = admin)만."""
    return await admin.get_overview(admin_id)


@router.get("/admin/intakes", tags=["B admin"])
async def list_admin_intakes(
    admin_id: AdminId,
    state: str | None = None,
    assignee_id: str | None = None,
    region: str | None = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=INTAKE_PAGE_MAX)] = 50,
) -> list[IntakeView]:
    """접수 건 목록(최근 접수 순). 다음 쪽은 마지막 건의 received_at을 before로 넘긴다."""
    return await admin.list_intakes(
        admin_id, state=state, assignee_id=assignee_id, region=region, before=before, limit=limit
    )


@router.post("/admin/intakes", tags=["B admin"], status_code=201)
async def register_admin_intake(admin_id: AdminId, body: IntakeRequest) -> IntakeView:
    """접수를 등록하고 바로 배정한다. 담당자를 구하지 못하면 미배정으로 남고 까닭이 온다."""
    return await admin.register_intake(
        admin_id, location=body.location, description=body.description,
        injured_count=body.injured_count, accident_at=body.accident_at,
    )
