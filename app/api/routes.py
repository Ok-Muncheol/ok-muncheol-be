"""B(통합): HTTP 경로 → 담당 서비스 함수. FE `src/lib/api/client.ts`의 `ApiClient`와 1:1로 맞춘다.

경로는 README 「API 계약 제안」 표가 기준이다.
tests/api/test_app.py가 그 표·계약 문서와 서버 OpenAPI를 대조한다.

이 파일은 경로·인증·요청 모양만 정한다. 권한·상태·버전 판단은 각 담당 서비스 함수가 한다.
서비스는 모듈 속성으로 호출해(`cases.create_case`) 테스트에서 바꿔 끼울 수 있게 한다.
미구현 서비스는 `NotImplementedError` → 501 `not_implemented`로 응답한다(app/api/errors.py).
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api import cases, facts, judgments, reports
from app.api.deps import ActorId, IdempotencyKey
from app.api.errors import unprocessable, version_conflict
from app.schemas.facts import FactCard, FactPatch
from app.schemas.judgment import QuestionAnswer
from app.schemas.reports import ClaimResult, ConfirmResult, CounterpartyInfo, ReviewReport

router = APIRouter()


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


class AgreedFaultRequest(BaseModel):
    agreed_counterparty_fault: int | None = None
    fault_change_reason: str | None = None
    clear: bool = False


# ── A: 사건 접수·영상 ──────────────────────────────────────────────
@router.post("/cases", tags=["A cases"], status_code=201)
async def create_case(actor_id: ActorId, payload: dict[str, object]) -> dict[str, object]:
    return await cases.create_case(actor_id, payload)


@router.post("/cases/{case_id}/uploads", tags=["A cases"])
async def request_upload(actor_id: ActorId, case_id: str) -> dict[str, object]:
    return await cases.request_upload(actor_id, case_id)


@router.post("/cases/{case_id}/analysis", tags=["A cases"], status_code=202)
async def start_analysis(
    actor_id: ActorId, case_id: str, body: AnalysisRequest, idempotency_key: IdempotencyKey
) -> dict[str, object]:
    return await cases.start_analysis(actor_id, case_id, body.storage_path, idempotency_key)


@router.get("/cases/{case_id}", tags=["A cases"])
async def get_case(actor_id: ActorId, case_id: str) -> dict[str, object]:
    return await cases.get_case(actor_id, case_id)


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
    return await judgments.ask_question(actor_id, case_id, body.text)


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
    """Storage 경로의 견적서를 /tmp로 내려받아 reports.upload_estimate에 넘긴다(후속)."""
    raise NotImplementedError("B: 견적서 Storage 내려받기 — 업로드 서명 URL·서버 키 준비 뒤 연결")


@router.patch("/cases/{case_id}/agreed-fault", tags=["D reports"])
async def set_agreed_fault(
    actor_id: ActorId, case_id: str, body: AgreedFaultRequest
) -> ReviewReport:
    return await reports.set_agreed_fault(
        actor_id, case_id, agreed_counterparty_fault=body.agreed_counterparty_fault,
        fault_change_reason=body.fault_change_reason, clear=body.clear,
    )
