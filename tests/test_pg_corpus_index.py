"""B(통합): DB 색인(pgvector)의 적재·검색·근거 저장소를 실제 Postgres로 검사한다.

`MIGRATION_TEST_DATABASE_URL`(버려도 되는 로컬·CI Postgres, pgvector 포함)과 psql이 있을 때만 돈다.
임시 데이터베이스에 supabase_stub.sql → 모든 마이그레이션을 적용하고 합성 코퍼스를 한 판으로 넣는다.
기준은 메모리 색인(`InMemoryIndex`)과 로컬 근거 저장소(`LocalCorpusEvidenceStore`)다.
같은 행으로 만든 두 구현의 결과가 같아야 한다. 외부 API는 부르지 않는다(가짜 임베더).
"""

import asyncio
import dataclasses
import json
import shutil
import uuid
from array import array
from datetime import date
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from app import db
from app.api import reports as api
from app.calculation.exclusions import flag_judge
from app.config import get_settings
from app.retrieval.common import configure_retrieval
from app.retrieval.embedding import EmbeddingMismatch, FakeEmbedder, RetrievalNotConfigured
from app.retrieval.evidence_store import LocalCorpusEvidenceStore
from app.retrieval.hybrid import RetrievalOptions, retrieve
from app.retrieval.index import SearchFilter
from app.retrieval.keyword import SimpleTokenizer
from app.retrieval.loader import TABLES, CorpusDataError, build_index, read_corpus
from app.retrieval.pg_evidence_store import PgEvidenceStore
from app.retrieval.pg_index import PgCorpusIndex, read_release
from app.retrieval.pg_load import (
    delete_release,
    list_releases,
    load_release,
    verify_release,
)
from app.tools import _evidence
from app.tools.dispute_cases import search_dispute_cases
from app.tools.fault_standards import search_fault_standards
from app.tools.policy_terms import search_policy_terms
from tests.migrations.test_migrations_apply import ADMIN_URL, HERE, MIGRATIONS, _psql
from tests.test_d_corpus_loader import (
    CHART,
    HASH_F,
    TERMS,
    chunk,
    document,
    fake_rows,
    parent_body,
    record,
)

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or shutil.which("psql") is None,
    reason="MIGRATION_TEST_DATABASE_URL과 psql이 있어야 한다(supabase/migrations/README.md)",
)

DIMS = 1536
EMBEDDER = FakeEmbedder(model="m", dimensions=DIMS, version="v")
RELEASE = "test-1"
OTHER = "other-9"  # 같은 DB에 함께 있는 다른 판. 읽는 판의 결과에 섞이면 안 된다
OLD, CASES = "fault-0", "cases-1"
HASH_O, HASH_D = "d" * 64, "e" * 64
SUMS = {name: "f" * 64 for name in TABLES}


def run(coro):
    return asyncio.run(coro)


def embed(text: str) -> list[float]:
    """가짜 임베더 벡터를 단정밀도로 맞춘다.

    pgvector가 단정밀도로 저장하므로, 이렇게 해야 두 색인이 같은 값을 쓴다.
    """
    return array("f", run(EMBEDDER.embed(text))).tolist()


def corpus_tables() -> dict[str, list[dict]]:
    """tests/test_d_corpus_loader.fake_rows에 도표 2개·구판 문서·결정례·약관 조항을 더한 합성 행.

    C의 검증 규칙(validate_hits)을 만족한다. chunk_id에 대문자·밑줄·한글을 섞어 정렬 규칙도 본다.
    """
    tables = fake_rows()
    chart1, chart2 = f"{CHART}:차1-1", f"{CHART}:차2-1"
    old_chart = f"{OLD}:구1-1"
    case1, case2 = f"{CASES}:2019-1", f"{CASES}:2020-7"
    rider = f"{TERMS}:rider:5:p1"
    same = "신호 없는 교차로 동일폭 직진 좌회전 맞은편"
    chart_meta = {"section": "chart", "chart_id": "차2-1", "related_parent_ids": [chart1]}
    shared = {**chart_meta, "shared_source": True}
    tables["corpus_documents"] += [
        {**document(OLD, "fault_standards", HASH_O, edition="2019.1", effective_from="2019-01-01"),
         "effective_to": "2023-06-29"},
        {**document(CASES, "dispute_cases", HASH_D, edition="unconfirmed"),
         "published_month": "2021-03"},
    ]
    tables["corpus_chunks"] += [
        chunk("f2", "fault_standards", CHART, chart1,
              "신호 교차로 녹색 직진 대 적색 직진 기본 과실",
              {"section": "chart", "chart_id": "차1-1"}),
        chunk("f3", "fault_standards", CHART, chart1, "수정요소 과속 서행 불이행 현저한 과실",
              {"section": "chart", "chart_id": "차1-1"}, page=2),
        # 같은 조항의 2쪽 청크를 1쪽 청크보다 먼저 넣는다(조항 조회는 적재 순서가 아니라 페이지 순)
        chunk("차-한글", "fault_standards", CHART, chart2,
              "신호 없는 교차로 대로 소로 직진 측면 선진입",
              {**chart_meta, "shared_source": False}, page=2),
        chunk("a_1", "fault_standards", CHART, chart2, same, shared),
        chunk("F-10", "fault_standards", CHART, chart2, same, shared),
        chunk("g1", "fault_standards", CHART, f"{CHART}:modifier-guide:1",
              "공통 수정요소 해설 현저한 과실 중대한 과실", {"section": "common_modifier_guide"}),
        chunk("o1", "fault_standards", OLD, old_chart, "교차로 녹색 직진 적색 직진 구판",
              {"section": "chart", "chart_id": "구1-1"}),
        chunk("d1", "dispute_cases", CASES, case1, "신호 교차로 직진 차량 충돌 결정례",
              {"case_id": "2019-1", "chart_id": "차1-1", "retrieval_role": "card_similarity",
               "related_chart_ids": ["차1-1", "차2-1"], "source_header_context": None}),
        chunk("d2", "dispute_cases", CASES, case1, "신호 없는 교차로 좌회전 직진 충돌 결정 이유",
              {"case_id": "2019-1", "chart_id": "차1-1", "retrieval_role": "supporting_evidence",
               "related_chart_ids": ["차1-1"], "source_header_context": "머리말"}, page=2),
        chunk("d3", "dispute_cases", CASES, case2, "주차장 후진 추돌 결정례",
              {"case_id": "2020-7", "chart_id": "차2-1", "retrieval_role": "card_similarity",
               "related_chart_ids": [], "source_header_context": None}),
        chunk("t4", "policy_terms", TERMS, rider, "자기차량손해 자기부담금 조항",
              {"clause_id": rider, "coverage": "own_damage", "retrieval_role": "primary_clause",
               "coverage_mentions": ["own_damage", "liability"], "page_hint": 7, "ratio": 0.5}),
    ]
    tables["corpus_records"] += [
        record("fault_standards", "chart", "차2-1",
               parent_body(chart2, CHART, chart_id="차2-1", modifier_ids=[]),
               parent_id=chart2, is_parent=True, document_id=CHART),
        record("fault_standards", "chart", "구1-1",
               parent_body(old_chart, OLD, chart_id="구1-1", modifier_ids=[]),
               parent_id=old_chart, is_parent=True, document_id=OLD),
        *[record("dispute_cases", "case", pid,
                 parent_body(pid, CASES, case_id=pid.rsplit(":", 1)[1], mapping_evidence=[]),
                 parent_id=pid, is_parent=True, document_id=CASES) for pid in (case1, case2)],
        record("policy_terms", "clause", rider,
               parent_body(rider, TERMS, clause_id=rider, coverage="own_damage",
                           side_note_parent_ids=[]),
               parent_id=rider, is_parent=True, document_id=TERMS),
        *[record("policy_terms", "schedule_value", f"{TERMS}:별표4:base:{n}",
                 {"record_id": f"{TERMS}:별표4:base:{n}", "value": value, "items": [n, "가"],
                  "calculation_ready": False, "document": {"document_id": TERMS}},
                 document_id=TERMS) for n, value in ((2, 0.2), (1, 30))],
    ]
    tables["corpus_source_pages"] += [
        {"document_id": doc_id, "page": page, "content_hash": content_hash}
        for doc_id, content_hash in ((OLD, HASH_O), (CASES, HASH_D))
        for page in (1, 2)
    ]
    for row in tables["corpus_chunks"]:
        row["embedding"] = embed(row["text"])
        row["embedding_dimensions"] = DIMS
    return tables


def other_release(corpus):
    """같은 ID에 글·벡터·레코드·페이지 해시가 다른 판. 판 조건이 빠지면 결과가 달라진다."""
    texts = ["다른 판 " + row["text"] for row in corpus.chunks]
    chunks = [{**row, "text": text, "embedding": embed(text)}
              for row, text in zip(corpus.chunks, texts, strict=True)]
    records = [{**row, "body": {**row["body"], "다른 판": True}} for row in corpus.records]
    pages = [*corpus.pages, {"document_id": CHART, "page": 99, "content_hash": HASH_F}]
    return dataclasses.replace(corpus, chunks=chunks, records=records, pages=pages)


def _temp_database(migrations=MIGRATIONS) -> tuple[str, str]:
    parts = urlsplit(ADMIN_URL)
    if "supabase" in (parts.hostname or ""):
        pytest.fail("버려도 되는 로컬 Postgres 주소만 쓴다(Supabase 주소 금지)")
    name = f"okm_corpus_{uuid.uuid4().hex[:12]}"
    created = _psql(ADMIN_URL, "-c", f'create database "{name}"')
    assert created.returncode == 0, created.stderr
    url = urlunsplit(parts._replace(path=f"/{name}"))
    assert _psql(url, "-f", str(HERE / "supabase_stub.sql")).returncode == 0
    for migration in migrations:
        applied = _psql(url, "--single-transaction", "-f", str(migration))
        assert applied.returncode == 0, f"{migration.name}\n{applied.stderr}"
    return name, url


def _drop(name: str) -> None:
    _psql(ADMIN_URL, "-c", f'drop database if exists "{name}" with (force)')


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """합성 행을 파일로 쓰고 실제 로더(`read_corpus`)로 읽는다. 적재가 받는 것과 같은 모양이다."""
    base = tmp_path_factory.mktemp("corpus") / "db_import"
    base.mkdir()
    for name, rows in corpus_tables().items():
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        (base / f"{name}.jsonl").write_text(text + "\n", encoding="utf-8")
    return read_corpus(base)


@pytest.fixture(scope="module")
def database_url(corpus):
    name, url = _temp_database()
    try:
        with psycopg.connect(url) as conn:
            load_release(conn, RELEASE, corpus, source_sha256=SUMS,
                         tokenizers=[SimpleTokenizer()], note="합성")
            conn.commit()
            load_release(conn, OTHER, other_release(corpus), source_sha256=SUMS,
                         tokenizers=[SimpleTokenizer()])
            conn.commit()
        yield url
    finally:
        _drop(name)


@pytest.fixture(scope="module")
def release(database_url):
    return read_release(RELEASE, database_url)


@pytest.fixture(scope="module")
def memory(corpus):
    return build_index(corpus, tokenizer=SimpleTokenizer())


@pytest.fixture
def pg(release, database_url):
    return PgCorpusIndex(release, connect=lambda: db.connect(database_url),
                         tokenizer=SimpleTokenizer())


class Counting:
    """연결을 몇 번 열었는지 센다."""

    def __init__(self, url: str) -> None:
        self.url, self.calls = url, 0

    def __call__(self):
        self.calls += 1
        return db.connect(self.url)


FILTERS = [
    ("fault_standards", {}),
    ("fault_standards", {"accident_date": date(2020, 1, 1)}),  # 구판 적용 기간 안
    ("fault_standards", {"accident_date": date(2023, 6, 29)}),  # 끝 날짜 포함
    ("fault_standards", {"accident_date": date(2023, 6, 30)}),  # 구판은 빠지고 기간 모름은 통과
    ("fault_standards", {"accident_date": date(2018, 12, 31)}),
    ("fault_standards", {"edition": "2019.1"}),
    ("fault_standards", {"edition": "없는 판"}),
    ("fault_standards", {"document_id": CHART}),
    ("fault_standards", {"metadata": {"chart_id": "차2-1"}}),
    ("fault_standards", {"metadata": {"chart_id": ["차1-1", "구1-1"]}}),
    ("fault_standards", {"metadata": {"shared_source": True}}),
    ("fault_standards", {"metadata": {"shared_source": False}}),
    ("fault_standards", {"metadata": {"related_parent_ids": f"{CHART}:차1-1"}}),
    ("fault_standards", {"metadata": {"section": "chart", "chart_id": "차1-1"}}),
    ("fault_standards", {"metadata": {"없는 키": "x"}}),
    ("fault_standards", {"metadata": {"chart_id": []}}),
    ("dispute_cases", {}),
    ("dispute_cases", {"accident_window": date(2020, 1, 1)}),
    ("dispute_cases", {"metadata": {"retrieval_role": "card_similarity"}}),
    ("dispute_cases", {"metadata": {"related_chart_ids": "차2-1"}}),
    ("dispute_cases", {"metadata": {"related_chart_ids": ["차2-1", "없음"]}}),
    ("dispute_cases", {"metadata": {"related_chart_ids": []}}),
    ("dispute_cases", {"metadata": {"source_header_context": None}}),
    ("dispute_cases", {"metadata": {"source_header_context": [None, "머리말"]}}),
    ("dispute_cases",
     {"metadata": {"chart_id": "차1-1", "retrieval_role": ["supporting_evidence"]}}),
    ("policy_terms", {}),
    ("policy_terms", {"edition": "판-1"}),
    ("policy_terms", {"edition": "판-2"}),
    ("policy_terms", {"accident_window": date(2022, 12, 31)}),
    ("policy_terms", {"accident_window": date(2023, 1, 1)}),
    ("policy_terms", {"metadata": {"schedule": "별표3"}, "accident_window": date(2024, 5, 1)}),
    ("policy_terms", {"metadata": {"coverage": None}}),
    ("policy_terms", {"metadata": {"coverage": "own_damage"}}),
    ("policy_terms", {"metadata": {"coverage_mentions": "liability"}}),
    ("policy_terms", {"metadata": {"page_hint": 7}}),
    ("policy_terms", {"metadata": {"page_hint": "7"}}),  # 숫자와 글자열은 다르다
    ("policy_terms", {"metadata": {"ratio": 0.5}}),
    ("policy_terms", {"metadata": {"coverage_mentions": ("own_damage",)}}),  # 튜플은 같을 수 없다
]
QUERIES = [
    "교차로 녹색 직진 적색 직진",
    "신호 없는 교차로 동일폭 직진 좌회전 맞은편",
    "수정요소 과속 현저한 과실",
    "주차장 후진 추돌",
    "자기차량손해 자기부담금 보상하지 않는 손해",
    "과실상계 별표3 사고",
    "abc 123 없는 낱말",
]


def assert_same(expected, found, *, tolerance):
    assert [c["chunk_id"] for c, _ in found] == [c["chunk_id"] for c, _ in expected]
    for (chunk_a, score_a), (chunk_b, score_b) in zip(expected, found, strict=True):
        assert chunk_b == chunk_a
        assert score_b == pytest.approx(score_a, abs=tolerance)


def test_release_head_matches_the_loaded_rows(release, corpus):
    assert release.name == RELEASE
    assert (release.spec.model, release.spec.dimensions, release.spec.version) == ("m", DIMS, "v")
    assert release.keyword_tokenizers == {"simple"}
    assert set(release.documents) == set(corpus.documents)
    assert release.terms_edition() == "판-1"
    assert dict(release.source_sha256) == SUMS
    assert release.row_counts["corpus_chunks"] == len(corpus.chunks)
    # 약관 수치 레코드는 적재 순서 그대로(파일 순서), 숫자 타입도 그대로다
    expected = tuple(r["body"] for r in corpus.records if r["kind"] == "schedule_value")
    assert release.schedule_values == expected
    assert [v["value"] for v in release.schedule_values] == [0.2, 30]


@pytest.mark.parametrize(("corpus_name", "spec"), FILTERS, ids=lambda v: str(v)[:60])
def test_search_matches_the_memory_index(pg, memory, corpus_name, spec):
    """필터마다 벡터·키워드 검색 결과(청크·순서·점수)가 메모리 색인과 같다."""
    search_filter = SearchFilter(corpus=corpus_name, **spec)
    matched = 0
    for text in QUERIES:
        vector = embed(text)
        for limit in (2, 50):
            expected = run(memory.search_vector(search_filter, vector, limit))
            assert_same(expected, run(pg.search_vector(search_filter, vector, limit)),
                        tolerance=1e-6)
            expected = run(memory.search_keyword(search_filter, text, limit))
            assert_same(expected, run(pg.search_keyword(search_filter, text, limit)),
                        tolerance=1e-12)
            matched += len(expected)
    empty = {"없는 판", "판-2", "없는 키"} & {str(spec.get("edition")), *spec.get("metadata", {})}
    if not empty and spec.get("metadata") not in (
            {"chart_id": []}, {"related_chart_ids": []}, {"page_hint": "7"},
            {"coverage_mentions": ("own_damage",)}):
        assert matched, "대조군: 이 필터는 결과가 있어야 한다"


def test_ties_break_by_chunk_id_in_code_point_order(pg, memory):
    """같은 글·같은 벡터의 두 청크는 chunk_id 순이다.

    대문자가 소문자보다 앞이다(Python 문자열 비교). DB 기본 정렬 규칙(en_US)과 다르다.
    """
    search_filter = SearchFilter(corpus="fault_standards", metadata={"chart_id": "차2-1"})
    text = "신호 없는 교차로 동일폭 직진 좌회전 맞은편"
    for found in (run(pg.search_vector(search_filter, embed(text), 10)),
                  run(pg.search_keyword(search_filter, text, 10))):
        assert [c["chunk_id"] for c, _ in found] == ["F-10", "a_1", "차-한글"]
        assert found[0][1] == found[1][1]
    assert sorted(["a_1", "차-한글", "F-10"]) == ["F-10", "a_1", "차-한글"]


def test_zero_vector_scores_zero_like_the_memory_index(pg, memory):
    search_filter = SearchFilter(corpus="dispute_cases")
    zero = [0.0] * DIMS
    expected = run(memory.search_vector(search_filter, zero, 10))
    assert {score for _, score in expected} == {0.0}
    assert_same(expected, run(pg.search_vector(search_filter, zero, 10)), tolerance=0)


def test_rrf_through_retrieve_matches_the_memory_index(pg, memory):
    for corpus_name, top_k in (("fault_standards", 3), ("fault_standards", 100),
                               ("dispute_cases", 2), ("policy_terms", 10)):
        for text in QUERIES[:6]:
            request = {"corpus": corpus_name, "text": text, "accident_date": "2024-05-01",
                       "filters": {}, "top_k": top_k}
            kwargs = {"options": RetrievalOptions(), "default_terms_edition": "판-1"}
            expected = run(retrieve(request, embed(text), index=memory, **kwargs))
            found = run(retrieve(request, embed(text), index=pg, **kwargs))
            assert [h["chunk"] for h in found] == [h["chunk"] for h in expected]
            assert [h["retrieval_version"] for h in found] == [
                h["retrieval_version"] for h in expected]
            for a, b in zip(expected, found, strict=True):
                assert b["score"] == pytest.approx(a["score"], abs=1e-12)
    assert pg.keyword_version == memory.keyword_version == "bm25-v1:simple"


def test_fetch_by_parent_matches_and_reads_the_corpus_once(release, database_url, memory, corpus):
    connect = Counting(database_url)
    pg = PgCorpusIndex(release, connect=connect, tokenizer=SimpleTokenizer())
    by_corpus: dict[str, list[str]] = {}
    for row in corpus.chunks:
        by_corpus.setdefault(row["corpus"], []).append(row["parent_id"])
    for corpus_name, parents in by_corpus.items():
        for edition in (None, "판-1", "2019.1"):
            for order in (parents, parents[::-1], [parents[-1], "없는 조항", parents[0]]):
                expected = run(memory.fetch_by_parent(corpus_name, order, edition=edition))
                assert run(pg.fetch_by_parent(corpus_name, order, edition=edition)) == expected
    assert connect.calls == len(by_corpus)  # 코퍼스마다 한 번만 읽는다
    # 같은 조항·같은 페이지는 적재 순서(f1 → f2), 다음 페이지가 뒤다
    chart = run(pg.fetch_by_parent("fault_standards", [f"{CHART}:차1-1"]))
    assert [c["chunk_id"] for c in chart] == ["f1", "f2", "f3"]
    # 2쪽 청크를 먼저 넣었어도 1쪽이 앞이고, 같은 1쪽끼리는 적재 순서(a_1 → F-10)다
    other = run(pg.fetch_by_parent("fault_standards", [f"{CHART}:차2-1"]))
    assert [c["chunk_id"] for c in other] == ["a_1", "F-10", "차-한글"]
    chart[0]["metadata"]["chart_id"] = "고친 값"  # 돌려받은 값을 고쳐도 다음 조회는 그대로다
    again = run(pg.fetch_by_parent("fault_standards", [f"{CHART}:차1-1"]))
    assert again[0]["metadata"]["chart_id"] == "차1-1"


def test_query_vector_length_must_match(pg):
    with pytest.raises(EmbeddingMismatch, match="벡터 길이"):
        run(pg.search_vector(SearchFilter(corpus="fault_standards"), [1.0, 0.0], 3))


def test_keyword_needs_the_tokenizer_the_release_was_loaded_with(release, database_url):
    class Other(SimpleTokenizer):
        name = "kiwi"

    pg = PgCorpusIndex(release, connect=lambda: db.connect(database_url), tokenizer=Other())
    with pytest.raises(RetrievalNotConfigured, match="kiwi 토큰화의 키워드 색인이 없습니다"):
        run(pg.search_keyword(SearchFilter(corpus="fault_standards"), "교차로", 3))
    # 벡터 검색과 조항 조회는 토큰화와 상관없이 된다
    assert run(pg.search_vector(SearchFilter(corpus="fault_standards"), embed("교차로"), 1))


def test_keyword_stops_when_the_tokenizer_changed_since_loading(release, database_url):
    """이름은 같은데 낱말을 다르게 나누는 토큰화(코드·모델이 바뀐 경우)는 조용히 쓰지 않는다."""

    class Drifted(SimpleTokenizer):
        def tokenize(self, text: str) -> list[str]:
            return text.lower().split()

    connect = Counting(database_url)
    pg = PgCorpusIndex(release, connect=connect, tokenizer=Drifted())
    with pytest.raises(CorpusDataError, match="낱말 수가 지금 simple 토큰화와"):
        run(pg.search_keyword(SearchFilter(corpus="fault_standards"), "교차로", 3))
    same = PgCorpusIndex(release, connect=connect, tokenizer=SimpleTokenizer())
    connect.calls = 0
    run(same.search_keyword(SearchFilter(corpus="fault_standards"), "교차로", 3))
    run(same.search_keyword(SearchFilter(corpus="fault_standards"), "직진", 3))
    assert connect.calls == 3  # 처음 한 번만 대조하고(1) 검색마다 한 번(2)


def test_evidence_store_matches_the_local_adapter(release, database_url, corpus):
    connect = Counting(database_url)
    store, local = PgEvidenceStore(release, connect=connect), LocalCorpusEvidenceStore(corpus)
    for row in corpus.chunks:
        key = (row["corpus"], row["chunk_id"])
        assert run(store.get_chunk(*key)) == run(local.get_chunk(*key))
    for row in corpus.records:
        key = (row["corpus"], row["kind"], row["record_id"])
        assert run(store.get_related(*key)) == run(local.get_related(*key))
        if row["is_parent"]:
            assert run(store.get_parent(row["corpus"], row["parent_id"])) == row["body"]
    for row in corpus.pages:
        assert run(store.get_source_page(row["document_id"], row["page"])) == {
            "content_hash": row["content_hash"]}
    # 없는 것은 None(코퍼스가 달라도 None)
    assert run(store.get_chunk("policy_terms", "f1")) is None
    assert run(store.get_chunk("없는 코퍼스", "f1")) is None
    assert run(store.get_parent("fault_standards", f"{CHART}:modifier-guide:1")) is None
    assert run(store.get_related("fault_standards", "chart", "차9-9")) is None
    assert run(store.get_source_page(CHART, 99)) is None
    # 코퍼스마다 청크 한 번·레코드 한 번, 페이지 한 번만 DB에 간다(없는 코퍼스 조회 1번 포함)
    assert connect.calls == 3 + 3 + 1 + 1
    # 근거 저장소의 청크는 색인이 돌려준 청크와 다른 객체다
    index = PgCorpusIndex(release, connect=connect, tokenizer=SimpleTokenizer())
    found = run(index.search_keyword(SearchFilter(corpus="fault_standards"), "녹색 직진", 1))[0][0]
    stored = run(store.get_chunk("fault_standards", found["chunk_id"]))
    assert stored == found and stored is not found
    assert stored["metadata"] is not found["metadata"]


@pytest.fixture
def connected(database_url, monkeypatch):
    """운영처럼 연결 함수로 켠다: DATABASE_URL + CORPUS_DB_RELEASE, 질의 임베더만 가짜."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    with patch.object(api.OpenAIEmbedder, "from_env", lambda: EMBEDDER):
        services = api.configure_with_corpus_db(RELEASE)
    yield services
    configure_retrieval(index=None, embedder=None, options=RetrievalOptions(),
                        default_terms_edition=None)
    _evidence.configure_evidence_store(None)
    api.configure(fetch_clauses=None, judge=flag_judge, schedule_values=())
    get_settings.cache_clear()


def _through_files(corpus, call):
    """같은 행을 파일 경로(메모리 색인 + 로컬 근거 저장소)로 연결해 같은 도구를 부른다."""
    local = LocalCorpusEvidenceStore(corpus)
    configure_retrieval(index=build_index(corpus, tokenizer=SimpleTokenizer()), embedder=EMBEDDER,
                        default_terms_edition="판-1")
    _evidence.configure_evidence_store(lambda: local)
    return run(call())


def test_tools_return_validated_hits_from_the_database(connected, corpus):
    """C의 세 도구(요청 검사 → 공통 검색 → validate_hits)가 DB 색인·DB 근거 저장소로 돈다."""
    calls = [
        lambda: search_fault_standards(
            {"text": "교차로 녹색 직진 적색 직진", "accident_date": "2024-05-01", "filters": {},
             "top_k": 100}),
        lambda: search_fault_standards(
            {"text": "수정요소 현저한 과실", "accident_date": "2020-03-01",
             "filters": {"chart_id": "차1-1"}, "top_k": 5}),
        lambda: search_dispute_cases(
            {"text": "교차로 직진 충돌", "accident_date": "", "filters": {"case_id": "2019-1"},
             "top_k": 5}),
        lambda: search_policy_terms(
            {"text": "과실상계", "accident_date": "2024-05-01", "filters": {"schedule": "별표3"},
             "top_k": 5}),
        lambda: search_policy_terms(
            {"text": "자기부담금", "accident_date": "", "filters": {"coverage": "own_damage"},
             "top_k": 5}),
    ]
    found = [run(call()) for call in calls]
    assert [len(hits) for hits in found] == [7, 3, 2, 1, 1]
    assert found[3][0]["chunk"]["chunk_id"] == "t3"  # 2023-01-01 이후 사고의 별표3
    assert found[0][0]["retrieval_version"] == "rrf-v1:k60:m:v:bm25-v1:simple"
    assert connected.schedule_values and connected.fetch_clauses is not None
    clause = run(connected.fetch_clauses("policy_terms", [f"{TERMS}:general_article:23:p1"]))
    assert [hit["chunk"]["chunk_id"] for hit in clause] == ["t1"]
    expected = [_through_files(corpus, call) for call in calls]
    for hits, wanted in zip(found, expected, strict=True):
        assert [h["chunk"] for h in hits] == [h["chunk"] for h in wanted]
        assert [h["retrieval_version"] for h in hits] == [h["retrieval_version"] for h in wanted]


def test_connect_failures_change_nothing(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    api.configure(fetch_clauses=None, judge=flag_judge, schedule_values=())
    cases = [
        ("없는-판", EMBEDDER, CorpusDataError, "DB에 없는 적재판"),
        (RELEASE, FakeEmbedder(model="m", dimensions=64, version="v"), CorpusDataError,
         "질의 임베더"),
    ]
    try:
        for name, embedder, error, message in cases:
            with patch.object(api.OpenAIEmbedder, "from_env", lambda e=embedder: e):
                with pytest.raises(error, match=message):
                    api.configure_with_corpus_db(name)
            assert api.services.fetch_clauses is None
            with pytest.raises(RuntimeError, match="not configured"):
                _evidence.evidence_store()
    finally:
        get_settings.cache_clear()


def test_read_release_problems_are_not_configured(database_url, monkeypatch):
    with pytest.raises(CorpusDataError, match="DB에 없는 적재판"):
        read_release("없는-판", database_url)
    closed = "postgresql://postgres:secret-pw@127.0.0.1:1/postgres"
    with pytest.raises(RetrievalNotConfigured, match="연결하지 못했습니다") as caught:
        read_release(RELEASE, closed)
    assert not isinstance(caught.value, CorpusDataError)
    assert "secret-pw" not in str(caught.value) and caught.value.__cause__ is None
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RetrievalNotConfigured, match="DATABASE_URL이 없습니다"):
            read_release(RELEASE)
    finally:
        get_settings.cache_clear()
    name, bare = _temp_database(migrations=[])  # 마이그레이션 적용 전 DB
    try:
        with pytest.raises(CorpusDataError, match="표가 없습니다"):
            read_release(RELEASE, bare)
    finally:
        _drop(name)


def test_verify_release_passes_and_detects_changes(database_url, corpus):
    tokenizers = [SimpleTokenizer()]
    checked = run(verify_release(database_url, RELEASE, corpus, source_sha256=SUMS,
                                 tokenizers=tokenizers))
    assert checked == {
        "chunks": len(corpus.chunks), "parents": sum(r["is_parent"] for r in corpus.records),
        "related": len(corpus.records), "pages": len(corpus.pages),
        "embeddings": len(corpus.chunks), "tokens": len(corpus.chunks),
    }
    with pytest.raises(CorpusDataError, match="파일 해시"):
        run(verify_release(database_url, RELEASE, corpus, source_sha256={**SUMS, "x": "y"},
                           tokenizers=tokenizers))
    changes = {
        "청크 f1": "update public.corpus_chunks set text = text || '!' where chunk_id = 'f1'",
        "레코드 chart 차2-1": "update public.corpus_records set body = body || '{\"x\": 1}'"
                           " where record_id = '차2-1'",
        "벡터나 순서": "update public.corpus_chunks set embedding = (select embedding from"
                  " public.corpus_chunks where chunk_id = 'd3' and release = 'copy-1')"
                  " where chunk_id = 'f1'",
        "낱말 수": "update public.corpus_chunk_tokens set token_count = token_count + 1"
                " where chunk_id = 'f1'",
        "페이지": "update public.corpus_source_pages set content_hash = repeat('0', 64)"
               " where page = 1",
    }
    for message, statement in changes.items():
        with psycopg.connect(database_url) as conn:  # 판을 하나 더 넣고 그 판만 고친다
            load_release(conn, "copy-1", corpus, source_sha256=SUMS, tokenizers=tokenizers)
            conn.execute(statement + " and release = 'copy-1'")
            conn.commit()
        try:
            with pytest.raises(CorpusDataError, match=message):
                run(verify_release(database_url, "copy-1", corpus, source_sha256=SUMS,
                                   tokenizers=tokenizers))
        finally:
            with psycopg.connect(database_url) as conn:
                assert delete_release(conn, "copy-1")
                conn.commit()
    # 다른 판을 넣고 지워도 읽던 판은 그대로다
    run(verify_release(database_url, RELEASE, corpus, source_sha256=SUMS, tokenizers=tokenizers))


def test_load_is_all_or_nothing_and_never_overwrites(database_url, corpus):
    tokenizers = [SimpleTokenizer()]
    with psycopg.connect(database_url) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            load_release(conn, RELEASE, corpus, source_sha256=SUMS, tokenizers=tokenizers)
        conn.rollback()
        with pytest.raises(ValueError, match="판 이름"):
            load_release(conn, "Bad Name", corpus, source_sha256=SUMS, tokenizers=tokenizers)
        with pytest.raises(ValueError, match="토큰화 이름이 겹칩니다"):
            load_release(conn, "twice-1", corpus, source_sha256=SUMS,
                         tokenizers=[SimpleTokenizer(), SimpleTokenizer()])
        # 끊긴 레코드(없는 문서)는 DB가 거절하고, 그때까지 넣은 행도 남지 않는다
        orphan = {**corpus.records[0], "record_id": "끊긴 레코드", "document_id": "없는 문서",
                  "is_parent": False}
        broken = dataclasses.replace(corpus, records=[*corpus.records, orphan])
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            load_release(conn, "broken-1", broken, source_sha256=SUMS, tokenizers=tokenizers)
        conn.rollback()
        # SQL이 날짜로 비교하는 값은 YYYY-MM-DD만 받는다(로더는 20230101도 날짜로 본다)
        loose = [{**row, "metadata": {**row["metadata"], "accident_date_from": "20230101"}}
                 if row["chunk_id"] == "t3" else row for row in corpus.chunks]
        with pytest.raises(CorpusDataError, match="accident_date_from가 YYYY-MM-DD가 아닙니다"):
            load_release(conn, "loose-1", dataclasses.replace(corpus, chunks=loose),
                         source_sha256=SUMS, tokenizers=tokenizers)
        conn.rollback()
        names = [row["release"] for row in list_releases(conn)]
        assert names == [RELEASE, OTHER]
        for table in ("corpus_documents", "corpus_chunks", "corpus_records",
                      "corpus_chunk_tokens"):
            others = conn.execute(
                f"select count(*) from public.{table} where release not in (%s, %s)",
                (RELEASE, OTHER),
            ).fetchone()[0]
            assert others == 0, table
        assert not delete_release(conn, "없는-판")
