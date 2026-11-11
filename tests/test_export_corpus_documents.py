"""코퍼스 문서 행(corpus_documents) 내보내기 — 합성 manifest 값만 쓴다."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_corpus_index_rows.py"
_SPEC = importlib.util.spec_from_file_location("export_corpus_index_rows", _PATH)
assert _SPEC and _SPEC.loader
export = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(export)

HASH = "a" * 64


def meta(**over):
    base = {
        "document_id": "synthetic-doc", "source_uri": "corpus-sources/x/synthetic-doc.pdf",
        "source_name": "synthetic.pdf", "edition": "판-1", "effective_from": "",
        "effective_to": None, "effective_period_status": "unknown", "content_hash": HASH,
        "parser_version": "synthetic", "usage_review": {"status": "permission_required"},
        "reviewed_by": None,
        "storage_object": {"bucket": "corpus-sources", "path": "x/synthetic-doc.pdf",
                           "relation": "original", "sha256": HASH, "bytes": 1},
    }
    base.update(over)
    return base


def test_published_month_is_exported_and_null_when_missing():
    assert export.document_row("fault_standards", meta(published_month="2023-06"))[
        "published_month"] == "2023-06"
    assert export.document_row("policy_terms", meta())["published_month"] is None


@pytest.mark.parametrize("bad", ["2023-6", "2023-13", "2023-06-30", "June 2023"])
def test_published_month_must_be_year_month(bad):
    with pytest.raises(ValueError, match="published_month"):
        export.document_row("fault_standards", meta(published_month=bad))
