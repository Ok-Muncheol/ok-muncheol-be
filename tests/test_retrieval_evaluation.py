import unittest
from unittest.mock import AsyncMock

from evals.retrieval.evaluate import evaluate, score_predictions


def example(identifier, expected, **changes):
    row = {
        "example_id": identifier,
        "request": {
            "corpus": "fault_standards",
            "text": "질문",
            "accident_date": "2026-11-12",
            "filters": {},
            "top_k": 3,
        },
        "expected_parent_ids": expected,
        "expected_edition": "test",
        "split_group_id": identifier,
        "split": "test",
        "label_status": "reviewed",
        "synthetic": True,
        "notes": "test",
    }
    return {**row, **changes}


class EvaluationTests(unittest.TestCase):
    def test_machine_labels_require_explicit_opt_in(self):
        row = example("model", ["A"], label_status="machine_reviewed", review={"grounded": True})
        with self.assertRaises(ValueError):
            score_predictions([row], {"model": ["A"]})
        metrics = score_predictions([row], {"model": ["A"]}, allow_machine_reviewed=True)
        self.assertEqual(metrics["fault_standards"]["mrr_at_10"], 1)
        row["review"]["grounded"] = False
        with self.assertRaises(ValueError):
            score_predictions([row], {}, allow_machine_reviewed=True)

    def test_parent_dedup_multiple_gold_and_missing_result(self):
        rows = [example("one", ["A", "B"]), example("two", ["C"]), example("none", [])]
        score = score_predictions(rows, {"one": ["A", "A", "X", "B"], "none": []})[
            "fault_standards"
        ]
        self.assertEqual(score["top1_accuracy"], 0.5)
        self.assertEqual(score["recall_at_3"], 0.5)
        self.assertEqual(score["no_answer_accuracy"], 1)

    def test_drafts_and_split_leakage_are_rejected(self):
        with self.assertRaises(ValueError):
            score_predictions([example("draft", ["A"], label_status="unlabelled")], {})
        with self.assertRaises(ValueError):
            score_predictions(
                [
                    example("one", ["A"], split_group_id="family", split="dev"),
                    example("two", ["B"], split_group_id="family", split="test"),
                ],
                {},
            )


class SearchAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_labels_block_external_search(self):
        search = AsyncMock()
        with self.assertRaises(ValueError):
            await evaluate([example("draft", ["A"], label_status="unlabelled")], search_fn=search)
        search.assert_not_called()

    async def test_search_results_are_scored_as_parent_ids(self):
        search = AsyncMock(return_value=[{"chunk": {"parent_id": "A"}}])
        result = await evaluate([example("checked", ["A"])], search_fn=search)
        self.assertEqual(result["predictions"], {"checked": ["A"]})
        self.assertEqual(result["label_tier"], "reviewed")


if __name__ == "__main__":
    unittest.main()
