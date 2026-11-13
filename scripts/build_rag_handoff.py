"""현재 파일을 복사하지 않고 RAG 전달 목록·해시·연결 계약을 검증해 저장한다."""

import hashlib
import json
import math
import sys
from itertools import zip_longest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_evaluation_release import TARGET_COUNTS  # noqa: E402 -- standalone CLI adds repository root before app imports
from embed_corpora import DIMENSIONS, LOCATIONS, MODEL, VERSION, key, read  # noqa: E402 -- standalone CLI adds repository root before app imports

from app.ingestion.embed import validate_vector  # noqa: E402 -- standalone CLI adds repository root before app imports


def file_info(path, role):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "role": role,
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def main():
    corpus_settings = [
        ("fault_standards", ["charts", "modifiers", "attributes"], "attribute_first"),
        ("dispute_cases", ["cases", "chart_mappings"], "fact_card_similarity"),
        ("policy_terms", ["clauses", "appendices", "schedule_values"], "clause_bm25"),
    ]
    manifest = {
        "status": "ready_for_index_import_not_business_approved",
        "paths_relative_to": "project_root",
        "embedding": {
            "model": MODEL,
            "dimensions": DIMENSIONS,
            "version": VERSION,
            "similarity": "cosine",
            "query_embedding_must_match": True,
        },
        "corpora": [],
        "index_import_contract": {
            "chunk_primary_key": "chunk_id",
            "parent_join_key": "parent_id",
            "embedding_join_key": "embeddings.jsonl -> chunk.chunk_id",
            "vector_field": "embedding",
            "text_field": "chunk.text",
            "page_numbering": "PDF actual pages, 1-based",
            "preserve_fields": [
                "document",
                "page_start",
                "page_end",
                "metadata",
                "embedding_model",
                "embedding_dimensions",
                "embedding_version",
                "embedding_content_hash",
            ],
            "unknown_filter_values": "null/empty means unknown, never false or non-applicable",
            "auxiliary_reference_policy": "policy side_note/cited_law_appendix are auxiliary, not primary clauses",  # noqa: E501 -- source text or prompt kept intact
            "policy_coverage_context": "document section context, not proof that a contract covers the loss",  # noqa: E501 -- source text or prompt kept intact
        },
        "evaluation_contract": {
            "request_type": "SearchRequest",
            "response_type": "list[EvidenceHit]",
            "request_keys": ["corpus", "text", "accident_date", "filters", "top_k"],
            "response_parent_path": "hit.chunk.parent_id",
            "prediction_file_shape": "{example_id: [parent_id, ...]}",
            "scoring_level": "parent ID; duplicate chunks under same parent are collapsed",
            "label_tier": "machine_reviewed",
            "human_reviewed": False,
            "query_origin": "GPT-5 source-grounded synthetic questions",
            "gold_scope": "reviewed candidate sources only; exhaustive relevance not guaranteed",
            "split_policy": "evaluation-only test set; no independent development split",
            "date_policy": "empty accident_date is intentional for fixed-source retrieval; never invent an effective date. Exception: payment-standard items split by accident date (metadata accident_date_from/to) carry the date stated in the question",  # noqa: E501 -- source text or prompt kept intact
            "required_filter": "document_id pins the provided source; unknown dates/editions need a source-only evaluation mode",  # noqa: E501 -- source text or prompt kept intact
            "allow_machine_reviewed_flag_required": True,
        },
        "not_included": [
            ".env/API keys",
            "raw PDF files",
            "generation/embedding caches",
            "temporary files",
        ],
        "integration_pending": [
            "D: database schema, loading, online query embedding and common search implementation",
            "D/C: source-only evaluation handling when accident_date is empty",
            "Source URI values currently contain local file paths; map them to accessible storage for citations",  # noqa: E501 -- source text or prompt kept intact
            "Document effective periods and policy contract applicability are unconfirmed",
            "Machine-reviewed labels are not expert-certified; no-answer cases are not covered",
        ],
    }
    total_chunks = total_questions = 0
    for (folder, version), (corpus, stems, strategy) in zip(
        LOCATIONS, corpus_settings, strict=False
    ):
        base = ROOT / "data/private" / folder
        data = base / version
        records = {stem: read(data / f"{stem}.jsonl") for stem in stems}
        chunks = read(data / "chunks.jsonl")
        examples = read(base / "evaluation.jsonl")
        ids = {c["chunk_id"] for c in chunks}
        if len(ids) != len(chunks):
            raise ValueError(f"{corpus}: duplicate chunk IDs")
        parents = {c["parent_id"] for c in chunks}
        if len(examples) != TARGET_COUNTS[corpus]:
            raise ValueError(f"{corpus}: evaluation count mismatch")
        source_parents = {r["parent_id"] for r in records[stems[0]]}
        if corpus == "policy_terms":
            source_parents |= {r["parent_id"] for r in records["appendices"]}
        if corpus == "fault_standards":
            source_parents |= {
                c["parent_id"]
                for c in chunks
                if c["metadata"].get("section") == "common_modifier_guide"
            }
        if not parents <= source_parents:
            raise ValueError(f"{corpus}: orphan parent IDs")
        if any(not set(e["expected_parent_ids"]) <= parents for e in examples):
            raise ValueError(f"{corpus}: evaluation parent not indexed")
        if corpus == "fault_standards" and any(
            r["parent_id"] not in source_parents for r in records["modifiers"]
        ):
            raise ValueError("orphan modifier")
        documents = {}
        with (data / "embeddings.jsonl").open(encoding="utf-8") as stream:
            for chunk, line in zip_longest(chunks, stream):
                if chunk is None or line is None:
                    raise ValueError(f"{corpus}: chunk/vector count mismatch")
                row = json.loads(line)
                if row["chunk"] != chunk or row["embedding_content_hash"] != key(chunk["text"]):
                    raise ValueError(f"{corpus}: stale embedding")
                if (
                    row["embedding_model"] != MODEL
                    or row["embedding_dimensions"] != DIMENSIONS
                    or row["embedding_version"] != VERSION
                ):
                    raise ValueError(f"{corpus}: embedding settings mismatch")
                validate_vector(row["embedding"], DIMENSIONS)
                if not math.isfinite(sum(v * v for v in row["embedding"])):
                    raise ValueError("invalid vector norm")
                if chunk["document"]["corpus"] != corpus:
                    raise ValueError("corpus contamination")
                document = chunk["document"]
                document_key = (document["document_id"], document["content_hash"])
                documents[document_key] = document
        files = [
            file_info(data / "chunks.jsonl", "searchable chunks"),
            file_info(data / "embeddings.jsonl", "precomputed document vectors"),
            file_info(base / "evaluation.jsonl", "evaluation questions and reference parent IDs"),
        ]
        files += [file_info(data / f"{stem}.jsonl", stem) for stem in stems]
        item = {
            "corpus": corpus,
            "intended_search_strategy": strategy,
            "chunks": len(chunks),
            "vectors": len(chunks),
            "evaluation_questions": len(examples),
            "record_counts": {s: len(rows) for s, rows in records.items()},
            "files": files,
            "documents": list(documents.values()),
            "no_answer_questions": sum(not e["expected_parent_ids"] for e in examples),
            "source_review_status": "unreviewed business rules; machine-reviewed evaluation references",  # noqa: E501 -- source text or prompt kept intact
            "unknown_effective_periods": any(not d["effective_from"] for d in documents.values()),
        }
        if corpus == "fault_standards":
            item["common_guide_parents"] = (
                "No chart row for these parents. Materialize parent groups from chunks whose metadata.section is common_modifier_guide."  # noqa: E501 -- source text or prompt kept intact
            )
            item["attribute_status"] = (
                "filter_candidates only; hard_filter_ready is false until confirmed"
            )
            item["numeric_rule_status"] = (
                "calculation_ready remains false; numeric values require business review"
            )
        if corpus == "policy_terms":
            item["payment_standard_status"] = (
                "별표1~5 items are appendices.record_type=payment_standard_item (chunk metadata clause_type=payment_standard); accident_date_from/to restrict some items, and hits outside the request accident_date are rejected by validate_hits"  # noqa: E501 -- source text or prompt kept intact
            )
            item["schedule_value_status"] = (
                "schedule_values.jsonl holds 별표4·5 numbers machine-matched to source text (calculation_ready=false until business review) and 52 injury/property values for 별표1·2·3 checked against the PDF (calculation_ready=true, source_parent_id)"  # noqa: E501 -- source text or prompt kept intact
            )
        if corpus == "dispute_cases":
            item["chart_mapping_status"] = (
                "candidate_only: chart_id_candidates are retrieval candidates, related_chart_ids are not confirmed mappings"  # noqa: E501 -- source text or prompt kept intact
            )
            item["card_similarity_input"] = (
                "chunks with metadata.retrieval_role=card_similarity contain facts; supporting_evidence contains issues/reasoning"  # noqa: E501 -- source text or prompt kept intact
            )
        manifest["corpora"].append(item)
        total_chunks += len(chunks)
        total_questions += len(examples)
        print(corpus, len(chunks), "vectors;", len(examples), "questions; links verified")
    helper_paths = [
        "scripts/run_retrieval_evaluation.py",
        "evals/retrieval/evaluate.py",
        "evals/retrieval/dataset.py",
        "app/schemas/retrieval.py",
        "scripts/embed_corpora.py",
        "app/ingestion/embed.py",
    ]
    manifest["evaluation_code"] = [file_info(ROOT / p, "evaluation runtime") for p in helper_paths]
    manifest["evaluation_commands"] = {
        "saved_predictions": "python scripts/run_retrieval_evaluation.py --predictions <predictions.json> --allow-machine-reviewed",  # noqa: E501 -- source text or prompt kept intact
        "connected_search": "python scripts/run_retrieval_evaluation.py --search-callable app.retrieval.common:search --allow-machine-reviewed",  # noqa: E501 -- source text or prompt kept intact
        "connected_search_status": "not executable yet: app.retrieval.common.search currently raises NotImplementedError",  # noqa: E501 -- source text or prompt kept intact
    }
    manifest["totals"] = {
        "chunks": total_chunks,
        "vectors": total_chunks,
        "evaluation_questions": total_questions,
    }
    target = ROOT / "data/private/handoff.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    print("Saved", target.relative_to(ROOT), manifest["totals"])


if __name__ == "__main__":
    main()
