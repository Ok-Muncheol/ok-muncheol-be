"""C: 오프라인 코퍼스 구축. API 요청 중 PDF 전체를 처리하지 않는다."""

from pathlib import Path

from app.schemas.retrieval import SourceDocument


async def ingest_document(path: Path, document: SourceDocument) -> dict[str, object]:
    """파싱 → 구조 검수 → 청킹 → 임베딩 → 적재/재개. 이용조건부터 확인."""
    raise NotImplementedError("C: 수집 manifest·검수·색인 적재·재개")
