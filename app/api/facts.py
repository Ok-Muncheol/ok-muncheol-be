"""A: 사실 카드 조회·수정·확정. actor_id는 인증 계층에서 받아야 한다."""

from app.schemas.facts import FactCard, FactPatch


async def get_fact_card(actor_id: str, case_id: str) -> FactCard:
    raise NotImplementedError("A: 담당 사건 카드 조회")


async def patch_fact_card(actor_id: str, case_id: str, patch: FactPatch) -> FactCard:
    """기대 버전 검사·원값 보존·이력·새 버전 생성. 기존 승인은 승계하지 않는다."""
    raise NotImplementedError("A: 카드 수정·충돌 검사·이력 저장")


async def confirm_fact_card(actor_id: str, case_id: str, expected_version: int) -> FactCard:
    """핵심 불명·충돌·당사자 매핑 검사 후 담당자/시각과 함께 확정."""
    raise NotImplementedError("A: 담당자 확인·버전 고정")
