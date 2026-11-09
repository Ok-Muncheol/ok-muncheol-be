import unittest

from app.ingestion.policy_layout import extract_columns


class Page:
    bbox = (0, 0, 312, 575)

    def __init__(self, chars):
        self.chars = chars

    def within_bbox(self, bbox):
        return self

    def dedupe_chars(self, tolerance):
        return self

    def filter(self, fn):
        return Page([c for c in self.chars if fn(c)])

    def extract_text(self):
        return "".join(c["text"] for c in self.chars)


def char(text, x, size=7.8, top=50):
    return dict(
        object_type="char", text=text, x0=x, x1=x + 5, top=top, bottom=top + size, size=size
    )


class PolicyLayoutTests(unittest.TestCase):
    def test_columns_preserve_small_note_and_main_reference_marker(self):
        result = extract_columns(
            Page(
                [
                    char("본문", 100),
                    char("(*1)", 110, 3.5),
                    char("각주", 30, 5.4),
                    char("쪽수", 30, 6.5, 560),
                ]
            ),
            64,
        )
        self.assertEqual(result["body"], "본문(*1)")
        self.assertEqual(result["side_note"], "각주")
        result = extract_columns(Page([char("본문", 25), char("각주", 240, 5.4)]), 67)
        self.assertEqual(result["body"], "본문")
        self.assertEqual(result["side_note"], "각주")

    def test_full_width_table_and_left_edge_heading_survive(self):
        result = extract_columns(Page([char("제27조", 43, 9.7), char("제출표", 100)]), 72)
        self.assertEqual(result["body"], "제27조제출표")
        self.assertFalse(result["side_note"])
        self.assertIn("[37]", extract_columns(Page([char("[37]", 19.8, 9.7)]), 157)["body"])


if __name__ == "__main__":
    unittest.main()
