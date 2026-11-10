"""D: 서식 견적서 PDF 자동 추출(read_by="pdf_extract") → 손해액·보험금 산정·보고서 8번.

실행: py -3.12 -m unittest discover -s tests -t .
- PDF를 실제로 읽는 테스트는 pdfplumber(선택 의존성)가 없으면 건너뛴다.
- tests/fixtures/estimate_sample_*.pdf는 data/forms/car_estimate_form.pdf 원본 위에 가상 값을 찍은
  것
  (tests/fixtures/make_estimate_pdfs.py로 생성, 값은 전부 synthetic).
정답은 손으로 계산했다. 예시 규칙: 자기부담금 20%, 최저 20만·최고 50만, 가입금액 2,500만.
"""

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

from app.api import reports as api
from app.calculation.damages import calculate_damages
from app.calculation.estimate_reader import (
    INSTALL_HINT,
    SUM_MISMATCH,
    TOTAL_NOT_FOUND,
    EstimateReaderUnavailable,
    read_estimate_pdf,
)
from app.calculation.insurance import calculate_insurance
from app.calculation.rules import RuleSet
from app.reports.assembler import assemble_review
from tests import fixtures_d as fx

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FORM = ROOT / "data" / "forms" / "car_estimate_form.pdf"
SAMPLE_10M = FIXTURES / "estimate_sample_10m.pdf"
SAMPLE_MISMATCH = FIXTURES / "estimate_sample_mismatch.pdf"
SAMPLE_NO_TOTAL = FIXTURES / "estimate_sample_no_total.pdf"
HAS_PDFPLUMBER = importlib.util.find_spec("pdfplumber") is not None
needs_pdfplumber = unittest.skipUnless(
    HAS_PDFPLUMBER, "pdfplumber 미설치 — 견적서 PDF 읽기 테스트 건너뜀"
)

REVIEWED = RuleSet(version="reviewed-test")
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}
RECEIVED = "2026-11-04T10:00:00+09:00"


def ready_report():
    return assemble_review(fx.judgment(), card=fx.card())  # 승인 단계 없이 바로 산정


def pdf_doc(
    amount, *, reasons=(), serial="SYN-2026-0001", doc_id="est-pdf-synthetic", received=RECEIVED
):
    """PDF 없이 추출 결과 모양만 흉내 낸 견적 dict."""
    return fx.estimate(
        amount,
        doc_id,
        read_by="pdf_extract",
        verification_status="unverified",
        received_at=received,
        serial_no=serial,
        extract_reasons=list(reasons),
    )


@needs_pdfplumber
class ReadEstimatePdfTest(unittest.TestCase):
    def test_sample_10m_reads_all_fields(self):
        d = read_estimate_pdf(SAMPLE_10M, party_id="P1", received_at=RECEIVED)
        self.assertEqual(d["amount"], 10_000_000)
        self.assertEqual((d["parts"], d["labor"], d["vat"]), (6_200_000, 2_890_909, 909_091))
        self.assertEqual(d["serial_no"], "SYN-2026-0001")
        self.assertEqual(d["issued_on"], "2026-11-03")
        self.assertEqual(d["estimate_type"], "insurance")
        self.assertEqual((d["read_by"], d["verification_status"]), ("pdf_extract", "unverified"))
        self.assertEqual(d["party_id"], "P1")  # 업로드 때 받은 값 그대로
        self.assertEqual(d["extract_reasons"], [])
        self.assertTrue(d["document_id"].startswith("est-pdf-"))

    def test_same_file_gives_same_document_id(self):
        a = read_estimate_pdf(SAMPLE_10M, party_id="P1")
        b = read_estimate_pdf(SAMPLE_10M, party_id="P1")
        c = read_estimate_pdf(SAMPLE_MISMATCH, party_id="P1")
        self.assertEqual(a["document_id"], b["document_id"])
        self.assertNotEqual(a["document_id"], c["document_id"])

    def test_mismatch_keeps_total_with_reason(self):
        d = read_estimate_pdf(SAMPLE_MISMATCH, party_id="P1", received_at=RECEIVED)
        self.assertEqual(
            d["amount"], 10_500_000
        )  # 6,200,000 + 2,890,909 + 909,091 = 10,000,000 ≠ 10,500,000
        self.assertEqual(d["extract_reasons"], [SUM_MISMATCH])

    def test_no_total_is_none_not_zero(self):
        d = read_estimate_pdf(SAMPLE_NO_TOTAL, party_id="P1", received_at=RECEIVED)
        self.assertIsNone(d["amount"])
        self.assertEqual(d["extract_reasons"], [TOTAL_NOT_FOUND])
        self.assertEqual(d["parts"], 6_200_000)

    def test_blank_form_reads_nothing(self):
        d = read_estimate_pdf(FORM, party_id="P1")
        self.assertIsNone(d["amount"])
        self.assertEqual(d["extract_reasons"], [TOTAL_NOT_FOUND])
        self.assertEqual((d["serial_no"], d["issued_on"], d["estimate_type"]), (None, None, None))
        self.assertEqual(
            (d["parts"], d["labor"], d["vat"]), (None, None, None)
        )  # 아래 법 조문 숫자를 읽지 않음


class ReaderUnavailableTest(unittest.TestCase):
    def test_missing_pdfplumber_raises_install_hint(self):
        with mock.patch.dict(sys.modules, {"pdfplumber": None}):
            with self.assertRaises(EstimateReaderUnavailable) as ctx:
                read_estimate_pdf(SAMPLE_10M, party_id="P1")
        self.assertEqual(str(ctx.exception), INSTALL_HINT)


class PdfExtractDamagesTest(unittest.TestCase):
    """PDF 없이 추출 결과 dict로 산정 규칙만 확인."""

    def test_pdf_extract_used_without_handler_confirmation(self):
        d = calculate_damages([pdf_doc(10_000_000)], party_id="P1")
        self.assertEqual((d["status"], d["amount"]), ("confirmed", 10_000_000))
        self.assertEqual(d["source_label"], "견적서 PDF 자동 추출 (일련번호 SYN-2026-0001)")

    def test_ocr_rule_unchanged(self):
        d = calculate_damages(
            [fx.estimate(10_000_000, read_by="ocr", verification_status="unverified")],
            party_id="P1",
        )
        self.assertEqual((d["status"], d["amount"]), ("needs_documents", None))
        self.assertEqual(d["reasons"], ["OCR로 읽은 견적 금액 — 담당자 확인 전"])
        self.assertNotIn("source_label", d)

    def test_no_total_pdf_is_pending(self):
        d = calculate_damages([pdf_doc(None, reasons=[TOTAL_NOT_FOUND])], party_id="P1")
        self.assertEqual((d["status"], d["amount"]), ("needs_documents", None))
        self.assertEqual(d["reasons"], [TOTAL_NOT_FOUND])

    def test_no_total_pdf_newer_than_confirmed_holds(self):
        d = calculate_damages(
            [fx.estimate(8_000_000), pdf_doc(None, reasons=[TOTAL_NOT_FOUND])], party_id="P1"
        )
        self.assertIsNone(d["amount"])
        self.assertTrue(d["reasons"][0].startswith("더 최신 견적 확인 필요"))
        self.assertIn(TOTAL_NOT_FOUND, d["reasons"])

    def test_mismatch_calculates_with_reason_in_calculation_and_section8(self):
        docs = [fx.contract(), pdf_doc(10_500_000, reasons=[SUM_MISMATCH])]
        calc = calculate_insurance(
            ready_report(), docs, exclusion_checks=fx.NO_EXCLUSION, rules=REVIEWED
        )
        # 10,500,000 × 20% = 2,100,000 → 최고 500,000 / 지급 10,000,000
        self.assertEqual(
            (calc["recognized_damage"], calc["deductible"], calc["insurance_payment"]),
            (10_500_000, 500_000, 10_000_000),
        )
        self.assertIn(SUM_MISMATCH, calc["reasons"])
        self.assertEqual(
            calc["status"], "confirmed"
        )  # 계산은 진행, 사유로만 알림(보고서 승인에서 확인)
        eight = next(
            s
            for s in assemble_review(fx.judgment(), calc, card=fx.card())["sections"]
            if s["no"] == 8
        )
        self.assertEqual(
            eight["body"]["손해액 출처"], "견적서 PDF 자동 추출 (일련번호 SYN-2026-0001)"
        )
        self.assertIn(SUM_MISMATCH, eight["body"]["사유"])

    def test_manual_estimate_has_no_source_label_in_section8(self):
        calc = calculate_insurance(
            ready_report(),
            [fx.contract(), fx.estimate()],
            exclusion_checks=fx.NO_EXCLUSION,
            rules=REVIEWED,
        )
        eight = next(
            s
            for s in assemble_review(fx.judgment(), calc, card=fx.card())["sections"]
            if s["no"] == 8
        )
        self.assertNotIn("손해액 출처", eight["body"])


@needs_pdfplumber
class UploadEstimateFlowTest(unittest.TestCase):
    """판단(바로 산정, 견적 없음 → 금액 미정) → PDF 업로드 → 자동 재산정 → 확인 완료."""

    actor, case = "handler-1", "synthetic-case"

    def setUp(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=REVIEWED, fetch_clauses=None)

    def tearDown(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)

    def run_(self, coro):
        return asyncio.run(coro)

    def test_upload_recalculates_pending_case(self):
        a, cid = self.actor, self.case
        self.run_(api.submit_judgment(a, fx.card(), fx.judgment()))
        v2 = self.run_(api.add_documents(a, cid, [fx.contract()], case_facts=NO_FLAGS))
        calc = v2["calculation"]
        self.assertEqual(
            (calc["status"], calc["recognized_damage"], calc["insurance_payment"]),
            ("needs_documents", None, None),
        )

        v3 = self.run_(api.upload_estimate(a, cid, str(SAMPLE_10M), "P1"))
        self.assertEqual(v3["versions"]["report_version"], 3)
        self.assertEqual(v3["approval_status"], "draft")
        c = v3["calculation"]
        self.assertEqual(
            (c["status"], c["recognized_damage"], c["deductible"], c["insurance_payment"]),
            ("confirmed", 10_000_000, 500_000, 9_500_000),
        )
        eight = next(s for s in v3["sections"] if s["no"] == 8)
        self.assertEqual(
            eight["body"]["손해액 출처"], "견적서 PDF 자동 추출 (일련번호 SYN-2026-0001)"
        )
        self.assertEqual(eight["body"]["고객 차량 손해액"], "10,000,000원")

        again = self.run_(
            api.upload_estimate(a, cid, str(SAMPLE_10M), "P1")
        )  # 같은 파일 재업로드 → 새 버전 없음
        self.assertEqual(again["versions"]["report_version"], 3)
        pkg = self.run_(api.confirm_report(a, cid, 3))["package"]
        self.assertEqual(
            (pkg["rows"][0]["insurance_payment"], pkg["amounts"]["planned"]), (9_500_000, 6_650_000)
        )

    def test_upload_without_total_keeps_amount_null(self):
        a, cid = self.actor, self.case
        self.run_(api.submit_judgment(a, fx.card(), fx.judgment()))
        v2 = self.run_(api.upload_estimate(a, cid, str(SAMPLE_NO_TOTAL), "P1"))
        doc = next(
            d for d in api.services.repo.get(a, cid).documents if d["kind"] == "repair_estimate"
        )
        self.assertEqual((doc["read_by"], doc["amount"]), ("pdf_extract", None))
        self.assertEqual(
            (v2["calculation"]["status"], v2["calculation"]["insurance_payment"]),
            ("needs_documents", None),
        )
        self.assertIn(TOTAL_NOT_FOUND, v2["calculation"]["reasons"])
        out = self.run_(api.confirm_report(a, cid, 2))
        self.assertEqual(
            (out["package"], out["claim_blocked_reason"]),
            (None, "견적서 필요 — 견적서 추가 후 청구서 생성"),
        )

    def test_upload_on_needs_confirmation_only_stores_document(self):
        a, cid = self.actor, self.case
        self.run_(api.submit_judgment(a, fx.card(), fx.judgment("needs_confirmation")))
        self.assertIsNone(self.run_(api.upload_estimate(a, cid, str(SAMPLE_10M), "P1")))
        doc = next(
            d for d in api.services.repo.get(a, cid).documents if d["kind"] == "repair_estimate"
        )
        self.assertEqual(doc["amount"], 10_000_000)

    def test_upload_registers_pdf_as_evidence(self):
        a, cid = self.actor, self.case
        self.run_(api.submit_judgment(a, fx.card(), fx.judgment()))
        self.run_(api.upload_estimate(a, cid, str(SAMPLE_10M), "P1"))  # 증빙 따로 추가하지 않음
        ev = [d for d in api.services.repo.get(a, cid).documents if d["kind"] == "evidence"]
        self.assertEqual(
            [(d["doc_type"], d["storage_path"]) for d in ev], [("repair_estimate", str(SAMPLE_10M))]
        )
        out = self.run_(api.confirm_report(a, cid, 2))
        pkg = out["package"]
        self.assertEqual((pkg["rows"][0]["evidence_count"], pkg["missing_evidence"]), (1, []))
        self.assertEqual(pkg["evidence"][0]["문서 유형"], "repair_estimate")
        self.run_(
            api.upload_estimate(a, cid, str(SAMPLE_10M), "P1")
        )  # 같은 파일 재업로드 → 증빙 중복 없음
        self.assertEqual(
            sum(d["kind"] == "evidence" for d in api.services.repo.get(a, cid).documents), 1
        )

    def test_upload_requires_case_access(self):
        self.run_(api.submit_judgment(self.actor, fx.card(), fx.judgment()))
        with self.assertRaises(PermissionError):
            self.run_(api.upload_estimate("someone-else", self.case, str(SAMPLE_10M), "P1"))


if __name__ == "__main__":
    unittest.main()
