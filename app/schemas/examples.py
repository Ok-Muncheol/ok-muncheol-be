"""전원: 비식별 합성 계약 견본의 단일 원천. 실제 사고·도표·금액을 담지 않는다."""

from app.agents.qa import FACT_CHANGE_HINT
from app.schemas.chart_scope import FIELD_OPTIONS, REASON_NO_REVIEWED_CHART
from app.schemas.common import ApiError, TaskView, VersionRef
from app.schemas.facts import FactCard, FactField
from app.schemas.judgment import ConfirmationQuestion, JudgmentResult, JudgmentRunView, QaAnswer
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
        # 도표 선택 칸(chart_scope)과 그 외 칸의 모양. 값은 합성이다.
        "fields": {
            "accident_type": field("car_to_car", True), "party_count": field(2, True),
            "location_type": field("signalized_intersection", True),
            "movement_A": field("straight", True), "movement_B": field("left_turn", False),
            "signal_A": field("green", True), "signal_B": field("green", False),
        },
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
        "question_id": "synthetic-question", "field_key": "movement_B",
        "text": "상대 차량(B)의 진행 방향을 확인해 주세요.",
        "options": list(FIELD_OPTIONS["movement_B"]), "allow_unknown": True,
    }
    judgment: JudgmentResult = {
        "versions": versions, "status": "deferred", "chart_id": None, "edition": None,
        "fault_A": None, "fault_B": None, "calculation_log": [], "evidence": [],
        "checks": [{"layer": "evidence", "passed": False, "reasons": [REASON_NO_REVIEWED_CHART]}],
        "questions": [], "reasons": ["합성 계약 양식: 검색/판단 미실행, 수치 미제공"],
        "base_fault_A": None, "modifiers": [], "alternatives": [],
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
    # 판단 조회(GET /cases/{id}/judgment): 구분 질문으로 멈춘 실행.
    # result는 비어 있고 질문은 questions에 있다
    run_waiting: JudgmentRunView = {
        "task_id": "synthetic-run", "case_id": card["case_id"], "judgment_version": 1,
        "fact_card_version": card["version"], "criteria_version": "synthetic-criteria",
        "rule_version": "synthetic-rule", "status": "waiting_for_confirmation",
        "stage": "ask_or_resume", "rebuttal_retry_count": 0, "result": None, "error": None,
        "report_version": None, "questions": [{**question, "answer": None}],
        "updated_at": "2026-11-05T09:10:00+09:00",
    }
    # 근거 질의응답(POST /cases/{id}/qa): 판단 로그를 읽고 B 진행 방향을 가정 실행한 답.
    # 가정 결과는 hypotheticals에만 있고 공식 판정(judgment_deferred)은 그대로다
    qa_answer: QaAnswer = {
        "case_id": card["case_id"], "judgment_version": 1,
        "answer": "합성 견본: 공식 판정은 보류입니다 [E1]. 가정 결과도 같은 이유로 보류입니다.",
        "citations": [{
            "ref": "E1", "chunk_id": "synthetic-chunk", "parent_id": "synthetic-parent",
            "corpus": "fault_standards", "edition": "synthetic", "page_start": 1, "page_end": 1,
            "excerpt": "합성 발췌",
        }],
        "hypotheticals": [{
            "changes": {"movement_B": "straight"}, "status": "completed",
            "judgment_status": "deferred", "chart_id": None, "fault_A": None, "fault_B": None,
            "reasons": [REASON_NO_REVIEWED_CHART], "open_fields": [], "error": None,
        }],
        "tool_calls": [{"name": "get_judgment_log", "ok": True}, {"name": "simulate", "ok": True}],
        "fact_change_hint": FACT_CHANGE_HINT,
    }
    return {
        "synthetic": True, "contract_version": "draft-1",
        "note": "필드 모양만 보여 주는 합성 양식. 실행 가능한 시나리오·검수 정답이 아님.",
        "fact_card_draft": draft, "fact_card_confirmed": card,
        "question": question, "judgment_deferred": judgment,
        "report_deferred": report, "task_failed": task, "version_conflict": conflict,
        "judgment_run_waiting": run_waiting, "qa_answer": qa_answer,
    }
