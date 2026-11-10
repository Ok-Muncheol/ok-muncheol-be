import json
import tempfile
import unittest
from pathlib import Path

from evals.retrieval.evaluate import score_predictions
from evals.retrieval.report import (
    CORPUS_HEADER,
    STEP_HEADER,
    load_predictions,
    misses,
    render,
    summarize,
)


def example(identifier, expected, category="direct", pool=()):
    return {
        "example_id": identifier,
        "request": {
            "corpus": "fault_standards",
            "text": "질문",
            "accident_date": "",
            "filters": {},
            "top_k": 10,
        },
        "expected_parent_ids": expected,
        "expected_edition": None,
        "split_group_id": "family",
        "split": "test",
        "label_status": "machine_reviewed",
        "synthetic": True,
        "notes": "test",
        "question_category": category,
        "review": {
            "grounded": True,
            "candidate_assessments": [
                {"parent_id": p, "relevant": p in expected} for p in (*expected, *pool)
            ],
        },
    }


def cells(row):
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.examples = [
            example("hit", ["A"]),
            example("judged", ["B"], "condition_disambiguation", pool=("X",)),
            example("outside", ["C"], "exception"),
            example("empty", ["D", "E"]),
        ]
        # 청크 순위라 같은 부모가 겹친다. 부모가 처음 나온 순서로 센다.
        self.now = {
            "hit": ["A", "A", "X"],
            "judged": ["X", "X", "B"],
            "outside": ["Y", "Z", "W", "V", "U", "C"],
            "empty": [],
        }
        self.before = {"hit": ["Q", "A"], "judged": ["B"], "outside": ["C"], "empty": ["D"]}

    def test_shared_metrics_match_the_scorer(self):
        mine = summarize(self.examples, self.now, allow_machine_reviewed=True)["fault_standards"]
        ref = score_predictions(self.examples, self.now, allow_machine_reviewed=True)
        ref = ref["fault_standards"]
        self.assertAlmostEqual(mine["top1"], ref["top1_accuracy"])
        self.assertAlmostEqual(mine["recall"][3], ref["recall_at_3"])
        self.assertAlmostEqual(mine["mrr_at_10"], ref["mrr_at_10"])

    def test_parent_counts_misses_categories_and_paired_change(self):
        s = summarize(self.examples, self.now, self.before, allow_machine_reviewed=True)
        s = s["fault_standards"]
        self.assertEqual(s["recall"], {3: 0.5, 5: 0.5, 10: 0.75})
        self.assertEqual((s["parents_mean"], s["parents_min"]), (2.5, 0))
        self.assertEqual(s["top1_misses"], {"judged": 1, "outside": 1, "empty": 1})
        self.assertEqual(s["hard_top1"], {"condition_disambiguation": (0, 1), "exception": (0, 1)})
        self.assertEqual(s["delta"], {"top1": (1, 3), "at5": (0, 2)})

    def test_rows_fit_the_report_tables(self):
        summary = summarize(self.examples, self.now, self.before, allow_machine_reviewed=True)
        lines = render(
            summary,
            {"latency_ms": {"p50": 12.3, "p95": 40.0}},
            step="1",
            change="RRF",
            commit="abc1234",
            today="2026-11-10",
        )
        step_row, corpus_row = (line for line in lines if line.startswith("|"))
        self.assertEqual(len(cells(step_row)), len(STEP_HEADER))
        self.assertEqual(
            cells(corpus_row),
            [
                "1",
                "0.25",
                "0.42",
                "0.50 · 0.50 · 0.75",
                "2.5 / 0",
                "+1 -3 · +0 -2",
                "1 / 1 / 1",
                "0/1 · 0/1",
            ],
        )
        self.assertEqual(len(cells(corpus_row)), len(CORPUS_HEADER))
        self.assertEqual(len(misses(self.examples, self.now)), 3)

    def test_unreviewed_labels_are_rejected_like_the_scorer(self):
        with self.assertRaises(ValueError):
            summarize(self.examples, self.now)

    def test_result_from_another_dataset_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.json"
            run = {"predictions": {}, "dataset_hashes": {"01_fault_standards": "old"}}
            path.write_text(json.dumps(run), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_predictions(path, {"01_fault_standards": "new"})
            path.write_text(json.dumps({"metrics": {}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_predictions(path, {})


if __name__ == "__main__":
    unittest.main()
