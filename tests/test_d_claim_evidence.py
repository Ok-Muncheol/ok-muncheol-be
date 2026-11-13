"""D(#198): 구상 청구서 증빙에는 우리 자차 구상 증빙만 넣는다(메모리 저장소).

- 상대 차 견적서는 대물 판정 근거로만 쓴다. 견적서 PDF가 자동 등록한 증빙("ev-<견적 ID>",
  doc_type="repair_estimate")에는 당사자가 없어 원래 견적서의 party_id로 판단한다.
- 진단서·진료비 영수증(medical_certificate·medical_bill)은 대인 판정 근거로만 쓴다.
- 사건 서류에는 그대로 남고, 대인·대물 판정과 우리 쪽 일반 증빙(사진·영수증 등)은 그대로다.
Postgres 저장·복원은 test_d_report_store_db.py.
"""

import asyncio
import unittest
from pathlib import Path

from app.api import reports as api
from app.calculation.rules import RuleSet
from app.claims.package import claim_evidence
from app.ingestion import injury_property_values
from tests import fixtures_d as fx

ACTOR, CASE = "handler-1", "synthetic-case"
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}
FIXTURES = Path(__file__).resolve().parent / "fixtures"
OUR_PDF = FIXTURES / "estimate_sample_10m.pdf"
CP_PDF = FIXTURES / "estimate_sample_cp_3m.pdf"
VALUES = tuple(injury_property_values.build({"document_id": "samsung-personal-auto-2026-11-11"}))
INJURED = {
    "kind": "injured_person",
    "document_id": "inj-1",
    "person_id": "P2-driver",
    "grade": 7,
    "treatment_cost": 3_000_000,
    "received_at": "2026-10-26T10:00:00+09:00",
}


def medical(doc_id, doc_type="medical_certificate", case=CASE):
    return {
        "kind": "evidence",
        "document_id": doc_id,
        "doc_type": doc_type,
        "storage_path": f"case-files/{case}/medical/{doc_id}.pdf",
    }


def run(coro):
    return asyncio.run(coro)


def files(pkg):
    return [e["파일"] for e in pkg["evidence"]]


class ClaimEvidenceUnitTest(unittest.TestCase):
    """자동 증빙에는 party_id가 없다 → 원래 견적서의 party_id로 판단한다."""

    def docs(self, estimate_party):
        estimate = {"kind": "repair_estimate", "document_id": "est-pdf-x", "amount": 1}
        if estimate_party is not ...:
            estimate["party_id"] = estimate_party
        auto = {
            "kind": "evidence",
            "document_id": "ev-est-pdf-x",
            "doc_type": "repair_estimate",
            "storage_path": "s/x.pdf",
        }
        self.assertNotIn("party_id", auto)  # 자동 증빙 자체에는 당사자가 없다
        return [estimate, auto]

    def test_auto_evidence_follows_original_estimate_party(self):
        ids = lambda docs, insured: [d["document_id"] for d in claim_evidence(docs, insured)]  # noqa: E731
        self.assertEqual(ids(self.docs("P2"), "P1"), [])  # 상대 차 견적 → 뺌
        self.assertEqual(ids(self.docs("P1"), "P1"), ["ev-est-pdf-x"])  # 우리 견적 → 둠
        self.assertEqual(ids(self.docs("P2"), "P2"), ["ev-est-pdf-x"])  # 우리 = P2면 둠
        self.assertEqual(ids(self.docs(None), "P1"), ["ev-est-pdf-x"])  # 당사자 없음 → 예전처럼 둠
        self.assertEqual(ids(self.docs(...), "P1"), ["ev-est-pdf-x"])  # 칸 자체가 없어도 둠

    def test_only_kind_and_doc_type_decide(self):
        """이름이 아니라 kind·doc_type·등록 출처로 거른다: 같은 이름의 다른 종류 증빙은 그대로."""
        docs = [
            {"kind": "repair_estimate", "document_id": "est-pdf-x", "party_id": "P2"},
            {"kind": "evidence", "document_id": "ev-est-pdf-x", "doc_type": "photo"},  # 사진
            {"kind": "evidence", "document_id": "dx", "doc_type": "medical_certificate"},
            {"kind": "evidence", "document_id": "bill", "doc_type": "medical_bill"},
            {"kind": "evidence", "document_id": "rcpt", "doc_type": "receipt"},
        ]
        kept = [d["document_id"] for d in claim_evidence(docs, "P1")]
        self.assertEqual(kept, ["ev-est-pdf-x", "rcpt"])


class ClaimEvidenceFlowTest(unittest.TestCase):
    def setUp(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            schedule_values=VALUES,
        )

    def tearDown(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            schedule_values=(),
        )

    def ready(self, insured="P1"):
        run(api.submit_judgment(ACTOR, fx.card(insured), fx.judgment()))
        run(api.upload_estimate(ACTOR, CASE, str(OUR_PDF), party_id=insured))
        return run(api.add_documents(ACTOR, CASE, [fx.contract(), *fx.evidence()], NO_FLAGS))

    def confirm(self, report):
        version = report["versions"]["report_version"]
        return run(api.confirm_report(ACTOR, CASE, version, counterparty=fx.counterparty()))

    def record(self):
        return run(api.services.repo.get(ACTOR, CASE))

    def test_medical_certificate_keeps_c1_and_stays_out_of_claim(self):
        """김지은 님 재현: 진단서를 붙이고 다시 확인해도 C1, 청구서 증빙에 진단서가 없다.

        고치기 전에는 진단서가 청구서 증빙에 들어가 청구 내용이 달라져 C2가 됐다.
        """
        v = self.ready()
        c1 = self.confirm(v)["package"]
        again = run(
            api.add_documents(ACTOR, CASE, [medical("dx-1"), medical("bill-1", "medical_bill")])
        )
        self.assertEqual(again["versions"], v["versions"])  # 증빙만 → 새 버전 없음(#185)
        out = self.confirm(v)
        self.assertIs(out["package"], c1)
        self.assertNotIn("dx-1", files(out["package"]))
        self.assertNotIn("bill-1", files(out["package"]))
        # 대인 입력 + 진단서로 새 버전(v+1)이 생겨도 C1(#195)
        v2 = run(api.add_documents(ACTOR, CASE, [INJURED, medical("dx-2")]))
        out = self.confirm(v2)
        self.assertIs(out["package"], c1)
        self.assertTrue(out["claim_reused"])
        self.assertEqual([p["package_version"] for p in self.record().packages], [1])
        # 진단서는 사건 서류에는 남는다
        kept = [d["document_id"] for d in self.record().documents if d.get("kind") == "evidence"]
        self.assertTrue({"dx-1", "bill-1", "dx-2"} <= set(kept))

    def test_counterparty_estimate_pdf_is_property_basis_only(self):
        v = self.ready()
        c1 = self.confirm(v)["package"]
        v2 = run(api.upload_estimate(ACTOR, CASE, str(CP_PDF), party_id="P2"))
        cp_estimate = next(
            d
            for d in self.record().documents
            if d.get("kind") == "repair_estimate" and d.get("party_id") == "P2"
        )
        auto = next(
            d
            for d in self.record().documents
            if d["document_id"] == f"ev-{cp_estimate['document_id']}"
        )
        self.assertEqual(auto["doc_type"], "repair_estimate")
        self.assertNotIn("party_id", auto)  # 원래 견적서의 party_id로 판단해야 하는 이유
        # 대물 판정은 그대로 상대 차 견적서를 쓴다
        prop = v2["liability"]["property"]
        self.assertEqual((prop["repair_cost"], prop["amount"]), (3_456_789, 1_037_036))
        self.assertIn(cp_estimate["document_id"], prop["source_document_ids"])
        self.assertIn(
            cp_estimate["document_id"],
            next(s for s in v2["sections"] if s["no"] == 8)["evidence_ids"],
        )
        # 청구서 증빙에는 없고, C1 그대로
        out = self.confirm(v2)
        self.assertIs(out["package"], c1)
        self.assertNotIn(auto["document_id"], files(out["package"]))
        our_auto = [f for f in files(out["package"]) if f.startswith("ev-est-pdf-")]
        self.assertEqual(len(our_auto), 1)

    def test_general_evidence_and_counts_unchanged(self):
        """우리 견적서 자동 증빙 + 사진·영수증·수리 명세 4건은 그대로, 증빙 수도 같다."""
        v = self.ready()
        run(api.upload_estimate(ACTOR, CASE, str(CP_PDF), party_id="P2"))
        run(api.add_documents(ACTOR, CASE, [medical("dx-1")]))
        pkg = self.confirm(self.record().latest)["package"]
        doc_types = sorted(e["문서 유형"] for e in pkg["evidence"])
        self.assertEqual(doc_types, ["photo", "receipt", "repair_estimate", "repair_invoice"])
        self.assertEqual(pkg["rows"][0]["evidence_count"], 4)
        self.assertEqual(pkg["missing_evidence"], [])
        self.assertEqual(v["calculation"]["insurance_payment"], pkg["rows"][0]["insurance_payment"])

    def test_insured_on_chart_b(self):
        """우리 차가 도표 B(P2)면 P1 견적서가 상대 차 견적서다."""
        self.ready(insured="P2")
        run(api.upload_estimate(ACTOR, CASE, str(CP_PDF), party_id="P1"))
        pkg = self.confirm(self.record().latest)["package"]
        estimates = {
            d["document_id"]: d["party_id"]
            for d in self.record().documents
            if d.get("kind") == "repair_estimate"
        }
        ours = [f"ev-{i}" for i, p in estimates.items() if p == "P2"]
        theirs = [f"ev-{i}" for i, p in estimates.items() if p == "P1"]
        self.assertEqual([f for f in files(pkg) if f.startswith("ev-est-pdf-")], ours)
        self.assertFalse(set(theirs) & set(files(pkg)))

    def test_injury_judgment_unchanged(self):
        """대인 판정은 진단서 유무와 상관없이 그대로(진단서는 첨부만, 읽지 않음)."""
        self.ready()
        before = run(api.add_documents(ACTOR, CASE, [INJURED]))
        without = before["liability"]["injuries"]
        with_dx = run(api.add_documents(ACTOR, CASE, [medical("dx-1")]))
        self.assertEqual(with_dx["versions"], before["versions"])  # 증빙만 → 새 버전 없음
        latest = self.record().latest["liability"]["injuries"]
        self.assertEqual(latest, without)
        self.assertEqual((latest[0]["bodily_1"], latest[0]["bodily_2"]), (3_000_000, 0))


if __name__ == "__main__":
    unittest.main()
