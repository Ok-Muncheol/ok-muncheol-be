"""B: 배선 초안. LangGraph·interrupt/resume·체크포인트는 미구현."""

STAGES = (
    "require_confirmed_card",
    "retrieve_chart_candidates",
    "ask_or_resume",
    "select_chart_and_modifiers",
    "check_evidence",
    "review_rebuttal",
    "calculate_and_validate_fault",
    "build_judgment_result",
)
MAX_REBUTTAL_RETRIES = 1


def build_graph() -> object:
    """C 검색 도구·검증 3겹 연결. 두 번째 반박 성립은 추가 확인으로 분기."""
    raise NotImplementedError("B: 고정 그래프·확인/재개·재실행 상한")
