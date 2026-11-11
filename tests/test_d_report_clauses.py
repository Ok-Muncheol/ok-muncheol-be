"""D: 검토 보고서 8·9번 약관 근거 조항(app/reports/clauses.py, #99).

- 조항 선택 함수: 8번 제24조, 9번 제34조·제24조 ④·상법 제682조(문구만) + 별표3(사고일로 하나)·별표2,
  제10조는 쓰지 않음
- 가짜 행(tmp db_import) → 실제 fetch_by_parent·validate_hits·로컬 EvidenceStore로 붙는 경우
- 붙지 않는 경우(조회 없음·색인 미연결·EvidenceStore 미등록·계약 약관 판이 코퍼스에 없음·
  조항 하나 없음·검증 실패)에도 보고서 생성
- 청구서 근거서에는 이름만, rule_notice 문구
- 실제 코퍼스(있을 때만)로 8·9번에 조항이 나오는지
LLM·네트워크 호출 없음.
"""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.api import reports as api
from app.calculation.exclusions import flag_judge
from app.calculation.rules import RuleSet
from app.reports import clauses as cl
from app.retrieval.common import configure_retrieval, fetch_by_parent
from app.retrieval.evidence_store import LocalCorpusEvidenceStore
from app.retrieval.hybrid import RetrievalOptions
from app.retrieval.keyword import SimpleTokenizer
from app.retrieval.loader import TABLES, build_index, read_corpus
from app.tools import _evidence
from tests import fixtures_d as fx
from tests.evidence_d import has_corpus

ACTOR, CASE = "handler-1", "synthetic-case"
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}
T = cl.TERMS
A24, A34, A10 = (f"{T}:general_article:{n}" for n in ("24:p68", "34:p76", "10:p61"))
S3_OLD = f"{T}:payment_standard:별표3:1:until-2022-12-31"
S3_NEW = f"{T}:payment_standard:별표3:1:from-2023-01-01"
S2 = f"{T}:payment_standard:별표2:1"
N24, N24_4 = "보통약관 제24조(지급보험금의 계산)", (
    "보통약관 제24조 ④(자기차량손해 지급 후 상대방에 대한 권리 취득)"
)
N34, N682 = "보통약관 제34조(보험회사의 대위)", "상법 제682조(제3자에 대한 보험대위)"
N_S3_NEW, N_S2 = "별표3 1. 과실상계(2023년 1월 1일 이후 사고)", "별표2 1. 수리비용"
HASH = "a" * 64
EDITION = "2026.11.11"  # 가짜 행·실제 코퍼스의 약관 판


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 선택 함수


class SelectTest(unittest.TestCase):
    def ids(self, refs):
        return [r.parent_id for r in refs]

    def test_sections_and_order(self):
        sel = cl.select_report_clauses("2026-10-20")
        self.assertEqual([(r.name, r.parent_id) for r in sel.payout], [(N24, A24)])
        self.assertEqual(
            [(r.name, r.parent_id) for r in sel.recovery_main],
            [(N34, A34), (N24_4, A24), (N682, None)],
        )
        self.assertEqual(self.ids(sel.recovery_reference), [S3_NEW, S2])
        # 청구서 이름 목록에는 제34조·상법 제682조만(제24조 ④는 넣지 않음)
        self.assertEqual([r.name for r in sel.all() if r.in_claim], [N34, N682])

    def test_schedule_3_follows_accident_date(self):
        for day, expected in (
            ("2022-12-31", [S3_OLD, S2]),
            ("2023-01-01", [S3_NEW, S2]),
            ("2023-01-01T09:00:00+09:00", [S3_NEW, S2]),
            (None, [S2]),
            ("", [S2]),
            ("사고일 미상", [S2]),
            ("2023-13-40", [S2]),
        ):
            with self.subTest(day=day):
                self.assertEqual(
                    self.ids(cl.select_report_clauses(day).recovery_reference), expected
                )

    def test_article_10_is_never_selected(self):
        for day in ("2022-01-01", "2026-10-20", None):
            ids = {r.parent_id for r in cl.select_report_clauses(day).all()}
            self.assertNotIn(A10, ids)
            self.assertFalse(any(":general_article:10:" in str(i) for i in ids))

    def test_notice(self):
        self.assertEqual(cl.clause_notice(True, False), "예시 규칙 기준 — 약관 검수 전 초안")
        self.assertEqual(
            cl.clause_notice(True, True), "예시 규칙 기준 — 약관 조항 원문 연결(실무 검수 전) 초안"
        )
        self.assertEqual(cl.clause_notice(False, True), "약관 조항 원문 연결")
        self.assertEqual(cl.clause_notice(False, False), "약관 조항 연결 안 됨")


# ------------------------------------------------------------------ 가짜 행


def _doc():
    return {
        "corpus": "policy_terms", "document_id": T,
        "source_uri": f"corpus-sources/policy_terms/{T}.pdf", "source_name": f"{T}.pdf",
        "edition": EDITION, "effective_from": "2026-11-11", "effective_to": None,
        "effective_period_status": "assumed", "content_hash": HASH, "parser_version": "p1",
        "usage_review": {"status": "permission_required"}, "reviewed_by": None,
        "storage_sha256": HASH, "storage_bytes": 10, "storage_relation": "original",
    }


def _chunk(chunk_id, parent_id, page, text, metadata):
    return {
        "corpus": "policy_terms", "document_id": T, "chunk_id": chunk_id, "parent_id": parent_id,
        "page_start": page, "page_end": page, "text": text, "metadata": metadata,
        "raw_text": text, "display_text": None, "embedding_model": "m",
        "embedding_dimensions": 2, "embedding_version": "v",
        "embedding_content_hash": "c" * 64, "embedding": [1.0, 0.0],
    }


def _parent(parent_id, kind, pages, **body):
    return {
        "corpus": "policy_terms", "kind": kind, "record_id": parent_id, "parent_id": parent_id,
        "is_parent": True, "document_id": T, "chart_id": None,
        "page_start": pages[0], "page_end": pages[1],
        "body": {"parent_id": parent_id, "document": {"document_id": T},
                 "page_start": pages[0], "page_end": pages[1], **body},
    }


def fake_rows():
    """실제와 같은 parent ID의 약관 조항·별표(합성 문구). 제10조도 넣어 쓰이지 않는지 본다."""
    def clause_meta(pid):
        return {"clause_id": pid, "coverage": "own_damage", "clause_type": "general_article"}

    s3 = {"schedule": "별표3", "clause_type": "payment_standard"}
    chunks = [
        _chunk("c24a", A24, 68, "(합성) 제24조(지급보험금의 계산) 자기차량손해 지급보험금",
               clause_meta(A24)),
        _chunk("c24b", A24, 69, "(합성) 비용. 4. 「대물배상」 책임이 발생하는 사고 시 권리 취득",
               clause_meta(A24)),
        _chunk("c34", A34, 76, "(합성) 제34조(보험회사의 대위) 제3자에 대한 권리 취득",
               clause_meta(A34)),
        _chunk("c10", A10, 61, "(합성) 제10조 대인배상Ⅱ·대물배상 지급보험금", clause_meta(A10)),
        _chunk("s3o", S3_OLD, 103, "(합성) 과실상계 이전 사고",
               {**s3, "accident_date_to": "2022-12-31"}),
        _chunk("s3n", S3_NEW, 103, "(합성) 과실상계 이후 사고",
               {**s3, "accident_date_from": "2023-01-01"}),
        _chunk("s2", S2, 100, "(합성) 대물배상 지급 기준 수리비용",
               {"schedule": "별표2", "clause_type": "payment_standard"}),
    ]
    records = [
        *[
            _parent(pid, "clause", pages, clause_id=pid, coverage="own_damage",
                    side_note_parent_ids=[])
            for pid, pages in ((A24, (68, 71)), (A34, (76, 76)), (A10, (61, 61)))
        ],
        *[
            _parent(pid, "appendix", pages, schedule=schedule)
            for pid, pages, schedule in (
                (S3_OLD, (103, 103), "별표3"), (S3_NEW, (103, 103), "별표3"),
                (S2, (100, 100), "별표2"),
            )
        ],
    ]
    pages = [{"document_id": T, "page": p, "content_hash": HASH}
             for p in (61, 68, 69, 70, 71, 76, 100, 103)]
    return {"corpus_documents": [_doc()], "corpus_chunks": chunks,
            "corpus_records": records, "corpus_source_pages": pages}


class ReportFlowCase(unittest.TestCase):
    rules = RuleSet()  # 기본(예시) 규칙

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()) / "db_import"
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base.parent)
        self.rows = fake_rows()
        self.connect()

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )
        _evidence.configure_evidence_store(None)
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(), fetch_clauses=None,
                      judge=flag_judge)

    def connect(self, *, drop_parent=None, fetch=fetch_by_parent, store=True):
        rows = {k: list(v) for k, v in self.rows.items()}
        if drop_parent:
            rows["corpus_chunks"] = [
                c for c in rows["corpus_chunks"] if c["parent_id"] != drop_parent
            ]
        for name in TABLES:
            text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows[name])
            (self.base / f"{name}.jsonl").write_text(text + "\n", encoding="utf-8")
        corpus = read_corpus(self.base)
        configure_retrieval(index=build_index(corpus, tokenizer=SimpleTokenizer()),
                            default_terms_edition=corpus.terms_edition())
        local = LocalCorpusEvidenceStore(corpus)
        _evidence.configure_evidence_store((lambda: local) if store else None)
        api.configure(repo=api.InMemoryReportRepository(), rules=self.rules, fetch_clauses=fetch,
                      judge=flag_judge)

    def flow(self, accident_date=None, terms_edition=EDITION):
        """판단 → 계약(약관 판)·견적·증빙 → 확인 완료. 견본 계약의 판은 코퍼스 판으로 바꾼다."""
        card, judgment, contract = fx.card(), fx.judgment(), fx.contract()
        if accident_date is not None:
            # 보험기간도 사고일에 맞춘다(기간 밖이면 지급 불가라 9번이 빔)
            card["accident_date"] = accident_date
            contract["period_start"], contract["period_end"] = accident_date, accident_date
        contract["terms_edition"] = terms_edition
        run(api.submit_judgment(ACTOR, card, judgment))
        report = run(api.add_documents(
            ACTOR, CASE, [contract, fx.estimate(), *fx.evidence()], NO_FLAGS
        ))
        assert report is not None
        out = run(api.confirm_report(ACTOR, CASE, report["versions"]["report_version"],
                                     counterparty=fx.counterparty()))
        return report, out["package"]

    def section(self, report, no):
        return next(s for s in report["sections"] if s["no"] == no)

    def names(self, rows):
        return [r if isinstance(r, str) else r["조항"] for r in rows]


class AttachedTest(ReportFlowCase):
    def test_section_8_has_article_24(self):
        report, _ = self.flow()
        s8 = self.section(report, 8)
        rows = s8["body"]["근거 조항"]
        self.assertEqual(self.names(rows), [N24])
        self.assertEqual(
            (rows[0]["ID"], rows[0]["적용판"], rows[0]["페이지"]), (A24, "2026.11.11", "68–69")
        )
        self.assertTrue(rows[0]["발췌"].startswith("(합성) 제24조"))
        self.assertEqual(s8["body"]["근거 상태"], cl.EXAMPLE_NOTICE_WITH_CLAUSES)
        self.assertIn(A24, s8["evidence_ids"])

    def test_section_9_main_and_reference(self):
        report, _ = self.flow()
        s9 = self.section(report, 9)
        basis = s9["body"]["근거 조항"]
        main = basis["주 근거"]
        self.assertEqual(self.names(main), [N34, N24_4, N682])
        self.assertEqual((main[0]["ID"], main[0]["페이지"]), (A34, "76"))
        # 제24조 ④는 그 문구가 있는 청크에서 발췌
        self.assertEqual(main[1]["페이지"], "69")
        self.assertTrue(main[1]["발췌"].startswith("「대물배상」 책임이 발생하는 사고"))
        self.assertEqual(main[2], {"조항": N682, "출처": cl.TEXT_ONLY_SOURCE})
        self.assertEqual(self.names(basis["참고 근거"]), [N_S3_NEW, N_S2])
        self.assertTrue({A34, A24, S3_NEW, S2} <= set(s9["evidence_ids"]))
        self.assertNotIn(S3_OLD, s9["evidence_ids"])
        all_ids = {i for s in report["sections"] for i in s["evidence_ids"]}
        self.assertNotIn(A10, all_ids)

    def test_old_accident_picks_old_schedule_3(self):
        report, _ = self.flow(accident_date="2022-06-01")
        s9 = self.section(report, 9)
        self.assertEqual(
            [r["ID"] for r in s9["body"]["근거 조항"]["참고 근거"]], [S3_OLD, S2]
        )

    def test_claim_package_has_names_only(self):
        _, pkg = self.flow()
        self.assertEqual(pkg["basis"]["근거 조항"], [N34, N682])
        self.assertEqual(pkg["rule_notice"], cl.EXAMPLE_NOTICE_WITH_CLAUSES)
        text = json.dumps(pkg, ensure_ascii=False)
        # 발췌·페이지·산정 근거(제24조·별표)는 넣지 않는다(결정례 발췌는 원래 있음)
        for absent in ("example:subrogation", A24, A34, S3_NEW, S2, "별표", "제24조", "(합성) 제"):
            self.assertNotIn(absent, text)


class ReviewedRulesTest(ReportFlowCase):
    rules = RuleSet(version="reviewed-test")

    def test_reviewed_rules_notice(self):
        report, pkg = self.flow()
        self.assertEqual(self.section(report, 8)["body"]["근거 상태"], "약관 조항 원문 연결")
        self.assertIsNone(pkg["rule_notice"])
        self.assertEqual(pkg["basis"]["근거 조항"], [N34, N682])


class NotAttachedTest(ReportFlowCase):
    def assert_not_connected(self, report, pkg):
        s8, s9 = self.section(report, 8), self.section(report, 9)
        self.assertEqual(s8["state"], "filled")  # 보고서는 그대로 나온다
        self.assertEqual(s8["body"]["근거 조항"], cl.NOT_CONNECTED)
        self.assertEqual(s8["body"]["근거 상태"], cl.EXAMPLE_NOTICE)
        self.assertEqual(s9["body"]["근거 조항"], cl.NOT_CONNECTED)
        self.assertEqual(s9["body"]["법령 근거"], [{"조항": N682, "출처": cl.TEXT_ONLY_SOURCE}])
        ids = {i for s in report["sections"] for i in s["evidence_ids"]}
        self.assertFalse(ids & {A24, A34, S3_NEW, S2})
        self.assertEqual(pkg["basis"]["근거 조항"], [N682])
        self.assertEqual(pkg["rule_notice"], cl.EXAMPLE_NOTICE)

    def test_no_clause_lookup(self):
        api.configure(fetch_clauses=None)
        self.assert_not_connected(*self.flow())

    def test_contract_terms_edition_not_in_corpus(self):
        self.assert_not_connected(*self.flow(terms_edition="다른-판"))

    def test_index_not_configured(self):
        configure_retrieval(index=None, default_terms_edition=None)  # fetch_by_parent → 미연결
        self.assert_not_connected(*self.flow())

    def test_evidence_store_not_registered(self):
        self.connect(store=False)  # validate_hits → RuntimeError(fail-closed)
        self.assert_not_connected(*self.flow())

    def test_one_missing_clause_leaves_the_rest(self):
        self.connect(drop_parent=A34)
        report, pkg = self.flow()
        s9 = self.section(report, 9)
        self.assertEqual(self.names(s9["body"]["근거 조항"]["주 근거"]), [N24_4, N682])
        self.assertNotIn(A34, s9["evidence_ids"])
        self.assertEqual(pkg["basis"]["근거 조항"], [N682])  # 제34조가 없으면 법령 이름만
        self.assertEqual(pkg["rule_notice"], cl.EXAMPLE_NOTICE_WITH_CLAUSES)

    def test_main_group_without_corpus_clause_says_not_connected(self):
        self.rows["corpus_chunks"] = [
            c for c in self.rows["corpus_chunks"] if c["parent_id"] not in (A34, A24)
        ]
        self.connect()
        report, _ = self.flow()
        s8, s9 = self.section(report, 8), self.section(report, 9)
        self.assertEqual(s8["body"]["근거 조항"], cl.NOT_CONNECTED)
        main = s9["body"]["근거 조항"]["주 근거"]
        self.assertEqual(main[0], cl.NOT_CONNECTED)
        self.assertEqual(self.names(main[1:]), [N682])
        self.assertEqual(self.names(s9["body"]["근거 조항"]["참고 근거"]), [N_S3_NEW, N_S2])

    def test_validation_failure_skips_only_that_clause(self):
        async def tampered(corpus, parent_ids, **kw):
            hits = await fetch_by_parent(corpus, parent_ids, **kw)
            if parent_ids == [S2]:  # 원문과 다른 발췌 → validate_hits ValueError
                return [{**h, "chunk": {**h["chunk"], "text": "바뀐 문구"}} for h in hits]
            return hits

        self.connect(fetch=tampered)
        report, _ = self.flow()
        reference = self.section(report, 9)["body"]["근거 조항"]["참고 근거"]
        self.assertEqual(self.names(reference), [N_S3_NEW])

    def test_code_bug_is_not_wrapped(self):
        async def broken(corpus, parent_ids, **kw):
            if parent_ids == [A24]:
                raise KeyError("코드 버그")
            return await fetch_by_parent(corpus, parent_ids, **kw)

        self.connect(fetch=broken)
        with self.assertRaises(KeyError):
            self.flow()


# ------------------------------------------------------------------ 실제 코퍼스


@unittest.skipUnless(has_corpus(), "data/private/corpus-db-import/db_import 파일이 없다")
class RealCorpusTest(ReportFlowCase):
    @classmethod
    def setUpClass(cls):
        corpus = read_corpus()
        cls.index = build_index(corpus, ["policy_terms"])
        cls.edition = corpus.terms_edition()
        cls.store = LocalCorpusEvidenceStore(corpus)

    def setUp(self):
        configure_retrieval(index=self.index, default_terms_edition=self.edition)
        _evidence.configure_evidence_store(lambda: self.store)
        api.configure(repo=api.InMemoryReportRepository(), rules=RuleSet(),
                      fetch_clauses=fetch_by_parent, judge=flag_judge)

    def test_sections_8_and_9_cite_real_clauses(self):
        report, pkg = self.flow()  # 견본 사고일 2026-10-20 → 별표3 2023-01-01 이후
        s8, s9 = self.section(report, 8), self.section(report, 9)
        (r24,) = s8["body"]["근거 조항"]
        self.assertEqual((r24["조항"], r24["ID"], r24["페이지"]), (N24, A24, "68–71"))
        self.assertIn("제24조", r24["발췌"])
        main = s9["body"]["근거 조항"]["주 근거"]
        self.assertEqual(self.names(main), [N34, N24_4, N682])
        self.assertEqual((main[0]["ID"], main[0]["페이지"]), (A34, "76"))
        self.assertIn("보험회사의 대위", main[0]["발췌"])
        self.assertEqual(main[1]["페이지"], "69")
        self.assertIn("「대물배상」 책임이 발생하는 사고", main[1]["발췌"])
        reference = s9["body"]["근거 조항"]["참고 근거"]
        self.assertEqual([r["ID"] for r in reference], [S3_NEW, S2])
        self.assertEqual([r["페이지"] for r in reference], ["103", "100"])
        self.assertEqual(s8["body"]["근거 상태"], cl.EXAMPLE_NOTICE_WITH_CLAUSES)
        self.assertEqual(pkg["basis"]["근거 조항"], [N34, N682])

    def test_old_accident_uses_old_schedule_3(self):
        report, _ = self.flow(accident_date="2022-06-01")
        reference = self.section(report, 9)["body"]["근거 조항"]["참고 근거"]
        self.assertEqual([r["ID"] for r in reference], [S3_OLD, S2])


if __name__ == "__main__":
    unittest.main()
