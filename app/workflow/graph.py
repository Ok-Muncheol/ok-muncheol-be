"""B: 배선 초안. LangGraph·interrupt/resume·체크포인트는 미구현."""

from app.schemas.chart_scope import (
    IN_SCOPE_ACCIDENT_TYPES,
    IN_SCOPE_LOCATION_TYPES,
    PENDING_DECISION_LOCATION_TYPES,
    REASON_OUT_OF_SCOPE,
    REASON_PENDING_DECISION,
    unconfirmed_fields,
)

STAGES = (
    "require_confirmed_card",
    "check_chart_scope",
    "retrieve_chart_candidates",
    "ask_or_resume",
    "select_chart_and_modifiers",
    "check_evidence",
    "review_rebuttal",
    "calculate_and_validate_fault",
    "build_judgment_result",
)
MAX_REBUTTAL_RETRIES = 1


def scope_gate(fields: dict[str, dict[str, object]]) -> tuple[str | None, list[str]]:
    """(보류 사유, 질문할 칸). 범위 밖이면 보류, 범위 안이면 미확인 칸을 질문으로 넘긴다.

    docs/contracts/mvp-chart-scope.md 범위. 값은 human_confirmed 인 것만 본다.
    """
    def confirmed(key: str) -> object:
        f = fields.get(key, {})
        return f.get("value") if f.get("verification_status") == "human_confirmed" else None

    accident_type, location_type = confirmed("accident_type"), confirmed("location_type")
    if accident_type is None or location_type is None:
        return None, unconfirmed_fields(fields, "other")
    if location_type in PENDING_DECISION_LOCATION_TYPES:
        return REASON_PENDING_DECISION, []
    if accident_type not in IN_SCOPE_ACCIDENT_TYPES or location_type not in IN_SCOPE_LOCATION_TYPES:
        return REASON_OUT_OF_SCOPE, []
    return None, unconfirmed_fields(fields, str(location_type))


def build_graph() -> object:
    """C 검색 도구·검증 3겹 연결. 두 번째 반박 성립은 추가 확인으로 분기."""
    raise NotImplementedError("B: 고정 그래프·확인/재개·재실행 상한")
