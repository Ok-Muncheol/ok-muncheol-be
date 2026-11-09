"""PDF 파싱 경계 검증. 실제 원문을 테스트에 포함하지 않는다."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.ingestion.parse import parse_pdf


class ParseTests(unittest.TestCase):
    def test_page_range_empty_page_and_table_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.pdf"
            path.write_bytes(b"fixture")
            source = dict(
                document_id="test",
                corpus="fault_standards",
                source_uri="test",
                edition="test",
                effective_from="",
                effective_to=None,
                content_hash="",
                parser_version="",
            )
            page = MagicMock()
            page.within_bbox.return_value.dedupe_chars.return_value = page
            page.extract_text.return_value = None
            page.extract_tables.return_value = [[["A", "B"], ["0", "100"]]]
            pdf = MagicMock()
            pdf.pages = [MagicMock(), page]
            with patch("pdfplumber.open") as opened:
                opened.return_value.__enter__.return_value = pdf
                blocks = parse_pdf(path, source, page_start=2, page_end=2)
                self.assertEqual([b["page"] for b in blocks], [2, 2])
                self.assertEqual(blocks[0]["text"], "")
                self.assertEqual(blocks[1]["kind"], "table")
                self.assertEqual(
                    blocks[0]["document"]["content_hash"], hashlib.sha256(b"fixture").hexdigest()
                )
                self.assertEqual(source["content_hash"], "")
                with self.assertRaises(ValueError):
                    parse_pdf(path, source, page_start=0)
            source["content_hash"] = "incorrect"
            with self.assertRaises(ValueError):
                parse_pdf(path, source)


if __name__ == "__main__":
    unittest.main()
