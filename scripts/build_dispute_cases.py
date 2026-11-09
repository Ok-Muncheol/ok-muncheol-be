"""② 사건별 구조화 및 ① 변경대비표 기반 도표 연결 후보 생성."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ingestion.preprocess import preprocess_chunks


def read(path):
    return [json.loads(s) for s in Path(path).read_text(encoding="utf-8").splitlines() if s.strip()]


def write(folder, name, rows):
    (folder / f"{name}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("blocks", type=Path)
    p.add_argument("--standards-blocks", type=Path, required=True)
    p.add_argument("--charts", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    blocks, standards, charts = read(a.blocks), read(a.standards_blocks), read(a.charts)
    mappings = []
    for b in standards:
        if b["kind"] != "table":
            continue
        table = json.loads(b["text"])
        if not table or "(기존)기준 번호" not in table[0]:
            continue
        for row in table[1:]:
            if len(row) >= 4 and row[1] and row[3] and re.fullmatch(r"차\d+-\d+", row[3].strip()):
                mappings.append(
                    {
                        "legacy_chart_id": row[1].strip(),
                        "legacy_variant": row[2] or None,
                        "chart_id": row[3].strip(),
                        "note": row[4] if len(row) > 4 else None,
                        "source_document": b["document"],
                        "source_pdf_page": b["page"],
                    }
                )
    index = defaultdict(list)
    for row in mappings:
        index[row["legacy_chart_id"]].append(row)
    chart_index = {c["chart_id"]: c for c in charts}
    pages = [b for b in blocks if b["kind"] == "text"]
    source_pdf = pdfplumber.open(
        unquote(urlparse(pages[0]["document"]["source_uri"]).path).lstrip("/")
    )
    groups = []
    for b in pages:
        match = re.search(r"심의번호\s*(\d{4}-\d+)", b["text"])
        if match and (not groups or groups[-1][0] != match[1]):
            groups.append((match[1], b["page"]))
    records, chunks = [], []
    section_re = re.compile(r"(?m)^\s*(입증 자료|주요 쟁점|결정 근거|결정 이유)\s*$")
    for n, (case_id, start) in enumerate(groups):
        end = (
            groups[n + 1][1] - 1
            if n + 1 < len(groups)
            else max(b["page"] for b in pages if b["text"].strip())
        )
        case_pages = [
            b
            for b in pages
            if start <= b["page"] <= end and not re.fullmatch(r"\s*제\d장[\s\S]{0,35}", b["text"])
        ]
        first = case_pages[0]
        text = first["text"]
        parent = f"{first['document']['document_id']}:{case_id}"
        ratio = re.search(
            r"결정비율\s*A\(([^)]+)\)\s*:\s*B\(([^)]+)\)\s*=\s*(\d+)\s*:\s*(\d+)", text
        )
        table = next(
            (
                json.loads(b["text"])
                for b in blocks
                if b["kind"] == "table" and b["page"] == start and "참고기준" in b["text"]
            ),
            None,
        )
        refs = []
        if table:
            ref_raw = " ".join(str(r[-1] or "") for r in table)
            refs = list(dict.fromkeys(re.findall(r"(?<!\d)(\d{3}(?:-\d+)?)(?!\d)", ref_raw)))
        title = table[0][0] if table else None
        variant_match = re.search(r"\(([가-힣])\)", title or "")
        legacy_variant = variant_match[1] if variant_match else None
        adopted_refs = re.findall(r"도표\s*(\d{3}(?:-\d+)?)\s*준용", title or "")
        mapping_evidence = [m for ref in refs for m in index[ref]]
        if not mapping_evidence and adopted_refs:
            mapping_evidence = [
                m
                for m in mappings
                if any(
                    m["legacy_chart_id"].startswith(f"{ref}-{adopted}")
                    for ref in refs
                    for adopted in adopted_refs
                )
            ]
            if not mapping_evidence:
                mapping_evidence = [m for adopted in adopted_refs for m in index[adopted]]
        candidates = sorted(
            {m["chart_id"] for m in mapping_evidence if m["chart_id"] in chart_index}
        )
        mapping_method = "appendix_number"
        if len(candidates) > 1 and legacy_variant:
            specific = [m for m in mapping_evidence if m["legacy_variant"] == legacy_variant]
            if specific:
                mapping_evidence = specific
                candidates = sorted({m["chart_id"] for m in specific})
                mapping_method = "appendix_number_and_variant"
        # 228은 동작에 따라 신번호가 나뉜다. 사례 헤더의 A/B 동작과 비교한다.
        if len(candidates) > 1 and refs == ["228"] and table:
            actions = " ".join(str(c or "") for c in table[0][1:-1])
            selected = (
                "차5-1"
                if "우회전" in actions and "직진(좌" not in actions
                else "차6-1"
                if "직진" in actions and "우회전" not in actions
                else None
            )
            if selected in candidates:
                candidates = [selected]
                mapping_evidence = [m for m in mapping_evidence if m["chart_id"] == selected]
                mapping_method = "appendix_and_case_movement_header"
        issues = ["unreviewed_source", "legacy_mapping_requires_fact_check", "edition_unconfirmed"]
        if len(candidates) != 1:
            issues.append("chart_mapping_ambiguous" if candidates else "chart_mapping_missing")
        if not ratio or int(ratio[3]) + int(ratio[4]) != 100:
            issues.append("decision_ratio_unresolved")
        sections = defaultdict(list)
        for b in case_pages:
            matches = list(section_re.finditer(b["text"]))
            for i, m in enumerate(matches):
                body = b["text"][
                    m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(b["text"])
                ].strip()
                sections[m[1]].append({"page": b["page"], "text": body})
        if not sections["결정 이유"]:
            issues.append("reasoning_missing")
        overview = text[
            text.find("사례 개요") : text.find("주장 내용") if "주장 내용" in text else len(text)
        ]
        raw_page = source_pdf.pages[start - 1]
        visible = raw_page.within_bbox(raw_page.bbox).dedupe_chars(tolerance=1)
        facts = None
        for source_table in visible.find_tables():
            rows = source_table.extract()
            for row_index, row in enumerate(rows):
                if row and row[0] and row[0].strip() == "사고내용":
                    label = source_table.rows[row_index].bbox
                    box = (label[2] + 1, label[1], visible.width - 20, label[3])
                    facts = (visible.crop(box).extract_text() or "").strip()
                    break
            if facts:
                break
        if not facts:
            issues.append("facts_region_unresolved")
        header_cells = table[0] if table else []
        header_values = [str(value or "").strip().replace("\n", " ") for value in header_cells]
        header_context = ""
        # 이 원문의 표 열 순서: 제목, 신호, 장소, A동작, B동작, 참고기준.
        if len(header_values) == 6:
            fact_title = re.sub(r"\((?:기본|수정)과실(?:\s*준용)?\)", "", header_values[0]).strip()
            header_context = "\n".join(
                [
                    f"사례 유형: {fact_title}",
                    f"신호 조건(원문): {header_values[1]}",
                    f"장소(원문): {header_values[2]}",
                    f"도표 A 동작(원문): {header_values[3]}",
                    f"도표 B 동작(원문): {header_values[4]}",
                ]
            )
        "\n".join(value for value in [header_context, facts] if value)
        if refs == ["228"] and len(candidates) > 1 and facts:
            # 이 구도표는 A=비신호측 차량의 실제 진행 방향으로 구분한다.
            role = ratio[1].strip() if ratio else None
            compact_facts = re.sub(r"\s+", "", facts)
            motion = (
                re.search(
                    rf"{re.escape(role)}차량(?:이|은).{{0,40}}?(직진|우회전|좌회전)", compact_facts
                )
                if role
                else None
            )
            selected = ("차5-1" if motion[1] == "우회전" else "차6-1") if motion else None
            if selected in candidates:
                candidates = [selected]
                mapping_method = "appendix_and_A_movement_from_facts"
                mapping_evidence = [m for m in mapping_evidence if m["chart_id"] == selected]
                issues = [s for s in issues if s != "chart_mapping_ambiguous"]
        record = {
            "case_id": case_id,
            "parent_id": parent,
            "document": first["document"],
            "page_start": start,
            "page_end": case_pages[-1]["page"],
            "title": title,
            "adopted_legacy_chart_ids": adopted_refs,
            "legacy_variant": legacy_variant,
            "mapping_method": mapping_method,
            "facts": facts,
            "facts_source_pdf_page": start,
            "source_header_cells": header_cells,
            "source_header_context": header_context,
            "card_similarity_text": facts,
            "card_similarity_status": "facts_only_unreviewed",
            "overview_raw": overview,
            "overview_contains_reference_standard": True,
            "decision_ratio": {"A": int(ratio[3]), "B": int(ratio[4])} if ratio else None,
            "party_mapping": {"A": ratio[1].strip(), "B": ratio[2].strip()} if ratio else None,
            "legacy_chart_ids": refs,
            "chart_id_candidates": candidates,
            "related_chart_ids": [],
            "mapping_status": "candidate_only",
            "mapping_evidence": mapping_evidence,
            "sections": dict(sections),
            "review_status": "unreviewed",
            "issues": issues,
            "split_group_id": f"dispute:{case_id}",
        }
        records.append(record)
        units = [("사고 사실", start, facts)] + [
            (name, x["page"], x["text"]) for name, rows in sections.items() for x in rows
        ]
        for i, (name, page, body) in enumerate(units):
            if not body:
                continue
            chunks.append(
                {
                    "chunk_id": hashlib.sha256(f"{parent}:{page}:{name}:{i}".encode()).hexdigest()[
                        :24
                    ],
                    "parent_id": parent,
                    "document": first["document"],
                    "page_start": page,
                    "page_end": page,
                    "text": f"심의번호 {case_id}\n{name}\n{body}",
                    "metadata": {
                        "case_id": case_id,
                        "section": name,
                        "legacy_chart_ids": refs,
                        "chart_id_candidates": candidates,
                        "related_chart_ids": [],
                        "mapping_status": "candidate_only",
                        "embedding_ready": False,
                        "retrieval_role": "card_similarity"
                        if name == "사고 사실"
                        else "supporting_evidence",
                        "source_header_context": header_context if name == "사고 사실" else None,
                        "review_status": "unreviewed",
                    },
                }
            )
    source_pdf.close()
    chunks = preprocess_chunks(chunks)
    a.output.mkdir(parents=True, exist_ok=True)
    write(a.output, "cases", records)
    write(a.output, "chunks", chunks)
    write(a.output, "chart_mappings", mappings)
    summary = {
        "cases": len(records),
        "chunks": len(chunks),
        "mapping_rows": len(mappings),
        "issues": dict(Counter(i for r in records for i in r["issues"])),
        "status": "unreviewed",
    }
    (a.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
