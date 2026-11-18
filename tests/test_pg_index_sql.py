"""B(통합): DB 색인의 SQL 조립(필터·벡터 표기)과 적재 전 검사. DB 없이 돈다.

실제 Postgres에서 메모리 색인과 결과를 맞대는 검사는 tests/test_pg_corpus_index.py다.
"""

import json
from datetime import date

import pytest

from app.retrieval.embedding import EmbeddingSpec
from app.retrieval.index import SearchFilter
from app.retrieval.loader import CorpusDataError, CorpusRows
from app.retrieval.pg_index import CorpusRelease, filter_sql, vector_literal
from app.retrieval.pg_load import RELEASE_NAME, check_rows


def test_empty_filter_is_true():
    assert filter_sql(SearchFilter(corpus="fault_standards")) == ("true", {})


def test_filter_values_go_in_as_parameters_not_sql():
    """필터 값(키 이름 포함)은 SQL 글자열에 섞이지 않고 모두 파라미터로 간다."""
    hostile = "x'; drop table public.corpus_chunks; --"
    sql, params = filter_sql(SearchFilter(
        corpus="policy_terms", edition=hostile, document_id=hostile,
        accident_date=date(2024, 5, 1), accident_window=date(2024, 5, 2),
        metadata={hostile: hostile, "schedule": ["별표3", hostile]},
    ))
    assert "drop table" not in sql
    assert params["edition"] == params["document_id"] == params["metadata_key_0"] == hostile
    assert params["accident_date"] == "2024-05-01" and params["accident_window"] == "2024-05-02"
    assert json.loads(params["metadata_wanted_0"]) == [hostile]
    assert json.loads(params["metadata_wanted_1"]) == ["별표3", hostile]
    assert sql.count(" and ") >= 5


@pytest.mark.parametrize(("value", "wanted"), [
    ("차1-1", ["차1-1"]),
    (["a", "b"], ["a", "b"]),
    ([], []),  # 빈 목록은 아무것도 통과하지 않는다
    (None, [None]),  # JSON null과 비교
    (True, [True]),
    (7, [7]),
    ({"k": [1]}, [{"k": [1]}]),
    (("a", "b"), []),  # 튜플·집합·날짜는 JSON에서 온 값과 같을 수 없다
    ({"a"}, []),
    ([date(2024, 1, 1), "남는 값"], ["남는 값"]),
    (float("nan"), []),
])
def test_metadata_filter_values_become_a_json_array(value, wanted):
    _, params = filter_sql(SearchFilter(corpus="fault_standards", metadata={"key": value}))
    assert json.loads(params["metadata_wanted_0"]) == wanted


def test_vector_literal_round_trips_floats():
    values = [0.023590087890625, -1.0, 0.0, 1e-07, 3]
    text = vector_literal(values)
    assert text.startswith("[") and text.endswith("]")
    assert [float(x) for x in text[1:-1].split(",")] == [float(v) for v in values]


def test_terms_edition_needs_exactly_one_policy_edition():
    def release(*editions: str) -> CorpusRelease:
        documents = {f"t{n}": {"corpus": "policy_terms", "edition": edition}
                     for n, edition in enumerate(editions)}
        documents["f"] = {"corpus": "fault_standards", "edition": "2023.6"}
        return CorpusRelease(name="r-1", spec=EmbeddingSpec("m", 1536, "v"),
                             keyword_tokenizers=frozenset({"simple"}), documents=documents)

    assert release("판-1").terms_edition() == "판-1"
    assert release("판-1", "판-1").terms_edition() == "판-1"
    for editions in ((), ("판-1", "판-2")):
        with pytest.raises(CorpusDataError, match="약관 판을 하나로"):
            release(*editions).terms_edition()


@pytest.mark.parametrize("name", ["20261116-1742", "a", "v1.2_rc-3", "0" * 64])
def test_release_names_accepted(name):
    assert RELEASE_NAME.fullmatch(name)


@pytest.mark.parametrize("name", ["", "Upper", "-앞붙임표", "한글", "a b", "a/b", "0" * 65])
def test_release_names_rejected(name):
    assert not RELEASE_NAME.fullmatch(name)


def _rows(**over) -> CorpusRows:
    document = {
        "document_id": "d1", "corpus": "policy_terms", "effective_from": "2026-11-11",
        "effective_to": None, "source_name": "d1.pdf", "effective_period_status": "assumed",
        "storage_sha256": "a" * 64, "storage_bytes": 1, "storage_relation": "original",
        "usage_review": {}, **over.get("document", {}),
    }
    chunk = {"chunk_id": "c1", "embedding_content_hash": "c" * 64,
             "metadata": over.get("metadata", {}), **over.get("chunk", {})}
    return CorpusRows(documents={"d1": document}, chunks=[chunk], records=[], pages=[])


def test_check_rows_accepts_plain_rows_and_open_ended_windows():
    check_rows(_rows())
    check_rows(_rows(document={"effective_from": "", "effective_to": "2027-01-01"}))
    check_rows(_rows(metadata={"accident_date_from": "2023-01-01", "accident_date_to": ""}))
    check_rows(_rows(metadata={"accident_date_from": None}))


@pytest.mark.parametrize(("over", "message"), [
    ({"document": {"effective_from": "20261111"}}, "effective_from가 YYYY-MM-DD가 아닙니다"),
    ({"document": {"effective_to": "2026-02-30"}}, "effective_to가 YYYY-MM-DD가 아닙니다"),
    ({"document": {"usage_review": None}}, "칼럼이 없습니다: usage_review"),
    ({"metadata": {"accident_date_to": "2023/01/01"}}, "metadata.accident_date_to가 YYYY-MM-DD"),
    ({"metadata": {"accident_date_from": 20230101}}, "metadata.accident_date_from가 YYYY-MM-DD"),
    ({"metadata": {"accident_date_from": False}}, "metadata.accident_date_from가 YYYY-MM-DD"),
    ({"chunk": {"embedding_content_hash": None}}, "embedding_content_hash가 없습니다"),
])
def test_check_rows_rejects_what_sql_cannot_compare(over, message):
    """SQL이 날짜로 바꿔 비교하는 값은 YYYY-MM-DD만 받는다(로더는 20261111·False도 통과시킨다)."""
    with pytest.raises(CorpusDataError, match=message):
        check_rows(_rows(**over))
