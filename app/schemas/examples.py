"""전원: 비식별 합성 계약 견본의 단일 원천. 실제 사고·도표·금액을 담지 않는다."""

from app.schemas.common import ApiError, TaskView, VersionRef
from app.schemas.facts import FactCard, FactField
from app.schemas.judgment import ConfirmationQuestion, JudgmentResult
from app.schemas.reports import ReviewReport


def contract_examples() -> dict[str, object]:
    def field(value: str | int | None, confirmed: bool) -> FactField:
        return {
            "value": value, "model_value": None, "source": "manual",
            "model_confidence": None,
            "verification_status": "human_confirmed" if confirmed else "needs_review",
            "evidence_frame_ids": [], "source_document_ids": ["synthetic-statement"],
        }

    card: FactCard = {
        "case_id": "synthetic-case", "version": 2, "accident_date": "2026-11-01",
        "fields": {"accident_type": field("car_to_car", True), "party_count": field(2, True)},
        "party_mapping": {
            "video_ego_party_id": None, "insured_party_id": "synthetic-party-1",
            "chart_party_A_id": "synthetic-party-1", "chart_party_B_id": "synthetic-party-2",
        },
        "evidence_frames": [], "status": "confirmed",
        "confirmed_by": "synthetic-reviewer", "confirmed_at": "2026-11-05T09:00:00+09:00",
    }
    # 문서의 모든 필수 사고 필드를 확정한 완성 카드가 아닌, 계약 필드 모양을 보여 주는 양식이다.
    draft: FactCard = {
        **card, "version": 1, "fields": {"location_type": field(None, False)},
        "status": "draft", "confirmed_by": None, "confirmed_at": None,
    }
    versions: VersionRef = {
        "case_id": card["case_id"], "fact_card_version": 2,
        "judgment_version": 1, "report_version": 1,
    }
    question: ConfirmationQuestion = {
        "question_id": "synthetic-question", "field_key": "location_type",
        "text": "사고 장소의 유형을 확인해 주세요.",
        "options": ["교차로", "일반 도로"], "allow_unknown": True,
    }
    judgment: JudgmentResult = {
        "versions": versions, "status": "deferred", "chart_id": None, "edition": None,
        "fault_A": None, "fault_B": None, "calculation_log": [], "evidence": [],
        "checks": [{"layer": "evidence", "passed": False, "reasons": ["검수한 도표 근거 없음"]}],
        "questions": [], "reasons": ["합성 계약 양식: 검색/판단 미실행, 수치 미제공"],
    }
    report: ReviewReport = {
        "versions": versions, "judgment": judgment, "sections": [],
        "calculation": None, "recovery": None, "approval_status": "draft",
        "approved_by": None, "approved_at": None,
    }
    task: TaskView = {
        "task_id": "synthetic-task", "case_id": card["case_id"], "stage": "vision",
        "status": "failed", "input_version": 1,
    }
    conflict: ApiError = {
        "code": "FACT_CARD_VERSION_CONFLICT", "message": "최신 카드 버전을 다시 확인해 주세요.",
        "retryable": False,
    }
    return {
        "synthetic": True, "contract_version": "draft-1",
        "note": "필드 모양만 보여 주는 합성 양식. 실행 가능한 시나리오·검수 정답이 아님.",
        "fact_card_draft": draft, "fact_card_confirmed": card,
        "question": question, "judgment_deferred": judgment,
        "report_deferred": report, "task_failed": task, "version_conflict": conflict,
    }
