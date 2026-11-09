"""C: PDF → 페이지/표/제목 블록. 무거운 파서와 OCR은 함수 안에서 로딩한다."""

import hashlib
import json
from pathlib import Path

from app.schemas.retrieval import ParsedBlock, SourceDocument


def parse_pdf(
    path: Path, document: SourceDocument, *, page_start: int = 1, page_end: int | None = None
) -> list[ParsedBlock]:
    """텍스트와 표를 추출한다. 빈 텍스트도 남겨 OCR/수동 검수 대상으로 삼는다.

    표는 행 배열 JSON으로 보존한다. 페이지 텍스트에도 표 내용이 포함될 수
    있으므로 이 출력은 검수용 중간 데이터이며 바로 임베딩하지 않는다.
    이미지·도표 해석, OCR, 제목 추론은 아직 수행하지 않는다.
    """
    import pdfplumber

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if document["content_hash"] and document["content_hash"] != digest:
        raise ValueError("원문 content_hash가 파일 SHA-256과 다릅니다.")
    source = document.copy()
    source["content_hash"] = digest
    source["parser_version"] = f"pdfplumber-{pdfplumber.__version__}:v2-visible-dedup"
    blocks: list[ParsedBlock] = []
    with pdfplumber.open(path) as pdf:
        last = len(pdf.pages) if page_end is None else page_end
        if not 1 <= page_start <= last <= len(pdf.pages):
            raise ValueError(
                "페이지 범위가 올바르지 않습니다. PDF 실제 페이지(1부터)를 지정하세요."
            )
        for number in range(page_start, last + 1):
            original_page = pdf.pages[number - 1]
            page = original_page.within_bbox(original_page.bbox).dedupe_chars(tolerance=1)
            blocks.append(
                {
                    "document": source,
                    "page": number,
                    "kind": "text",
                    "text": page.extract_text() or "",
                }
            )
            for table in page.extract_tables():
                blocks.append(
                    {
                        "document": source,
                        "page": number,
                        "kind": "table",
                        "text": json.dumps(table, ensure_ascii=False),
                    }
                )
    return blocks
