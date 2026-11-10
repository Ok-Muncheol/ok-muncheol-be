"""C: data/private 코퍼스 산출물 → DB 적재 행(JSONL). 결과는 data/private/db_import/에만 쓴다.

테이블 계약은 docs/contracts/corpus-index.md.
결과에 원문·청크·벡터가 들어 있으므로 저장소에 커밋하지 않는다.
예전 app/tools/_evidence.py가 파일을 읽을 때 하던 적재 시점 무결성 검사를 여기서 한다.

    python scripts/export_corpus_index_rows.py [--manifests data/corpora]
"""

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "data" / "private"
FOLDERS = {
    "fault_standards": "01_fault_standards",
    "dispute_cases": "02_dispute_cases",
    "policy_terms": "03_policy_terms",
}
PARENT_FILES = {
    "fault_standards": [("charts.jsonl", "chart")],
    "dispute_cases": [("cases.jsonl", "case")],
    "policy_terms": [("clauses.jsonl", "clause"), ("appendices.jsonl", "appendix")],
}
DOCUMENT_COLUMNS = (
    "document_id", "source_uri", "source_name", "edition", "effective_from", "effective_to",
    "effective_period_status", "content_hash", "parser_version", "usage_review", "reviewed_by",
)
CHUNK_COLUMNS = ("chunk_id", "parent_id", "page_start", "page_end", "text", "metadata")


def read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def unique(rows, key, what):
    counts = Counter(key(row) for row in rows)
    dupes = [k for k, n in counts.items() if n > 1]
    if dupes:
        raise ValueError(f"Duplicate {what}: {dupes[:3]}")


def crosswalk_key(link: dict) -> str:
    # app/tools/_evidence.crosswalk_key와 같은 규칙
    return f"{link['legacy_chart_id']}|{link['chart_id']}|{link['source_pdf_page']}"


def record_row(corpus, kind, record_id, row, is_parent):
    return {
        "corpus": corpus,
        "kind": kind,
        "record_id": record_id,
        "parent_id": row.get("parent_id"),
        "is_parent": is_parent,
        "document_id": (row.get("document") or {}).get("document_id"),
        "chart_id": row.get("chart_id"),
        "page_start": row.get("page_start", row.get("source_pdf_page")),
        "page_end": row.get("page_end", row.get("source_pdf_page")),
        "body": row,
    }


def document_row(corpus: str, meta: dict) -> dict:
    """manifest 문서 → corpus_documents 행. 적용판·source_uri·이용조건은 manifest 값 그대로."""
    storage = meta["storage_object"]
    if meta["source_uri"] != f"{storage['bucket']}/{storage['path']}":
        raise ValueError(f"{meta['document_id']}: source_uri differs from storage_object path")
    if storage["relation"] == "original" and storage["sha256"] != meta["content_hash"]:
        raise ValueError(f"{meta['document_id']}: original upload must keep content_hash")
    return {
        "corpus": corpus,
        **{key: meta[key] for key in DOCUMENT_COLUMNS},
        "storage_sha256": storage["sha256"],
        "storage_bytes": storage["bytes"],
        "storage_relation": storage["relation"],
    }


def build(corpus: str, manifest_docs: dict) -> dict[str, list[dict]]:
    base = PRIVATE / FOLDERS[corpus]
    processed = base / "processed"
    chunks = read(processed / "chunks.jsonl")
    unique(chunks, lambda r: r["chunk_id"], f"{corpus} chunk_id")
    embedded = {row["chunk"]["chunk_id"]: row for row in read(processed / "embeddings.jsonl")}
    if embedded.keys() != {row["chunk_id"] for row in chunks}:
        raise ValueError(f"{corpus}: embeddings do not cover exactly the chunks")

    records, parents = [], {}
    for name, kind in PARENT_FILES[corpus]:
        for row in read(processed / name):
            parents[row["parent_id"]] = row
            record_id = row.get("chart_id") if kind == "chart" else row["parent_id"]
            records.append(record_row(corpus, kind, record_id, row, True))
    unique(records, lambda r: r["parent_id"], f"{corpus} parent_id")
    indexed_parents = {row["parent_id"] for row in chunks}

    if corpus == "fault_standards":
        charts = {row["chart_id"] for row in parents.values()}
        modifiers = read(processed / "modifiers.jsonl")
        if any(row["chart_id"] not in charts for row in modifiers):
            raise ValueError("Modifier references missing chart ID")
        modifier_ids = {row["modifier_id"] for row in modifiers}
        if any(not set(row["modifier_ids"]) <= modifier_ids for row in parents.values()):
            raise ValueError("Chart references missing modifier IDs")
        records += [record_row(corpus, "modifier", r["modifier_id"], r, False) for r in modifiers]
        attributes = read(processed / "attributes.jsonl")
        if any(row["chart_id"] not in charts for row in attributes):
            raise ValueError("Attribute references missing chart ID")
        records += [
            record_row(corpus, "attribute", f"{r['chart_id']}:attr:{i}", r, False)
            for i, r in enumerate(attributes)
        ]
        if any(parent not in indexed_parents for parent in parents):
            raise ValueError("Chart lacks searchable chunks")
    elif corpus == "dispute_cases":
        mappings = read(processed / "chart_mappings.jsonl")
        keys = {crosswalk_key(row) for row in mappings}
        for row in parents.values():
            if row["parent_id"] not in indexed_parents:
                raise ValueError("Case lacks searchable chunks")
            if any(crosswalk_key(link) not in keys for link in row["mapping_evidence"]):
                raise ValueError("Case chart link is absent from source crosswalk")
        # 같은 (구 도표, 새 도표, 쪽)이 변형별로 여러 줄일 수 있다. 키 하나로 묶는다.
        grouped: dict[str, list[dict]] = {}
        for row in mappings:
            grouped.setdefault(crosswalk_key(row), []).append(row)
        for key, rows in grouped.items():
            first = rows[0]
            records.append({
                **record_row(corpus, "chart_mapping", key, first, False),
                "body": {"links": rows},
            })
    else:
        for row in parents.values():
            if "clause_id" not in row:
                continue
            if row["parent_id"] not in indexed_parents:
                raise ValueError("Clause lacks searchable chunks")
            if any(note not in parents for note in row["side_note_parent_ids"]):
                raise ValueError("Clause references missing source note")
    unique(records, lambda r: (r["kind"], r["record_id"]), f"{corpus} record_id")

    pages, documents = set(), {}
    for block in read(base / "parsed" / "blocks.jsonl"):
        doc = block["document"]
        seen = documents.setdefault(doc["document_id"], doc)
        if seen["content_hash"] != doc["content_hash"]:
            raise ValueError("Parsed blocks have conflicting document hashes")
        pages.add((doc["document_id"], block["page"], doc["content_hash"]))

    document_rows = []
    for document_id, doc in documents.items():
        meta = manifest_docs.get(document_id)
        if meta is None:
            raise ValueError(f"{document_id} is missing from {corpus} manifest")
        if meta["content_hash"] != doc["content_hash"]:
            raise ValueError(f"{document_id}: manifest content_hash differs from parsed PDF")
        document_rows.append(document_row(corpus, meta))

    page_set = {(d, p) for d, p, _ in pages}
    chunk_rows = []
    for row in chunks:
        doc_id = row["document"]["document_id"]
        if row["document"]["content_hash"] != documents[doc_id]["content_hash"]:
            raise ValueError("Chunk document differs from parsed PDF source")
        if any((doc_id, p) not in page_set for p in range(row["page_start"], row["page_end"] + 1)):
            raise ValueError(f"Chunk page is absent from parsed PDF blocks: {row['chunk_id']}")
        emb = embedded[row["chunk_id"]]
        chunk_rows.append({
            "corpus": corpus,
            "document_id": doc_id,
            **{column: row[column] for column in CHUNK_COLUMNS},
            "raw_text": row.get("raw_text"),
            "display_text": row.get("display_text"),
            "embedding_model": emb["embedding_model"],
            "embedding_dimensions": emb["embedding_dimensions"],
            "embedding_version": emb["embedding_version"],
            "embedding_content_hash": emb["embedding_content_hash"],
            "embedding": emb["embedding"],
        })
    return {
        "corpus_documents": document_rows,
        "corpus_source_pages": [
            {"document_id": d, "page": p, "content_hash": h} for d, p, h in sorted(pages)
        ],
        "corpus_records": records,
        "corpus_chunks": chunk_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="코퍼스 산출물 → DB 적재 행")
    parser.add_argument("--manifests", type=Path, default=ROOT / "data" / "corpora")
    parser.add_argument("--output", type=Path, default=PRIVATE / "db_import")
    args = parser.parse_args()
    if PRIVATE not in args.output.resolve().parents:
        parser.error("출력은 data/private/ 아래에만 쓴다(원문·벡터 포함)")
    tables: dict[str, list[dict]] = {}
    for corpus in FOLDERS:
        manifest = json.loads((args.manifests / corpus / "manifest.json").read_text("utf-8"))
        docs = {d["document_id"]: d for d in manifest["documents"]}
        for table, rows in build(corpus, docs).items():
            tables.setdefault(table, []).extend(rows)
    args.output.mkdir(parents=True, exist_ok=True)
    for table, rows in tables.items():
        with (args.output / f"{table}.jsonl").open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{table}: {len(rows)}")


if __name__ == "__main__":
    main()
