"""③ 제공된 삼성화재 개인용 약관: 조항·특약 단위의 검수용 레코드."""

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import pdfplumber
from build_dispute_cases import read, write

from app.ingestion import injury_property_values, policy_appendix
from app.ingestion.policy_layout import extract_columns
from app.ingestion.preprocess import preprocess_chunks
from app.ingestion.retrieval_metadata import normalize_document, policy_coverage


def main():
    p = argparse.ArgumentParser()
    p.add_argument("blocks", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--source-pdf", type=Path, help="Local PDF path, separate from display citation")
    a = p.parse_args()
    blocks = read(a.blocks)
    pages = [b for b in blocks if b["kind"] == "text"]
    doc = normalize_document(pages[0]["document"])
    if doc["document_id"] != "samsung-personal-auto-2026-11-11":
        p.error("이 스크립트의 페이지 경계는 제공된 삼성화재 약관 전용입니다.")
    # 목차·가이드와 본문을 구별한다. 다른 개정판은 경계 재확인이 필요하다.
    starts = []
    layouts = {}
    # 중간 목차는 원문 블록에 보존하되 조항 본문/비고로 색인하지 않는다.
    contents_pages = {58, 70, 78}
    source = a.source_pdf or unquote(urlparse(doc["source_uri"]).path).lstrip("/")
    # 이 PDF의 본문 제목(9.7pt)과 목차/주석 글꼴을 구별한다.
    with pdfplumber.open(source) as pdf:
        for b in pages:
            page = b["page"]
            if page in contents_pages:
                b["text"] = ""
                continue
            if not (54 <= page <= 89 or 122 <= page <= 263):
                continue
            raw = pdf.pages[page - 1]
            layout = extract_columns(raw, page)
            layouts[page] = layout
            b["text"] = layout["body"]
            text = b["text"]
            headings = layout["headings"]
            if page <= 89:
                for m in re.finditer(r"(?m)^제\s*(\d+)\s*조\s*\(([^\n]*)", headings):
                    match = re.search(rf"제\s*{m[1]}\s*조\s*\(", text)
                    if match:
                        starts.append((page, match.start(), "general_article", m[1], m[0].strip()))
            else:
                for m in re.finditer(r"\[(\d+(?:-\d+)?)\]\s*([^\[]*?특별약관)", headings):
                    match = re.search(rf"\[{m[1]}\]", text)
                    if match:
                        starts.append(
                            (page, match.start(), "rider", m[1], re.sub(r"\s+", " ", m[2]))
                        )
    starts.sort(key=lambda row: (row[0], row[1]))
    records, chunks = [], []
    for i, (page, offset, kind, number, title) in enumerate(starts):
        next_start = starts[i + 1] if i + 1 < len(starts) else (264, 0, None, None, None)
        end_page, end_offset = next_start[:2]
        if kind == "general_article" and end_page > 89:
            end_page, end_offset = 90, 0
        pieces = []
        for b in pages:
            if not page <= b["page"] <= end_page:
                continue
            body = b["text"]
            if b["page"] == end_page:
                body = body[:end_offset]
            if b["page"] == page:
                body = body[offset:]
            if body.strip():
                pieces.append((b["page"], body.strip()))
        if not pieces:
            continue
        parent = f"{doc['document_id']}:{kind}:{number}:p{page}"
        body = "\n".join(t for _, t in pieces)
        # 아래 맥락은 본문의 절 제목에서 확인한 문서 구조이며 계약 적용 판정은 아님.
        context = None
        context_page = None
        if kind == "general_article":
            for lower, upper, name, source_page in [
                (3, 5, "대인배상Ⅰ", 59),
                (6, 11, "대인배상Ⅱ와 대물배상", 59),
                (12, 16, "자기신체사고", 62),
                (17, 20, "무보험자동차에 의한 상해", 64),
                (21, 24, "자기차량손해", 67),
            ]:
                if lower <= int(number) <= upper:
                    context, context_page = name, source_page
                    break
        # 내용에 단어가 있다는 이유만으로 담보를 확정하지 않는다.
        record = {
            "clause_id": parent,
            "parent_id": parent,
            "document": doc,
            "clause_type": kind,
            "article_number": number if kind == "general_article" else None,
            "rider_number": number if kind == "rider" else None,
            "title": title,
            "insurer": "삼성화재",
            "product": "개인용 애니카 자동차보험",
            "page_start": page,
            "page_end": pieces[-1][0],
            "text": body,
            "layout_version": "samsung-20261111-columns-v1",
            "coverage_context": context,
            "coverage_context_source_page": context_page,
            "coverage_context_status": "document_section_only_not_contract_applicability",
            "side_note_parent_ids": [
                f"{doc['document_id']}:side_note:p{p}"
                for p, _ in pieces
                if layouts.get(p, {}).get("side_note", "").replace("* 비고", "").strip()
            ],
            "coverage": None,
            "coverage_mentions": [
                s
                for s in [
                    "자기차량손해",
                    "대물배상",
                    "대인배상",
                    "자동차상해",
                    "자기신체사고",
                    "무보험자동차",
                ]
                if s in body
            ],
            "cross_reference_numbers": sorted(set(re.findall(r"제\s*(\d+)\s*조", body))),
            "cross_reference_status": "unresolved_context",
            "review_status": "unreviewed",
            "contract_applicability": "unconfirmed",
            "issues": [
                "unreviewed_source",
                "coverage_scope_unconfirmed",
                "contract_edition_requires_confirmation",
            ],
        }
        record.update(policy_coverage(record))
        records.append(record)
        for j, (source_page, text) in enumerate(pieces):
            chunks.append(
                {
                    "chunk_id": hashlib.sha256(f"{parent}:{j}".encode()).hexdigest()[:24],
                    "parent_id": parent,
                    "document": doc,
                    "page_start": source_page,
                    "page_end": source_page,
                    "text": f"{context + ' | ' if context else ''}{title}\n{text}",
                    "metadata": {
                        "clause_type": kind,
                        "clause_id": parent,
                        "article_number": record["article_number"],
                        "rider_number": record["rider_number"],
                        **policy_coverage(record),
                        "coverage_mentions": record["coverage_mentions"],
                        "side_note_parent_ids": record["side_note_parent_ids"],
                        "layout_version": "samsung-20261111-columns-v1",
                        "coverage_context": context,
                        "coverage_context_source_page": context_page,
                        "retrieval_role": "primary_clause",
                        "requires_parent_context": len(pieces) > 1,
                        "review_status": "unreviewed",
                        "embedding_ready": False,
                    },
                }
            )
    appendix = []
    for page, layout in layouts.items():
        note = layout["side_note"].replace("* 비고", "").strip()
        if not note:
            continue
        parent = f"{doc['document_id']}:side_note:p{page}"
        related = [r["parent_id"] for r in records if r["page_start"] <= page <= r["page_end"]]
        appendix.append(
            {
                "parent_id": parent,
                "document": doc,
                "page_start": page,
                "page_end": page,
                "text": note,
                "record_type": "side_note",
                "related_parent_ids": related,
                "relationship_status": "same_page_not_exact_clause_mapping",
                "source_bbox": layout["side_note_bbox"],
                "review_status": "unreviewed",
            }
        )
        chunks.append(
            {
                "chunk_id": hashlib.sha256(parent.encode()).hexdigest()[:24],
                "parent_id": parent,
                "document": doc,
                "page_start": page,
                "page_end": page,
                "text": "약관 비고·각주\n" + note,
                "metadata": {
                    "clause_type": "side_note",
                    "related_parent_ids": related,
                    "relationship_status": "same_page_not_exact_clause_mapping",
                    "layout_version": layout["version"],
                    "review_status": "unreviewed",
                    "retrieval_role": "auxiliary_reference",
                    "embedding_ready": False,
                },
            }
        )
    # 보험금 지급기준 별표(p.91–105)는 표 칸 좌표로 항목 단위 레코드·청크를 만든다.
    with pdfplumber.open(source) as pdf:
        segments = []
        for page in policy_appendix.SCHEDULE_PAGES:
            segments += policy_appendix.page_segments(pdf.pages[page - 1], page)
    items = policy_appendix.assemble_items(segments)
    schedule_records, schedule_chunks = policy_appendix.build_schedule(items, doc)
    cited_records, cited_chunks = policy_appendix.build_cited(
        [b for b in pages if 106 <= b["page"] <= 119], doc
    )
    appendix += schedule_records + cited_records
    chunks += schedule_chunks + cited_chunks
    by_label = {(i["schedule"]["number"], i["label"].split(".")[0]): i for i in items}

    def item_lines(number, letter):
        return [line for _, line in by_label[(number, letter)]["lines"]]

    values = policy_appendix.passenger_reduction_records(
        "\n".join(item_lines(4, "가") + item_lines(4, "나")), doc, 104
    )
    values += policy_appendix.amount_table_records(
        item_lines(5, "가"), doc, 105, "별표5-가-상해", policy_appendix.INJURY_COLUMNS
    )
    values += policy_appendix.amount_table_records(
        item_lines(5, "나"), doc, 105, "별표5-나-후유장애", policy_appendix.DISABILITY_COLUMNS
    )
    # 대인·대물 1차 계산 숫자(별표1·2·3, D). 원문과 숫자·문장이 다르면 ValueError로 멈춘다.
    values += injury_property_values.records(
        {r["parent_id"]: r["text"] for r in schedule_records + cited_records}, doc
    )
    for b in pages:
        if 264 <= b["page"] <= 293:
            parent = f"{doc['document_id']}:appendix:p{b['page']}"
            appendix.append(
                {
                    "parent_id": parent,
                    "document": doc,
                    "page_start": b["page"],
                    "page_end": b["page"],
                    "text": b["text"],
                    "review_status": "unreviewed",
                    "record_type": "appendix_page",
                }
            )
            chunks.append(
                {
                    "chunk_id": hashlib.sha256(parent.encode()).hexdigest()[:24],
                    "parent_id": parent,
                    "document": doc,
                    "page_start": b["page"],
                    "page_end": b["page"],
                    "text": "자동차보험약관 인용 법조문\n" + b["text"],
                    "metadata": {
                        "clause_type": "cited_law_appendix",
                        "review_status": "unreviewed",
                        "retrieval_role": "auxiliary_reference",
                        "embedding_ready": False,
                        "requires_parent_context": True,
                    },
                }
            )
    chunks = preprocess_chunks(chunks)
    a.output.mkdir(parents=True, exist_ok=True)
    write(a.output, "clauses", records)
    write(a.output, "chunks", chunks)
    write(a.output, "appendices", appendix)
    write(a.output, "schedule_values", values)
    summary = {
        "general_articles": sum(r["clause_type"] == "general_article" for r in records),
        "riders": sum(r["clause_type"] == "rider" for r in records),
        "chunks": len(chunks),
        "appendix_pages": sum(r["record_type"] == "appendix_page" for r in appendix),
        "side_note_pages": sum(r["record_type"] == "side_note" for r in appendix),
        "payment_standard_items": len(schedule_records),
        "cited_law_schedules": len(cited_records),
        "schedule_values": len(values),
        "status": "unreviewed",
        "limitations": [
            "문서 전용 페이지 경계 사용",
            "별표4·5 수치 구조 레코드는 기계 대조만 했고 calculation_ready=false",
            "별표1·2·3 대인·대물 숫자(52개)는 PDF와 대조 검수해 calculation_ready=true",
            "그 밖의 별표 수치는 청크 서술로만 있고 구조 레코드가 아님",
            "항·호와 단서의 자동 의미 연결은 미완료",
            "담보 mentions는 적용 담보 확정값이 아님",
        ],
    }
    (a.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
