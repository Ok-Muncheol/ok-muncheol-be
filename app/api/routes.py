"""B(통합): HTTP 경로 → 담당 서비스 함수. FE `src/lib/api/client.ts`의 `ApiClient`와 1:1로 맞춘다.

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
from app.schemas.reports import CalculationResult, ReviewReport

router = APIRouter()


class AnalysisRequest(BaseModel):
    storage_path: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    expected_version: int = Field(ge=1)


class JudgmentRequest(BaseModel):
    card_version: int = Field(ge=1)


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


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
@router.get("/cases/{case_id}/facts", tags=["A facts"])
async def get_fact_card(actor_id: ActorId, case_id: str) -> FactCard:
    return await facts.get_fact_card(actor_id, case_id)


@router.patch("/cases/{case_id}/facts", tags=["A facts"])
async def patch_fact_card(actor_id: ActorId, case_id: str, patch: FactPatch) -> FactCard:
    return await facts.patch_fact_card(actor_id, case_id, patch)


@router.post("/cases/{case_id}/facts/confirm", tags=["A facts"])
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


@router.post("/cases/{case_id}/answers", tags=["B judgments"])
async def answer_question(
    actor_id: ActorId, case_id: str, answer: QuestionAnswer
) -> dict[str, object]:
    return await judgments.answer_question(actor_id, case_id, answer)


@router.post("/cases/{case_id}/qa", tags=["B judgments"])
async def ask_question(actor_id: ActorId, case_id: str, body: AskRequest) -> dict[str, object]:
    return await judgments.ask_question(actor_id, case_id, body.text)


# ── D: 보고서·산정·구상 ───────────────────────────────────────────
@router.get("/cases/{case_id}/reports/{version}", tags=["D reports"])
async def get_report(actor_id: ActorId, case_id: str, version: int) -> ReviewReport:
    return await reports.get_report(actor_id, case_id, version)


@router.post("/cases/{case_id}/reports/{version}/approve", tags=["D reports"])
async def approve_report(actor_id: ActorId, case_id: str, version: int) -> ReviewReport:
    return await reports.approve_report(actor_id, case_id, version)


@router.post("/cases/{case_id}/reports/{version}/calculation", tags=["D reports"])
async def calculate_payment(actor_id: ActorId, case_id: str, version: int) -> CalculationResult:
    return await reports.calculate_payment(actor_id, case_id, version)


@router.post("/cases/{case_id}/reports/{version}/claim-export", tags=["D reports"])
async def export_claim(actor_id: ActorId, case_id: str, version: int) -> dict[str, object]:
    return await reports.export_claim(actor_id, case_id, version)
