"""D: 대물·대인 시연 스크립트(scripts/demo_liability.py)가 끝까지 돈다. 코퍼스·네트워크 없음.

숫자 자료는 injury_property_values.build()(PDF 대조 검수 52개). 금액은 손으로 계산했다.
- 대물: 3,456,789 × 30% = 1,037,036
- 대인: 7급 (300만 + 40만) × 30% = 102만 < 300만 → 300만(대인Ⅰ) / 3급 (1,500만 + 152만) × 30%
  = 495.6만 < 1,500만 → 1,500만 → 대인Ⅰ 한도 1,200만 + 대인Ⅱ 300만
"""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.api import reports as api
from app.calculation.rules import RuleSet

ROOT = Path(__file__).resolve().parents[1]
HAS_PDFPLUMBER = importlib.util.find_spec("pdfplumber") is not None


def load_demo():
    spec = importlib.util.spec_from_file_location(
        "demo_liability", ROOT / "scripts" / "demo_liability.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@unittest.skipUnless(HAS_PDFPLUMBER, "pdfplumber 미설치 — 견적서 PDF 읽기 건너뜀")
class DemoLiabilityTest(unittest.TestCase):
    def tearDown(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            schedule_values=(),
        )

    def test_demo_runs_to_claim_with_liability_block(self):
        demo = load_demo()
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            done = asyncio.run(demo.main(out))
            report, pkg = done["report"], done["package"]
            html = (out / "liability_report.html").read_text(encoding="utf-8")
            self.assertTrue((out / "liability_claim_package.html").is_file())
        self.assertEqual(report["approval_status"], "approved")
        liability = report["liability"]
        self.assertEqual(liability["insured_fault"], 30)
        prop = liability["property"]
        self.assertEqual(
            (prop["status"], prop["repair_cost"], prop["amount"]), ("draft", 3_456_789, 1_037_036)
        )
        self.assertEqual(prop["reasons"], [])  # 상대 견적서 합계 일치(경고 없음)
        self.assertEqual(prop["source_document_ids"][0][:8], "est-pdf-")
        people = {i["label"]: (i["bodily_1"], i["bodily_2"]) for i in liability["injuries"]}
        self.assertEqual(
            people, {"상대 운전자": (3_000_000, 0), "상대 동승자": (12_000_000, 3_000_000)}
        )
        block = next(s for s in report["sections"] if s["no"] == 8)["body"][demo.KEY]
        self.assertEqual(block["대인 합계"], {"대인Ⅰ": "15,000,000원", "대인Ⅱ": "3,000,000원"})
        self.assertIn(demo.KEY, html)
        # 자차 산정·구상 청구서는 대물·대인과 별개로 그대로 나온다
        self.assertEqual(report["calculation"]["insurance_payment"], 9_500_000)
        self.assertEqual(pkg["rows"][0]["planned_share"], 6_650_000)
        self.assertEqual(pkg["rows"][0]["claim_no"], "synthetic-case-C1")


if __name__ == "__main__":
    unittest.main()
