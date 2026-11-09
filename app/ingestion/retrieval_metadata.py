"""Source-grounded retrieval labels; these do not determine contract applicability."""

import re
from urllib.parse import unquote, urlparse

POLICY_DOCUMENT = "samsung-personal-auto-2026-11-11"
POLICY_EDITION = "2026.11.11"


def normalize_document(document):
    source = document.get("source_uri", "")
    if source.startswith("file:"):
        filename = unquote(urlparse(source).path).replace("\\", "/").rsplit("/", 1)[-1]
        document["source_uri"] = filename
        document["source_name"] = filename
        document["source_uri_type"] = "document_name_not_download_url"
    if document.get("document_id") == POLICY_DOCUMENT:
        document["edition"] = POLICY_EDITION
    if not document.get("effective_from"):
        document["effective_period_status"] = "unknown"
        document["effective_from_source"] = None
    return document


def policy_coverage(record):
    """Only label the explicit own-damage section or an explicitly titled rider."""
    if record.get("clause_type") == "general_article":
        if record.get("article_number") in {"21", "22", "23", "24"}:
            return {
                "coverage": "own_damage",
                "coverage_status": "source_section_classified",
                "coverage_source": {
                    "pdf_page": 67,
                    "quote": "제3절 자기차량손해",
                    "basis": "general_articles_21_to_24",
                },
            }
    if record.get("clause_type") == "rider" and "자기차량손해" in record.get("title", ""):
        return {
            "coverage": "own_damage",
            "coverage_status": "explicit_rider_title",
            "coverage_source": {
                "pdf_page": record["page_start"],
                "quote": record["title"],
                "basis": "explicit_title_not_body_keyword",
            },
        }
    # These references identify retrieval relevance, not exclusive coverage or enrollment.
    text = record.get("text", "")
    mention = "자기차량손해"
    if mention in text and (
        record.get("clause_type") == "rider" or record.get("article_number") in {"2", "33", "34"}
    ):
        start = text.index(mention)
        return {
            "coverage": "own_damage",
            "coverage_status": "explicit_reference_retrieval_relevance",
            "coverage_scope": "shared_or_related_not_exclusive",
            "coverage_source": {
                "pdf_page_start": record["page_start"],
                "pdf_page_end": record["page_end"],
                "quote": text[max(0, start - 60) : start + 160],
                "basis": "explicit_clause_reference_not_contract_applicability",
            },
        }
    return {
        "coverage": None,
        "coverage_status": "unclassified_include_in_fallback",
        "coverage_source": None,
    }


def candidate_chart_metadata(record):
    candidates = record.get("chart_id_candidates", [])
    evidence = record.get("mapping_evidence", [])
    if (
        len(candidates) == 1
        and any(
            row.get("chart_id") == candidates[0]
            and (
                row.get("legacy_chart_id") in record.get("legacy_chart_ids", [])
                or any(
                    re.fullmatch(
                        re.escape(f"{legacy}-{adopted}CO") + r"\d*",
                        row.get("legacy_chart_id", ""),
                    )
                    for legacy in record.get("legacy_chart_ids", [])
                    for adopted in record.get("adopted_legacy_chart_ids", [])
                )
            )
            and row.get("source_pdf_page")
            for row in evidence
        )
        and record.get("mapping_method")
        in {
            "appendix_number",
            "appendix_number_and_variant",
            "appendix_and_A_movement_from_facts",
        }
    ):
        return {
            "chart_id": candidates[0],
            "chart_id_candidates": candidates,
            "mapping_status": "source_crosswalk_linked",
            "chart_filter_usage": "exact_reference_lookup_not_accident_applicability",
            "chart_link_basis": record["mapping_method"],
            "chart_link_human_reviewed": False,
        }
    return {
        "chart_id": None,
        "chart_id_candidates": record.get("chart_id_candidates", []),
        "mapping_status": "candidate_only",
        "chart_filter_usage": "candidate_expansion_or_ranking_not_hard_exclusion",
    }
