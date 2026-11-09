"""Update existing corpus metadata in place without changing embedded text or vectors."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingestion.retrieval_metadata import (  # noqa: E402
    POLICY_DOCUMENT,
    POLICY_EDITION,
    candidate_chart_metadata,
    normalize_document,
    policy_coverage,
)


def read(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def main():
    base = ROOT / "data/private"
    policies = read(base / "03_policy_terms/processed/clauses.jsonl")
    coverage = {row["parent_id"]: policy_coverage(row) for row in policies}
    cases = read(base / "02_dispute_cases/processed/cases.jsonl")
    mappings = {row["parent_id"]: candidate_chart_metadata(row) for row in cases}

    def update(value):
        if isinstance(value, list):
            for child in value:
                update(child)
        elif isinstance(value, dict):
            for child in list(value.values()):
                update(child)
            if {"document_id", "edition", "effective_from"} <= value.keys():
                normalize_document(value)
            parent = value.get("parent_id")
            if parent in coverage and ("text" in value or "clause_id" in value):
                target = value.get("metadata", value)
                target.update(coverage[parent])
            if parent in mappings and ("text" in value or "case_id" in value):
                target = value.get("metadata", value)
                target.update(mappings[parent])
            if value.get("request", {}).get("corpus") == "policy_terms":
                value["expected_edition"] = POLICY_EDITION
                filters = value["request"].get("filters", {})
                if "edition" in filters:
                    filters["edition"] = POLICY_EDITION
            if "example_id" in value and not value.get("request", {}).get("accident_date"):
                value["accident_date_status"] = "not_provided_in_question"
                value["accident_date_source"] = None
                value["date_evaluation_scope"] = "not_evaluated_fixed_document_retrieval"
            if value.get("document_id") == POLICY_DOCUMENT and "edition" in value:
                value["edition"] = POLICY_EDITION

    count = 0
    for folder in sorted(base.glob("0*_*")):
        paths = [folder / "evaluation.jsonl", folder / "source.json"]
        paths += list((folder / "parsed").glob("*.jsonl"))
        paths += [p for p in (folder / "processed").glob("*.jsonl") if "cache" not in p.name]
        for path in paths:
            if not path.exists():
                continue
            if path.suffix == ".jsonl":
                rows = read(path)
                before = json.dumps(rows, ensure_ascii=False)
                update(rows)
                if json.dumps(rows, ensure_ascii=False) == before:
                    continue
                output = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            else:
                row = json.loads(path.read_text("utf-8-sig"))
                update(row)
                output = json.dumps(row, ensure_ascii=False, indent=2) + "\n"
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(output, encoding="utf-8")
            temporary.replace(path)
            count += 1
    print(f"Updated {count} files; text, chunk IDs and embedding vectors preserved")


if __name__ == "__main__":
    main()
