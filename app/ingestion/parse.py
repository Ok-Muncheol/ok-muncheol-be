"""C: PDF → 페이지/표/제목 블록. 무거운 파서와 OCR은 함수 안에서 로딩한다."""

from pathlib import Path

from app.schemas.retrieval import ParsedBlock, SourceDocument


def parse_pdf(path: Path, document: SourceDocument) -> list[ParsedBlock]:
    """텍스트 PDF/스캔 OCR 분기. 페이지·표 구조·원문 hash·파서 버전 보존."""
    raise NotImplementedError("C: PDF 파싱·OCR·숫자 원문 대조")
