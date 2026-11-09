from app.ingestion.retrieval_metadata import (
    candidate_chart_metadata,
    normalize_document,
    policy_coverage,
)


def test_cover_date_is_not_an_effective_date():
    doc = normalize_document(
        {"document_id": "samsung-personal-auto-2026-11-11", "effective_from": ""}
    )
    assert doc["edition"] == "2026.11.11"
    assert doc["effective_from"] == ""
    assert doc["effective_period_status"] == "unknown"


def test_local_citation_becomes_decoded_document_name():
    doc = normalize_document({"source_uri": "file:///C:/Users/Admin/Desktop/a%20b.pdf"})
    assert doc["source_uri"] == "a b.pdf"
    assert doc["source_name"] == "a b.pdf"
    assert doc["source_uri_type"] == "document_name_not_download_url"


def test_own_damage_section_includes_exclusions():
    for number in ["21", "22", "23", "24"]:
        result = policy_coverage({"clause_type": "general_article", "article_number": number})
        assert result["coverage"] == "own_damage"
        assert result["coverage_source"]["pdf_page"] == 67


def test_body_mention_does_not_classify_unrelated_article():
    assert (
        policy_coverage(
            {"clause_type": "general_article", "article_number": "1", "text": "자기차량손해"}
        )["coverage"]
        is None
    )


def test_candidate_is_never_promoted_to_confirmed_chart():
    result = candidate_chart_metadata({"chart_id_candidates": ["차1-1"]})
    assert result["chart_id"] is None
    assert result["chart_id_candidates"] == ["차1-1"]
    assert result["mapping_status"] == "candidate_only"


def test_chart_link_requires_source_crosswalk():
    record = {
        "chart_id_candidates": ["차1-1"],
        "legacy_chart_ids": ["201"],
        "mapping_method": "appendix_number",
        "mapping_evidence": [
            {"chart_id": "차1-1", "legacy_chart_id": "201", "source_pdf_page": 589}
        ],
    }
    assert candidate_chart_metadata(record)["chart_id"] == "차1-1"
    record["chart_id_candidates"].append("차1-2")
    assert candidate_chart_metadata(record)["chart_id"] is None


def test_shared_rider_reference_is_retrieval_label_only():
    result = policy_coverage(
        {
            "clause_type": "rider",
            "title": "운전자 한정",
            "text": "자기차량손해에서 보상하지 않는 경우",
            "page_start": 122,
            "page_end": 123,
        }
    )
    assert result["coverage"] == "own_damage"
    assert result["coverage_scope"] == "shared_or_related_not_exclusive"
