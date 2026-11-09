"""280문항 릴리스의 원문·근거 ID·인용구·모델 검수·분할 일관성 검증."""

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_evaluation_release import TARGET_COUNTS, VERSION, norm, source_text  # noqa: E402 -- standalone CLI adds repository root before app imports
from embed_corpora import LOCATIONS, read  # noqa: E402 -- standalone CLI adds repository root before app imports


def main():
    all_ids = set()
    all_queries = set()
    splits = {}
    for (folder, version), corpus, stem in zip(
        LOCATIONS,
        ["fault_standards", "dispute_cases", "policy_terms"],
        ["charts", "cases", "clauses"],
        strict=False,
    ):
        base = ROOT / "data/private" / folder
        examples = read(base / "evaluation.jsonl")
        records = {r["parent_id"]: r for r in read(base / version / f"{stem}.jsonl")}
        parents = {c["parent_id"] for c in read(base / version / "chunks.jsonl")}
        target_count = TARGET_COUNTS[corpus]
        assert len(examples) == target_count, f"{folder}: exactly {target_count} examples required"
        assert len({e["primary_parent_id"] for e in examples}) == target_count
        for e in examples:
            identifier = e["example_id"]
            question = e["request"]["text"]
            assert identifier not in all_ids and question not in all_queries, "duplicate ID/query"
            all_ids.add(identifier)
            all_queries.add(question)
            assert e["dataset_version"] == VERSION
            assert e["label_status"] == "machine_reviewed" and e["synthetic"] is True
            assert e["review"]["human_reviewed"] is False and e["review"]["grounded"] is True
            assert e["review"]["generator_model"] == e["review"]["reviewer_model"] == "gpt-5"
            assert e["review"]["separate_review_request"] is True
            assert e["request"]["corpus"] == corpus
            primary = records[e["primary_parent_id"]]
            assert e["source_document_hash"] == primary["document"]["content_hash"]
            assert e["request"]["filters"]["document_id"] == primary["document"]["document_id"]
            assert norm(e["source_quote"]) in norm(source_text(corpus, primary))
            assert len(norm(e["source_quote"])) >= 8
            gold = set(e["expected_parent_ids"])
            assert gold and gold <= parents and e["primary_parent_id"] in gold
            judgments = e["review"]["candidate_assessments"]
            assert (
                len({j["parent_id"] for j in judgments})
                == len(judgments)
                == e["review"]["candidate_pool_size"]
            )
            assert {j["parent_id"] for j in judgments if j["relevant"]} == gold
            for j in judgments:
                assert j["parent_id"] in records
                if j["relevant"]:
                    assert len(norm(j["evidence_quote"])) >= 8
                    assert norm(j["evidence_quote"]) in norm(
                        source_text(corpus, records[j["parent_id"]])
                    )
                else:
                    # 포함 근거는 필수 원문 인용으로, 제외 근거는 설명으로 검증한다.
                    assert j["reason"].strip()
            for p, span in e["source_pages_by_parent"].items():
                assert span == {"start": records[p]["page_start"], "end": records[p]["page_end"]}
            group = e["split_group_id"]
            assert group not in splits or splits[group] == e["split"], "group split leakage"
            splits[group] = e["split"]
        summary_path = base / version / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["evaluation"] = {
            "file": "../evaluation.jsonl",
            "count": target_count,
            "dataset_version": VERSION,
            "sha256": hashlib.sha256((base / "evaluation.jsonl").read_bytes()).hexdigest(),
            "label_status": "machine_reviewed",
            "human_reviewed": False,
            "question_categories": dict(Counter(e["question_category"] for e in examples)),
            "multiple_gold_questions": sum(len(e["expected_parent_ids"]) > 1 for e in examples),
            "split": "test",
            "split_note": "단일 평가 전용 세트. 독립된 개발셋/전문가 평가셋 없음.",
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, target_count, "verified", summary["evaluation"]["question_categories"])
    print("TOTAL", len(all_ids), "source-linked machine-reviewed examples")


if __name__ == "__main__":
    main()
