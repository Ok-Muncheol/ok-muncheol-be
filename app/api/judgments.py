"""B: 확정 카드 판단 접수·질문 응답·질의응답. HTTP 연결은 후속 작업."""

from app.schemas.facts import FactCard
from app.schemas.judgment import QuestionAnswer


async def start_judgment(
    actor_id: str, card: FactCard, idempotency_key: str
) -> dict[str, object]:
    """권한·카드 확정/버전 검사 후 고정 그래프 실행 또는 재개."""
    raise NotImplementedError("B: 판단 접수·체크포인트")


async def answer_question(
    actor_id: str, case_id: str, answer: QuestionAnswer
) -> dict[str, object]:
    """답변은 A의 카드 수정/확정 경로로 연결. 곧바로 판단을 확정하지 않는다."""
    raise NotImplementedError("B+A: 질문 응답·카드 재확정·재검색")


async def ask_question(actor_id: str, case_id: str, text: str) -> dict[str, object]:
    raise NotImplementedError("B: 1차 확장 근거 질의응답 API")
