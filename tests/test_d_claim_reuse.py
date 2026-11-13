"""D(#194): 청구 내용이 같으면 보고서 새 버전을 확인해도 구상 청구서 번호(C1)를 유지한다(메모리).

청구 내용 = 금액·과실·상대 정보·근거서·증빙·기본 가정(claim_content). 보고서 버전·청구 번호·증빙의
'관련 구상 건'·확인 기록은 빼고 비교한다. Postgres 저장·복원은 test_d_report_store_db.py.
"""

import asyncio
import json
import unittest

from app.api import reports as api
from app.calculation.rules import RuleSet
from app.claims.package import claim_content
from tests import fixtures_d as fx

ACTOR, CASE = "handler-1", "synthetic-case"
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}
INJURED = {
    "kind": "injured_person",
    "document_id": "inj-1",
    "person_id": "P2-driver",
    "grade": 7,
    "treatment_cost": 3_000_000,
    "received_at": "2026-10-26T10:00:00+09:00",
}


def run(coro):
    return asyncio.run(coro)


def add(docs, facts=None):
    return run(api.add_documents(ACTOR, CASE, docs, case_facts=facts))


def confirm(version, counterparty=None):
    return run(
        api.confirm_report(ACTOR, CASE, version, counterparty=counterparty or fx.counterparty())
    )


def ver(report):
    return report["versions"]["report_version"]


def injured(doc_id, cost):
    return {**INJURED, "document_id": doc_id, "treatment_cost": cost}


def route_client(actor: str):
    """실제 앱 경로(인증은 로컬 개발 사용자). 응답 모델(TypedDict)을 거친 결과를 본다."""
    from fastapi.testclient import TestClient

    from app.config import Settings, get_settings
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="local",
        on_vercel=False,
        supabase_url=None,
        supabase_jwt_secret=None,
        jwt_audience="authenticated",
        cors_allow_origins=(),
        dev_actor_id=actor,
    )
    return app, TestClient(app, raise_server_exceptions=False)


class ClaimReuseTest(unittest.TestCase):
    def setUp(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)
        run(api.submit_judgment(ACTOR, fx.card(), fx.judgment()))
        self.v2 = add([fx.contract(), fx.estimate(), *fx.evidence()], NO_FLAGS)
        self.first = confirm(ver(self.v2))

    def tearDown(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(),
            fetch_clauses=None,
            schedule_values=(),
        )

    def packages(self):
        return run(api.services.repo.get(ACTOR, CASE)).packages

    def test_first_confirm_makes_c1_and_is_not_reused(self):
        pkg = self.first["package"]
        self.assertEqual((pkg["package_version"], pkg["report_version"]), (1, ver(self.v2)))
        self.assertEqual(pkg["rows"][0]["claim_no"], "synthetic-case-C1")
        self.assertEqual(
            (self.first["claim_reused"], self.first["claim_reused_from"]), (False, None)
        )
        self.assertEqual(
            self.first["report"]["claim_package"],
            {"package_version": 1, "report_version": ver(self.v2)},
        )

    def test_liability_only_change_keeps_c1(self):
        """1) 대물·대인 입력만 바뀐 새 버전(v3)을 확인해도 C1 그대로."""
        v3 = add([injured("inj-1", 3_000_000)])
        self.assertEqual(ver(v3), ver(self.v2) + 1)
        self.assertIn("대물·대인 1차 판정", next(s for s in v3["sections"] if s["no"] == 8)["body"])
        out = confirm(ver(v3))
        self.assertIs(out["package"], self.first["package"])
        self.assertEqual(out["package"]["rows"][0]["claim_no"], "synthetic-case-C1")
        ref = {"package_version": 1, "report_version": ver(self.v2)}
        self.assertEqual((out["claim_reused"], out["claim_reused_from"]), (True, ref))
        self.assertEqual(out["report"]["approval_status"], "approved")  # 보고서 확인은 그대로
        self.assertEqual(out["report"]["claim_package"], ref)
        self.assertEqual(len(self.packages()), 1)

    def test_get_claim_of_new_version_returns_c1(self):
        """3) get_claim(v3)은 C1(v2 기준)을 돌려준다. v2도 C1."""
        v3 = add([injured("inj-1", 3_000_000)])
        confirm(ver(v3))
        got3 = run(api.get_claim(ACTOR, CASE, ver(v3)))["package"]
        got2 = run(api.get_claim(ACTOR, CASE, ver(self.v2)))["package"]
        self.assertEqual((got3["package_version"], got3["report_version"]), (1, ver(self.v2)))
        self.assertIs(got3, got2)

    def test_content_change_makes_c2(self):
        """2) 금액·증빙·상대 정보·협의 과실이 바뀌면 지금처럼 새 청구서."""
        other_cp = {**fx.counterparty(), "claim_no": "synthetic-cp-999"}
        photo = {
            "kind": "evidence",
            "document_id": "ev-new",
            "doc_type": "photo",
            "storage_path": "synthetic/ev-new.jpg",
        }
        new_estimate = fx.estimate(
            12_000_000, doc_id="est-2", received_at="2026-10-27T10:00:00+09:00"
        )

        def counterparty_info():
            return confirm(ver(add([injured("inj-1", 3_000_000)])), other_cp)

        def evidence():
            return confirm(ver(add([injured("inj-1", 3_000_000), photo])))

        def amount():
            return confirm(ver(add([new_estimate])))

        def agreed_fault():
            v = run(
                api.set_agreed_fault(
                    ACTOR, CASE, agreed_counterparty_fault=60, fault_change_reason="협의"
                )
            )
            return confirm(ver(v))

        cases = [
            ("상대 정보", counterparty_info),
            ("증빙", evidence),
            ("금액", amount),
            ("협의 과실", agreed_fault),
        ]
        for label, step in cases:
            with self.subTest(label):
                self.setUp()
                out = step()
                pkg = out["package"]
                self.assertEqual(pkg["package_version"], 2, label)
                self.assertEqual(pkg["rows"][0]["claim_no"], "synthetic-case-C2")
                self.assertEqual((out["claim_reused"], out["claim_reused_from"]), (False, None))
                self.assertNotEqual(claim_content(pkg), claim_content(self.first["package"]))
                self.assertEqual(len(self.packages()), 2)

    def test_same_version_reconfirm_returns_same_package(self):
        """5) 같은 버전을 다시 확인하면 같은 청구서, claim_reused는 false."""
        again = confirm(ver(self.v2))
        self.assertIs(again["package"], self.first["package"])
        self.assertEqual((again["claim_reused"], again["claim_reused_from"]), (False, None))
        self.assertEqual(len(self.packages()), 1)

    def test_content_back_to_c1_reuses_c1(self):
        """내용이 바뀌었다가(C2) 다시 C1과 같아지면 C1을 다시 쓴다."""
        other_cp = {**fx.counterparty(), "claim_no": "synthetic-cp-999"}
        c2 = confirm(ver(add([injured("inj-1", 3_000_000)])), other_cp)
        self.assertEqual(c2["package"]["package_version"], 2)
        back = confirm(ver(add([injured("inj-2", 4_000_000)])))  # 상대 정보 원래대로
        self.assertIs(back["package"], self.first["package"])
        self.assertEqual(
            back["claim_reused_from"], {"package_version": 1, "report_version": ver(self.v2)}
        )
        self.assertEqual(len(self.packages()), 2)

    def test_legacy_key_package_is_reused_by_content(self):
        """4) 예전 키(보고서 버전 포함)로 만든 청구서도 내용이 같으면 그대로 쓴다."""
        self.packages()[0]["idempotency_key"] = "legacy-key-with-report-version"
        out = confirm(ver(add([injured("inj-1", 3_000_000)])))
        self.assertIs(out["package"], self.first["package"])
        self.assertTrue(out["claim_reused"])
        self.assertEqual(len(self.packages()), 1)

    def test_blocked_claim_has_reuse_fields(self):
        """청구서를 못 만드는 확인 완료도 새 칸을 false·null로 돌려준다."""
        api.configure(repo=api.InMemoryReportRepository())
        v1 = run(api.submit_judgment(ACTOR, fx.card(), fx.judgment()))
        out = confirm(ver(v1))
        self.assertIsNone(out["package"])
        self.assertEqual((out["claim_reused"], out["claim_reused_from"]), (False, None))
        self.assertNotIn("claim_package", out["report"])

    def test_liability_evidence_does_not_mark_clauses_connected(self):
        """대물·대인 근거(별표 조항 ID)가 8번 evidence_ids에 있어도 약관 조항 검증 연결이 아니다.

        예전에는 evidence_ids로 판단해 청구서 rule_notice가 '약관 조항 원문 연결'로 바뀌었다(#194).
        """
        v3 = add([injured("inj-1", 3_000_000)])
        ids = next(s for s in v3["sections"] if s["no"] == 8)["evidence_ids"]
        self.assertTrue(any(":payment_standard:별표3:1:" in i for i in ids))
        out = confirm(ver(v3))
        self.assertEqual(out["package"]["rule_notice"], "예시 규칙 기준 — 약관 검수 전 초안")

    def test_legacy_report_without_ref_uses_report_version(self):
        """claim_package 기록이 없는 예전 보고서는 그 버전으로 만든 청구서를 찾는다."""
        rec = run(api.services.repo.get(ACTOR, CASE))
        rec.report(ver(self.v2)).pop("claim_package")
        got = run(api.get_claim(ACTOR, CASE, ver(self.v2)))["package"]
        self.assertIs(got, self.first["package"])


class ClaimRouteTest(unittest.TestCase):
    """HTTP 경로: v3 확인 뒤 GET /cases/{id}/reports/3/claim이 404가 아니라 200으로 C1을 준다."""

    def setUp(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)
        self.app, self.client = route_client(ACTOR)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)

    def test_claim_route_returns_c1_for_v3(self):
        run(api.submit_judgment(ACTOR, fx.card(), fx.judgment()))
        body = {"documents": [fx.contract(), fx.estimate(), *fx.evidence()], "case_facts": NO_FLAGS}
        v2 = self.client.post(f"/cases/{CASE}/documents", json=body).json()["versions"]
        cp = {"counterparty": fx.counterparty()}
        c1 = self.client.post(f"/cases/{CASE}/reports/{v2['report_version']}/confirm", json=cp)
        self.assertEqual(c1.status_code, 200, c1.text)
        res = self.client.post(f"/cases/{CASE}/documents", json={"documents": [INJURED]})
        self.assertEqual(res.json()["versions"]["report_version"], 3)

        res = self.client.get(f"/cases/{CASE}/reports/3/claim")  # 확인 전에는 없다
        self.assertEqual((res.status_code, res.json()["error"]["code"]), (404, "not_found"))
        res = self.client.post(f"/cases/{CASE}/reports/3/confirm", json=cp)
        self.assertEqual(res.status_code, 200, res.text)
        ref = {"package_version": 1, "report_version": 2}
        self.assertEqual((res.json()["claim_reused"], res.json()["claim_reused_from"]), (True, ref))
        self.assertEqual(res.json()["report"]["claim_package"], ref)

        res = self.client.get(f"/cases/{CASE}/reports/3/claim")
        self.assertEqual(res.status_code, 200, res.text)
        package = res.json()["package"]
        self.assertEqual(package, c1.json()["package"])  # C1 그대로(응답 모델을 거쳐도 같다)
        self.assertEqual((package["package_version"], package["report_version"]), (1, 2))
        self.assertEqual(package["rows"][0]["claim_no"], "synthetic-case-C1")
        self.assertEqual(
            res.json(),
            json.loads(json.dumps(run(api.get_claim(ACTOR, CASE, 3)), ensure_ascii=False)),
        )


if __name__ == "__main__":
    unittest.main()
