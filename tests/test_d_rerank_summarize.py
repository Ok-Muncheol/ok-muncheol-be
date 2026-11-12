"""D: 재정렬 비교 결과 원본 → 원문 없는 실행 요약(evals/rerank_compare/summarize.py)."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from evals.rerank_compare.harness import SCHEMA
from evals.rerank_compare.summarize import (
    SUMMARY_SCHEMA,
    check_no_raw,
    corpus_edition,
    summarize,
    write_summaries,
)

RECORDS_1413 = "b6d4c4cbdbf47e133b5456ed2318f14049e64466ff90a05a6236166dd0c8a8a4"


def fake_result():
    return {
        "schema": SCHEMA,
        "method": "fault-rrf",
        "description": "고유 parent 10개",
        "created_at": "2026-11-12T14:17:18+09:00",
        "git_commit": "fae7a17",
        "dataset": {"corpus": "fault_standards", "count": 1, "sha256": "d" * 64},
        "corpus": {"corpus_records.jsonl": RECORDS_1413, "corpus_chunks.jsonl": "c" * 64},
        "common": {"relevance_instruction": "Given a question …"},
        # 점수기 설명의 "question"은 요청 형식 설명이라 params 안에서는 괜찮다
        "params": {"scorer": {"question": '{"type": "noul"}'}, "usage": {"cost_usd": 0.5}},
        "metrics": {"fault100": {"n": 1, "top1": 1.0}},
        "questions": [
            {
                "example_id": "fault_standards-1",
                "subset": "fault100",
                "question": "적색점멸신호에서 직진하는 A차와 …",
                "accident_date": "",
                "expected": ["fault-standards-2023-06:차1-5"],
                "ranked": ["fault-standards-2023-06:차1-5", "fault-standards-2023-06:차1-4"],
                "scores": {"first_correct_rank": 1, "top1": 1.0},
                "stages": [{"name": "L1", "kept_parents": ["fault-standards-2023-06:차1-5"]}],
            }
        ],
    }


class SummarizeTest(unittest.TestCase):
    def test_keeps_numbers_and_drops_raw_text(self):
        result = fake_result()
        entry = {"slot": "⑥", "owner": "옥석현", "file": "x.json", "out": "fault/x.json"}
        summary = summarize(result, source="x.json", sha256="s" * 64, entry=entry)
        self.assertEqual(summary["schema"], SUMMARY_SCHEMA)
        self.assertEqual(summary["run"], {"slot": "⑥", "owner": "옥석현"})
        self.assertEqual(summary["corpus_edition"], "1413")
        self.assertEqual(summary["params"]["usage"]["cost_usd"], 0.5)
        self.assertEqual(
            summary["questions"],
            [{"example_id": "fault_standards-1", "subset": "fault100", "first_correct_rank": 1}],
        )
        blob = json.dumps(summary, ensure_ascii=False)
        for raw in ("적색점멸신호", "차1-5", "차1-4", "kept_parents", '"expected"'):
            self.assertNotIn(raw, blob)

    def test_stops_on_raw_fields_or_question_text(self):
        with self.assertRaises(ValueError):
            check_no_raw({"questions": [{"example_id": "a", "ranked": ["p"]}]})
        with self.assertRaises(ValueError):
            check_no_raw({"note": "질문 그대로"}, ["질문 그대로"])
        check_no_raw({"params": {"scorer": {"question": "형식"}}})  # params 안은 괜찮다
        with self.assertRaises(ValueError):
            summarize({"schema": "other"}, source="x", sha256="", entry={})

    def test_unknown_corpus_edition_is_none(self):
        self.assertIsNone(corpus_edition({"corpus_records.jsonl": "0" * 64}))
        self.assertIsNone(corpus_edition({}))

    def test_write_from_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            results, runs = Path(tmp, "results"), Path(tmp, "runs")
            results.mkdir()
            runs.mkdir()
            raw = json.dumps(fake_result(), ensure_ascii=False).encode("utf-8")
            (results / "fault-rrf.json").write_bytes(raw)
            manifest = runs / "manifest.json"
            entry = {"slot": "기준선", "file": "fault-rrf.json", "out": "fault/rrf.summary.json"}
            manifest.write_text(json.dumps({"runs": [entry]}), encoding="utf-8")
            (out,) = write_summaries(manifest, results)
            summary = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(out.name, "rrf.summary.json")
        self.assertEqual(summary["source"]["sha256"], hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
