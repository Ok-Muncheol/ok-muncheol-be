"""C: 대물·대인 1차 판정을 보고서 8번에 붙이는 흐름(add_documents → 재산정 → 8번 새 칸).

기존 자차 8번 칸·9번 구상은 그대로여야 하고, 대물·대인 입력이 없는 사건은 지금 보고서와 같아야 한다.
숫자 자료는 app.ingestion.injury_property_values.build()(PDF 대조 검수 52개). 사고일 2026-10-20.
"""

import asyncio
import json
import unittest

from app.api import reports as api
from app.calculation.damages import calculate_damages
from app.calculation.liability import liability_from_report
from app.calculation.rules import RuleSet
from app.ingestion import injury_property_values
from app.reports.render import report_html
from tests import fixtures_d as fx

REVIEWED = RuleSet(version="reviewed-test")
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}
VALUES = tuple(injury_property_values.build({"document_id": "samsung-personal-auto-2026-11-11"}))
KEY = "대물·대인 1차 판정"


def section(report, no: int) -> dict:
    return next(s for s in report["sections"] if s["no"] == no)


def injured(doc_id="inj-1", person="P2-driver", grade=7, cost=3_000_000, at="10:00", **kw):
    return {
        "kind": "injured_person",
        "document_id": doc_id,
        "person_id": person,
        "grade": grade,
        "treatment_cost": cost,
        "received_at": f"2026-10-26T{at}:00+09:00",
        **kw,
    }


def cp_estimate(amount=3_000_000):
    return fx.estimate(amount, doc_id="cp-est-1", party_id="P2")


class LiabilityReportTest(unittest.TestCase):
    actor, case = "handler-1", "synthetic-case"

    def setUp(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=REVIEWED,
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

    def run_(self, coro):
        return asyncio.run(coro)

    def add(self, docs, facts=None):
        return self.run_(api.add_documents(self.actor, self.case, docs, case_facts=facts))

    def ready(self):
        """v1 판단(우리 차 P1 = 도표 A, 우리 과실 30%) → v2 자차 계약·견적·증빙."""
        self.run_(api.submit_judgment(self.actor, fx.card(), fx.judgment(fault_A=30)))
        return self.add([fx.contract(), fx.estimate(), *fx.evidence()], NO_FLAGS)

    def test_no_liability_input_keeps_report_as_before(self):
        report = self.ready()
        self.assertNotIn("liability", report)
        self.assertNotIn(KEY, section(report, 8)["body"])
        # 진단서 등 증빙만 붙여도 대물·대인 입력이 아니라 새 버전·새 칸이 없다
        evidence = {"kind": "evidence", "document_id": "dx-1", "doc_type": "medical_certificate"}
        again = self.add([{**evidence, "storage_path": "synthetic/dx-1.pdf"}])
        self.assertEqual(again["versions"]["report_version"], report["versions"]["report_version"])
        self.assertNotIn("liability", again)

    def test_property_and_injury_are_added_without_touching_own_damage_or_recovery(self):
        before = self.ready()
        after = self.add([cp_estimate(), injured()])
        self.assertEqual(
            after["versions"]["report_version"], before["versions"]["report_version"] + 1
        )
        # 자차 8번 기존 칸과 9번 구상은 그대로(상대 견적은 자차에 섞이지 않음)
        body8 = dict(section(after, 8)["body"])
        block = body8.pop(KEY)
        self.assertEqual(body8, section(before, 8)["body"])
        self.assertEqual(section(after, 9), section(before, 9))
        drop = lambda c: {k: v for k, v in c.items() if k != "versions"}  # noqa: E731
        self.assertEqual(drop(after["calculation"]), drop(before["calculation"]))
        # 대물: 3,000,000 × 30% = 900,000
        liability = after["liability"]
        self.assertEqual(liability["insured_fault"], 30)
        self.assertEqual(liability["property"]["amount"], 900_000)
        self.assertEqual(block["대물"]["대물 초안"], "900,000원")
        # 대인 7급: (300만 + 40만) × 30% = 102만 < 치료관계비 300만 → 300만, 한도 500만 안
        person = liability["injuries"][0]
        self.assertEqual((person["bodily_1"], person["bodily_2"]), (3_000_000, 0))
        self.assertTrue(person["treatment_guarantee"])
        self.assertEqual(block["대인 합계"], {"대인Ⅰ": "3,000,000원", "대인Ⅱ": "0원"})
        html = report_html(after)
        self.assertIn(KEY, html)
        self.assertIn("900,000원", html)
        evidence = section(after, 8)["evidence_ids"]
        self.assertIn("cp-est-1", evidence)
        self.assertIn("samsung-personal-auto-2026-11-11:별표1-나-2-부상위자료:7급", evidence)

    def test_label_and_latest_input_per_person(self):
        self.ready()
        report = self.add(
            [
                injured("inj-1", grade=7, at="10:00", label="상대 운전자"),
                injured("inj-2", grade=3, cost=5_000_000, at="11:00", label="상대 운전자"),
            ]
        )
        people = report["liability"]["injuries"]
        self.assertEqual(len(people), 1)
        self.assertEqual((people[0]["grade"], people[0]["treatment_cost"]), (3, 5_000_000))
        self.assertEqual(section(report, 8)["body"][KEY]["대인"][0]["피해자"], "상대 운전자")
        self.assertIsNone(report["liability"]["property"])  # 대물 입력 없음

    def test_property_input_supplies_limit(self):
        self.ready()
        limit = {
            "kind": "property_damage_input",
            "document_id": "pd-1",
            "property_damage_limit": 500_000,
            "received_at": "2026-10-26T10:00:00+09:00",
        }
        report = self.add([cp_estimate(), limit])
        prop = report["liability"]["property"]
        self.assertEqual((prop["amount"], prop["limit_applied"]), (500_000, True))
        labels = [f["항목"] for f in section(report, 8)["body"][KEY]["대물"]["확인 필요"]]
        self.assertIn("전손 여부 확인 필요", labels)
        self.assertIn("대차료·휴차료 별도 확인", labels)

    def test_without_schedule_values_injury_has_no_amount(self):
        api.configure(schedule_values=())
        self.ready()
        person = self.add([injured()])["liability"]["injuries"][0]
        self.assertEqual((person["status"], person["bodily_1"]), ("needs_input", None))
        self.assertTrue(any("자료 없음" in r for r in person["reasons"]))

    def test_invalid_injured_input_is_rejected_before_saving(self):
        self.ready()
        bad = [
            injured(grade=15),
            injured(cost=-1),
            injured(is_driver="yes"),
            {**injured(), "person_id": ""},
            {**injured(), "received_at": "어제"},
            {
                "kind": "property_damage_input",
                "document_id": "pd-x",
                "release_date": "2026/01/01",
                "received_at": "2026-10-26T10:00:00+09:00",
            },
        ]
        for doc in bad:
            with self.subTest(doc=doc), fx.raises_api(self, 422, "invalid_documents"):
                self.add([doc])
        self.assertEqual(
            [
                d
                for d in fx.record(self.actor, self.case).documents
                if d["kind"] == "injured_person"
            ],
            [],
        )


class LiabilityRouteTest(unittest.TestCase):
    """HTTP 응답 모델(TypedDict)을 거쳐도 새 칸(liability·8번 대물·대인)이 그대로 나오는지."""

    def setUp(self):
        from fastapi.testclient import TestClient

        from app.config import Settings, get_settings
        from app.main import app

        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=REVIEWED,
            fetch_clauses=None,
            schedule_values=VALUES,
        )
        self.app = app
        app.dependency_overrides[get_settings] = lambda: Settings(
            app_env="local",
            on_vercel=False,
            supabase_url=None,
            supabase_jwt_secret=None,
            jwt_audience="authenticated",
            cors_allow_origins=(),
            dev_actor_id="handler-1",
        )
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            schedule_values=(),
        )

    def test_documents_route_returns_liability_unchanged(self):
        asyncio.run(api.submit_judgment("handler-1", fx.card(), fx.judgment(fault_A=30)))
        docs = [fx.contract(), fx.estimate(), *fx.evidence(), cp_estimate(), injured(label="A")]
        res = self.client.post(
            "/cases/synthetic-case/documents", json={"documents": docs, "case_facts": NO_FLAGS}
        )
        self.assertEqual(res.status_code, 200, res.text)
        version = res.json()["versions"]["report_version"]
        stored = asyncio.run(api.get_report("handler-1", "synthetic-case", version))
        self.assertEqual(res.json(), json.loads(json.dumps(stored, ensure_ascii=False)))
        self.assertEqual(res.json()["liability"]["injuries"][0]["label"], "A")
        bad = {"documents": [injured(grade=0)]}
        res = self.client.post("/cases/synthetic-case/documents", json=bad)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "invalid_documents")


class LiabilityFromReportTest(unittest.TestCase):
    def report(self, insured="P1", fault_A=30):
        mapping = {"insured_party_id": insured, "chart_party_A_id": "P1", "chart_party_B_id": "P2"}
        judgment = fx.judgment(fault_A=fault_A)
        sections = [{"no": 2, "body": {"사고 일자": "2026-10-20"}}]
        return {"party_mapping": mapping, "judgment": judgment, "sections": sections}

    def test_insured_on_chart_b_uses_fault_b_and_other_side_as_counterparty(self):
        docs = [fx.estimate(1_000_000, doc_id="a-est", party_id="P1")]
        r = liability_from_report(self.report(insured="P2"), docs, VALUES)
        self.assertEqual(r["insured_fault"], 70)
        self.assertEqual(r["property"]["counterparty_id"], "P1")
        self.assertEqual(r["property"]["amount"], 700_000)

    def test_no_mapping_gives_no_amount_with_reason(self):
        r = liability_from_report(self.report(insured=None), [injured()], VALUES)
        person = r["injuries"][0]
        self.assertIsNone(person["bodily_1"])
        self.assertTrue(person["reasons"][0].startswith("당사자 매핑 없음"))

    def test_no_inputs_returns_none(self):
        own = [fx.estimate(), fx.contract()]
        self.assertIsNone(liability_from_report(self.report(), own, VALUES))


class OwnDamageEstimateMixTest(unittest.TestCase):
    def test_unknown_party_with_two_parties_picks_nothing(self):
        docs = [fx.estimate(1_000_000), fx.estimate(9_000_000, doc_id="cp", party_id="P2")]
        d = calculate_damages(docs, party_id=None)
        self.assertEqual((d["status"], d["amount"]), ("needs_documents", None))
        self.assertIn("우리 차 당사자 미지정", d["reasons"][0])

    def test_unknown_party_with_one_party_is_unchanged(self):
        d = calculate_damages([fx.estimate(1_000_000)], party_id=None)
        self.assertEqual((d["status"], d["amount"]), ("confirmed", 1_000_000))

    def test_known_party_ignores_counterparty_estimate(self):
        docs = [fx.estimate(1_000_000), fx.estimate(9_000_000, doc_id="cp", party_id="P2")]
        self.assertEqual(calculate_damages(docs, party_id="P1")["amount"], 1_000_000)


if __name__ == "__main__":
    unittest.main()
