"""D: 보고서 API 서비스 함수의 오류 → ApiException(404·403·409·422), get_claim, 응답 모양.

HTTP 확인은 테스트 안에서 만든 FastAPI 앱(install_error_handlers)으로 한다.
app/api/routes.py는 쓰지 않는다.
"""

import asyncio
import typing
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import reports as api
from app.api.errors import ApiException, install_error_handlers
from app.calculation.rules import RuleSet
from app.schemas.reports import ClaimResult, ConfirmResult
from tests import fixtures_d as fx

ACTOR, OTHER, CASE = "handler-1", "handler-2", "synthetic-case"
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}


def run(coro):
    return asyncio.run(coro)


class ServiceBase(unittest.TestCase):
    def setUp(self):
        api.configure(
            repo=api.InMemoryReportRepository(),
            rules=RuleSet(version="reviewed-test"),
            fetch_clauses=None,
        )

    def tearDown(self):
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None)

    def submit(self, status="draft_ready"):
        return run(api.submit_judgment(ACTOR, fx.card(), fx.judgment(status)))

    def ready(self):
        """v1 판단(견적 없음) → v2 계약·견적·증빙(청구서 조건 충족)."""
        self.submit()
        return run(
            api.add_documents(ACTOR, CASE, [fx.contract(), fx.estimate(), *fx.evidence()], NO_FLAGS)
        )

    def confirm(self, version):
        return run(api.confirm_report(ACTOR, CASE, version, counterparty=fx.counterparty()))


class ErrorMappingTest(ServiceBase):
    def test_unknown_case_is_404(self):
        with fx.raises_api(self, 404, "not_found", "사건이 없습니다"):
            run(api.get_report(ACTOR, "없는-사건", 1))

    def test_unknown_report_version_is_404(self):
        self.submit()
        with fx.raises_api(self, 404, "not_found", "v9"):
            run(api.get_report(ACTOR, CASE, 9))

    def test_other_handlers_case_is_403(self):
        self.submit()
        for call in (
            lambda: api.get_report(OTHER, CASE, 1),
            lambda: api.add_documents(OTHER, CASE, [fx.estimate()]),
            lambda: api.confirm_report(OTHER, CASE, 1),
            lambda: api.get_claim(OTHER, CASE, 1),
            lambda: api.submit_judgment(OTHER, fx.card(), fx.judgment()),
        ):
            with self.subTest(call=call), fx.raises_api(self, 403, "forbidden"):
                run(call())

    def test_upload_estimate_checks_case_before_reading_pdf(self):
        with fx.raises_api(self, 404, "not_found"):
            run(api.upload_estimate(ACTOR, "없는-사건", "/tmp/없는-파일.pdf", "P1"))

    def test_stale_version_is_409(self):
        self.ready()
        with fx.raises_api(self, 409, "version_conflict", "최신 버전"):
            self.confirm(1)

    def test_not_ready_judgment_is_422(self):
        for status in ("needs_confirmation", "deferred"):
            with self.subTest(status=status):
                self.setUp()
                self.submit(status)
                with fx.raises_api(self, 422, "confirm_blocked"):
                    self.confirm(1)

    def test_invalid_case_facts_is_422_and_saves_nothing(self):
        self.submit()
        with fx.raises_api(self, 422, "invalid_case_facts", "과속"):
            run(api.add_documents(ACTOR, CASE, [fx.estimate()], {"과속": True}))
        self.assertEqual(fx.record(ACTOR, CASE).documents, [])

    def test_unconfirmed_card_is_422(self):
        card = {**fx.card(), "status": "draft"}
        with fx.raises_api(self, 422, "card_not_confirmed"):
            run(api.submit_judgment(ACTOR, card, fx.judgment()))

    def test_agreed_fault_errors_are_422(self):
        self.submit()
        cases = (
            {"agreed_counterparty_fault": 120, "fault_change_reason": "협의"},
            {"agreed_counterparty_fault": 50},
            {"clear": True, "fault_change_reason": "되돌림"},
        )
        for kw in cases:
            with self.subTest(kw=kw), fx.raises_api(self, 422, "invalid_agreed_fault"):
                run(api.set_agreed_fault(ACTOR, CASE, **kw))

    def test_internal_errors_are_not_turned_into_404(self):
        class BrokenRepo(api.InMemoryReportRepository):
            def get(self, actor_id, case_id):
                raise KeyError("내부 버그")

        api.configure(repo=BrokenRepo())
        with self.assertRaises(KeyError):  # LookupError 하위지만 404로 숨기지 않는다(500)
            run(api.get_report(ACTOR, CASE, 1))

    def test_internal_exception_classes_are_kept(self):
        for kind in (api.StaleVersion, api.ConfirmBlocked, api.CaseFactsInvalid):
            self.assertTrue(issubclass(kind, ValueError))
        with self.assertRaises(api.CaseFactsInvalid):
            api.validate_case_facts({"음주": "yes"})


class GetClaimTest(ServiceBase):
    def test_returns_package_made_for_that_version(self):
        self.ready()
        confirmed = self.confirm(2)
        claim = run(api.get_claim(ACTOR, CASE, 2))
        self.assertEqual(claim["package"], confirmed["package"])
        self.assertEqual(claim["package"]["report_version"], 2)
        self.assertEqual(claim["html"], confirmed["html"])

    def test_latest_package_for_version_after_reconfirm(self):
        self.ready()
        self.confirm(2)
        again = self.confirm(2)  # 같은 입력 → 같은 패키지
        self.assertEqual(run(api.get_claim(ACTOR, CASE, 2))["package"], again["package"])

    def test_no_claim_for_version_is_404(self):
        self.ready()
        self.confirm(2)
        with fx.raises_api(self, 404, "not_found", "청구서가 없습니다"):
            run(api.get_claim(ACTOR, CASE, 1))  # v1은 확인하지 않음

    def test_blocked_confirm_leaves_no_claim(self):
        self.submit()
        result = self.confirm(1)  # 견적서 없음 → 청구서 없음
        self.assertIsNone(result["package"])
        with fx.raises_api(self, 404, "not_found"):
            run(api.get_claim(ACTOR, CASE, 1))

    def test_unknown_version_is_404(self):
        self.ready()
        with fx.raises_api(self, 404, "not_found", "v7"):
            run(api.get_claim(ACTOR, CASE, 7))


class ResponseShapeTest(ServiceBase):
    def test_confirm_result_keys_match_typed_dict(self):
        keys = set(typing.get_type_hints(ConfirmResult))
        self.ready()
        made = self.confirm(2)
        self.setUp()
        self.submit()
        blocked = self.confirm(1)
        for name, result in (("made", made), ("blocked", blocked)):
            with self.subTest(name):
                self.assertEqual(set(result), keys)
        self.assertEqual(made["claim_blocked_reasons"], [])
        self.assertIsNone(made["claim_blocked_reason"])
        self.assertEqual(blocked["claim_blocked_reason"], blocked["claim_blocked_reasons"][0])
        self.assertIsNone(blocked["html"])

    def test_claim_result_keys_match_typed_dict(self):
        self.ready()
        self.confirm(2)
        claim = run(api.get_claim(ACTOR, CASE, 2))
        self.assertEqual(set(claim), set(typing.get_type_hints(ClaimResult)))
        self.assertIsInstance(claim["html"], str)

    def test_service_return_annotations(self):
        self.assertIs(typing.get_type_hints(api.confirm_report)["return"], ConfirmResult)
        self.assertIs(typing.get_type_hints(api.get_claim)["return"], ClaimResult)


def _app() -> FastAPI:
    """오류 응답 확인용 최소 앱. 실제 경로 이름은 routes.py(구준휘 님)가 정한다."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/r/{case_id}/{version}")
    async def report(case_id: str, version: int, actor: str = ACTOR):
        return await api.get_report(actor, case_id, version)

    @app.post("/r/{case_id}/{version}/confirm")
    async def confirm(case_id: str, version: int):
        return await api.confirm_report(ACTOR, case_id, version)

    @app.post("/r/{case_id}/facts")
    async def facts(case_id: str, body: dict[str, object]):
        return await api.add_documents(ACTOR, case_id, [], body)

    @app.get("/r/{case_id}/{version}/claim")
    async def claim(case_id: str, version: int):
        return await api.get_claim(ACTOR, case_id, version)

    return app


class HttpStatusTest(ServiceBase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(_app(), raise_server_exceptions=False)

    def check(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        error = response.json()["error"]
        self.assertEqual((error["code"], error["retryable"]), (code, False))
        self.assertTrue(error["message"])

    def test_status_codes(self):
        self.ready()  # v1, v2
        self.check(self.client.get("/r/없는-사건/1"), 404, "not_found")
        self.check(self.client.get(f"/r/{CASE}/2", params={"actor": OTHER}), 403, "forbidden")
        self.check(self.client.post(f"/r/{CASE}/1/confirm"), 409, "version_conflict")
        self.check(
            self.client.post(f"/r/{CASE}/facts", json={"음주": "yes"}), 422, "invalid_case_facts"
        )
        self.check(self.client.get(f"/r/{CASE}/2/claim"), 404, "not_found")

    def test_confirm_blocked_is_422(self):
        self.setUp()
        self.submit("deferred")
        self.check(self.client.post(f"/r/{CASE}/1/confirm"), 422, "confirm_blocked")

    def test_success_still_200(self):
        self.ready()
        self.assertEqual(self.client.get(f"/r/{CASE}/2").status_code, 200)
        self.assertEqual(self.client.post(f"/r/{CASE}/2/confirm").status_code, 200)
        self.assertEqual(self.client.get(f"/r/{CASE}/2/claim").status_code, 200)

    def test_api_exception_type(self):
        with self.assertRaises(ApiException):
            run(api.get_report(ACTOR, "없는-사건", 1))
