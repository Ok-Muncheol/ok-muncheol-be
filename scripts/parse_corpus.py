"""실행: python scripts/parse_corpus.py --help (project3 기준)."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingestion.parse import parse_pdf  # noqa: E402 -- standalone CLI adds repository root before app imports
from app.schemas.retrieval import SourceDocument  # noqa: E402 -- standalone CLI adds repository root before app imports


def main() -> None:
    parser = argparse.ArgumentParser(description="PDF → 검수용 페이지 텍스트·표 JSONL")
    parser.add_argument("pdf", type=Path)
    parser.add_argument(
        "--metadata",
        type=Path,
        required=True,
        help="SourceDocument 형식 JSON. 예: data/corpora/source.example.json",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="새 출력 폴더. 기존 폴더는 덮어쓰지 않습니다."
    )
    parser.add_argument("--page-start", type=int, default=1)
    parser.add_argument("--page-end", type=int)
    args = parser.parse_args()
    document: SourceDocument = json.loads(args.metadata.read_text(encoding="utf-8-sig"))
    required = set(SourceDocument.__required_keys__)
    if required - document.keys():
        parser.error(f"필수 메타데이터 누락: {sorted(required - document.keys())}")
    if document["corpus"] not in ("fault_standards", "dispute_cases", "policy_terms"):
        parser.error("지원하지 않는 corpus입니다.")
    for field in required - {"effective_to"}:
        if not isinstance(document[field], str):
            parser.error(f"{field}는 문자열이어야 합니다.")
    for field in ("document_id", "source_uri", "edition"):
        if not document[field].strip():
            parser.error(f"{field}를 입력하세요.")
    start = date.fromisoformat(document["effective_from"]) if document["effective_from"] else None
    if document["effective_to"] is not None:
        end = date.fromisoformat(document["effective_to"])
        if start is None or end < start:
            parser.error("effective_to가 effective_from보다 빠릅니다.")
    if args.output.exists():
        parser.error("출력 폴더가 이미 있습니다. 새 경로를 지정하세요.")
    blocks = parse_pdf(args.pdf, document, page_start=args.page_start, page_end=args.page_end)
    pages = []
    for block in blocks:
        if block["kind"] != "text":
            continue
        text = block["text"]
        reasons = []
        if not text.strip():
            reasons.append("no_text: OCR 또는 원문 확인 필요")
        if "\ufffd" in text or "\x00" in text:
            reasons.append("broken_characters: 문자 추출 확인 필요")
        pages.append({"page": block["page"], "characters": len(text), "issues": reasons})
    report = {
        "status": "unreviewed",
        "pages": pages,
        "block_count": len(blocks),
        "table_count": sum(b["kind"] == "table" for b in blocks),
        "notes": [
            "검수 전 청킹·임베딩 금지",
            "페이지 텍스트와 표 내용 중복 가능",
            "effective_from 빈 값은 적용 시작일 미확인. 색인 전 확인 필요",
            "자동 검사는 품질 보증이 아님. 도표 이미지·A/B·숫자·페이지를 원문 대조",
        ],
    }
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "blocks.jsonl").write_text(
        "".join(json.dumps(b, ensure_ascii=False) + "\n" for b in blocks), encoding="utf-8"
    )
    (args.output / "review.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"저장: {args.output} / {len(pages)}페이지, {len(blocks)}블록 (미검수)")


if __name__ == "__main__":
    main()
