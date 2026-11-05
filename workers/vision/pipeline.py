"""A: 로컬/Modal 공통 진입점. 작업 상태·재시도·저장은 아직 미구현."""

from app.schemas.facts import FactCard


async def analyze_video(case_id: str, storage_path: str, input_version: int) -> FactCard:
    """비공개 파일 조회 → 탐지/추적 → 카드 추출 → 원값/버전 저장."""
    raise NotImplementedError("A: 비전 파이프라인·Modal/큐 연결")
