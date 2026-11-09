import unittest

from app.ingestion.preprocess import clean_text, preprocess_chunks


class PreprocessTests(unittest.TestCase):
    def test_header_removed_but_exception_numbers_retained(self):
        source = "자동차사고 과실비율 인정기준 │ 제3편 146\nA 중대한 과실 +20\n다만, 0.03% 미만은 제외한다.\n① 이륜차 -5~10\n제49조 제1항"  # noqa: E501 -- source text or prompt kept intact
        result = clean_text(source, "fault_standards", 147)
        self.assertNotIn("│", result)
        for value in ["+20", "0.03%", "다만", "제외", "-5~10", "제49조 제1항"]:
            self.assertIn(value, result)

    def test_idempotent_and_parent_specific_dedup(self):
        chunk = {
            "chunk_id": "a",
            "parent_id": "p",
            "document": {"corpus": "policy_terms"},
            "page_start": 54,
            "text": "제1조(정의)\n* 비고\n가. 본문은 유지한다.\n52",
            "metadata": {},
        }
        cleaned = preprocess_chunks([chunk])
        self.assertEqual(preprocess_chunks(cleaned), cleaned)
        self.assertEqual(cleaned[0]["raw_text"], chunk["text"])
        self.assertNotIn("* 비고", cleaned[0]["text"])
        other = {**chunk, "chunk_id": "b", "parent_id": "other"}
        self.assertEqual(len(preprocess_chunks([chunk, other])), 2)


if __name__ == "__main__":
    unittest.main()
