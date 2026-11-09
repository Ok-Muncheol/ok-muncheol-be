"""Corpus record and source-page contract for the three search tools.

The index is immutable within a process. Restart a worker after replacing corpus files.
"""

import json
import math
from functools import lru_cache
from pathlib import Path

from app.schemas.retrieval import EvidenceHit, SearchRequest

ROOT = Path(__file__).resolve().parents[2] / "data" / "private"
FOLDERS = {
    "fault_standards": "01_fault_standards",
    "dispute_cases": "02_dispute_cases",
    "policy_terms": "03_policy_terms",
}


def _read(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]
    except OSError as exc:
        raise RuntimeError(f"Corpus source file unavailable: {path}") from exc


@lru_cache(maxsize=3)
def _load(corpus: str) -> tuple[dict, dict, dict, dict]:
    base = ROOT / FOLDERS[corpus]
    chunks = _read(base / "processed" / "chunks.jsonl")
    blocks = _read(base / "parsed" / "blocks.jsonl")
    record_file = {
        "fault_standards": "charts.jsonl",
        "dispute_cases": "cases.jsonl",
        "policy_terms": "clauses.jsonl",
    }[corpus]
    records = _read(base / "processed" / record_file)
    if corpus == "policy_terms":
        records += _read(base / "processed" / "appendices.jsonl")
    chunk_index = {row["chunk_id"]: row for row in chunks}
    record_index = {row["parent_id"]: row for row in records}
    if len(chunk_index) != len(chunks) or len(record_index) != len(records):
        raise ValueError(f"Duplicate chunk or parent ID in {corpus} corpus")
    indexed_parents = {row["parent_id"] for row in chunks}
    if corpus == "fault_standards":
        modifiers = _read(base / "processed" / "modifiers.jsonl")
        modifier_ids = {row["modifier_id"] for row in modifiers}
        charts = {row["chart_id"] for row in records}
        if any(not set(row["modifier_ids"]) <= modifier_ids for row in records):
            raise ValueError("Chart references missing modifier IDs")
        if any(row["chart_id"] not in charts for row in modifiers):
            raise ValueError("Modifier references missing chart ID")
        if any(
            row["chart_id"] not in charts for row in _read(base / "processed" / "attributes.jsonl")
        ):
            raise ValueError("Attribute references missing chart ID")
        if any(parent not in indexed_parents for parent in record_index):
            raise ValueError("Chart lacks searchable chunks")
    if corpus == "dispute_cases":
        mapping_rows = _read(base / "processed" / "chart_mappings.jsonl")
        mapping_keys = {
            (row["legacy_chart_id"], row["chart_id"], row["source_pdf_page"])
            for row in mapping_rows
        }
        for record in records:
            if record["parent_id"] not in indexed_parents:
                raise ValueError("Case lacks searchable chunks")
            if any(
                (link["legacy_chart_id"], link["chart_id"], link["source_pdf_page"])
                not in mapping_keys
                for link in record["mapping_evidence"]
            ):
                raise ValueError("Case chart link is absent from source crosswalk")
    if corpus == "policy_terms":
        for record in records:
            if "clause_id" not in record:
                continue
            if record["parent_id"] not in indexed_parents:
                raise ValueError("Clause lacks searchable chunks")
            if any(note not in record_index for note in record["side_note_parent_ids"]):
                raise ValueError("Clause references missing source note")
    page_index = {}
    for block in blocks:
        document = block["document"]
        entry = page_index.setdefault(
            document["document_id"], {"pages": set(), "hash": document["content_hash"]}
        )
        if entry["hash"] != document["content_hash"]:
            raise ValueError("Parsed blocks have conflicting document hashes")
        entry["pages"].add(block["page"])
    chart_ids = set()
    if corpus == "dispute_cases":
        chart_ids = {
            row["chart_id"] for row in _read(ROOT / "01_fault_standards/processed/charts.jsonl")
        }
    return chunk_index, record_index, page_index, {"chart_ids": chart_ids}


def _record_for(chunk: dict, records: dict, corpus: str) -> dict | None:
    parent = chunk["parent_id"]
    if corpus == "fault_standards" and chunk["metadata"].get("section") == "common_modifier_guide":
        if not parent.startswith(f"{chunk['document']['document_id']}:modifier-guide:"):
            raise ValueError("Modifier guide parent ID does not match its source document")
        return None
    record = records.get(parent)
    if record is None:
        raise ValueError(f"No structured source record for parent_id {parent}")
    if record.get("document", {}).get("document_id") != chunk["document"]["document_id"]:
        raise ValueError(f"Source document differs for parent_id {parent}")
    if chunk["page_start"] < record["page_start"] or chunk["page_end"] > record["page_end"]:
        raise ValueError(f"Chunk page outside parent record: {parent}")
    metadata = chunk["metadata"]
    if corpus == "fault_standards":
        if not parent.endswith(":" + record["chart_id"]):
            raise ValueError(f"Chart ID differs from parent_id {parent}")
    elif corpus == "dispute_cases":
        if metadata.get("case_id") != record["case_id"]:
            raise ValueError(f"Case ID differs from parent_id {parent}")
        if record.get("chart_id"):
            if record["chart_id"] not in _load(corpus)[3]["chart_ids"]:
                raise ValueError(f"Linked chart does not exist: {parent}")
            if not any(
                link.get("chart_id") == record["chart_id"]
                for link in record.get("mapping_evidence", [])
            ):
                raise ValueError(f"Linked chart lacks crosswalk evidence: {parent}")
            if metadata.get("chart_id") != record["chart_id"]:
                raise ValueError(f"Chunk chart ID differs from case record: {parent}")
    elif corpus == "policy_terms" and "clause_id" in record:
        if metadata.get("clause_id") != record["clause_id"]:
            raise ValueError(f"Clause ID differs from parent_id {parent}")
        if metadata.get("coverage") != record["coverage"]:
            raise ValueError(f"Coverage differs from clause record: {parent}")
    return record


def _matches(filters: dict, chunk: dict, record: dict | None, corpus: str) -> bool:
    document = chunk["document"]
    for name, expected in filters.items():
        if name in {"document_id", "edition"}:
            actual = document.get(name)
        elif name == "chart_id":
            actual = record.get("chart_id") if record else None
        elif name in {"case_id", "clause_id", "article_number", "rider_number", "coverage"}:
            actual = record.get(name) if record else None
        else:
            raise ValueError(f"Unsupported {corpus} filter: {name}")
        if actual != expected:
            return False
    return True


def validate_hits(request: SearchRequest, hits: list[EvidenceHit]) -> list[EvidenceHit]:
    """Reject hits detached from an indexed chunk, structured parent, or PDF page."""
    if not isinstance(hits, list):
        raise TypeError("Search results must be a list of EvidenceHit")
    if not hits:
        return hits
    corpus = request["corpus"]
    chunks, records, pages, _ = _load(corpus)
    if len(hits) > request["top_k"]:
        raise ValueError("Search returned more hits than top_k")
    for hit in hits:
        if not isinstance(hit, dict) or not isinstance(hit.get("chunk"), dict):
            raise ValueError("Search hit has no source chunk")
        chunk = hit["chunk"]
        indexed = chunks.get(chunk.get("chunk_id"))
        if indexed is None:
            raise ValueError("Search hit has an unknown chunk_id")
        for field in ("parent_id", "page_start", "page_end", "text", "document"):
            if chunk.get(field) != indexed[field]:
                raise ValueError(f"Search hit {field} differs from indexed source")
        if chunk.get("metadata") != indexed["metadata"]:
            raise ValueError("Search hit metadata differs from indexed source")
        document = indexed["document"]
        if document.get("corpus") != corpus:
            raise ValueError("Search hit belongs to another corpus")
        source = pages.get(document["document_id"])
        if source is None or source["hash"] != document["content_hash"]:
            raise ValueError("Search hit document differs from parsed PDF source")
        if not all(
            page in source["pages"] for page in range(chunk["page_start"], chunk["page_end"] + 1)
        ):
            raise ValueError("Search hit page is absent from parsed PDF blocks")
        record = _record_for(indexed, records, corpus)
        if not _matches(request["filters"], indexed, record, corpus):
            raise ValueError("Search hit does not satisfy the requested filters")
        if (
            not isinstance(hit.get("score"), (int, float))
            or not math.isfinite(hit["score"])
            or not isinstance(hit.get("retrieval_version"), str)
            or not hit["retrieval_version"]
        ):
            raise ValueError("Search hit score or retrieval_version is invalid")
    return hits
