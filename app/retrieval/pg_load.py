"""B(통합): 코퍼스 적재 행(db_import)을 Postgres에 한 판으로 넣고, 넣은 것을 다시 읽어 대조한다.

서버는 이 모듈을 부르지 않는다. `scripts/load_corpus_release.py`(사람이 실행)와 테스트가 쓴다.
표는 `supabase/migrations/20261118180000_corpus_index.sql`, 읽는 쪽은 `pg_index.py`·
`pg_evidence_store.py`다.

- 행은 D 로더(`loader.read_corpus`)가 읽고 검사한 `CorpusRows`를 그대로 받는다.
- 한 판은 한 트랜잭션으로 넣는다(`load_release`는 commit하지 않는다. 호출하는 쪽이 한다).
  중간에 실패하면 아무것도 남지 않는다. 같은 이름의 판이 있으면 기본 키 충돌로 실패한다.
- 키워드 검색용 낱말 수는 여기서 센다. 검색 때 질의를 나누는 것과 같은 토큰화 객체를 넘긴다.
- `verify_release`는 서버가 쓰는 읽기 코드(`PgEvidenceStore`)로 DB를 다시 읽어 파일과 대조한다.
"""

import re
from array import array
from collections import Counter
from collections.abc import Sequence
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import db
from app.retrieval.evidence_store import LocalCorpusEvidenceStore
from app.retrieval.keyword import Tokenizer
from app.retrieval.loader import CorpusDataError, CorpusRows
from app.retrieval.pg_evidence_store import PgEvidenceStore
from app.retrieval.pg_index import read_release, vector_literal

RELEASE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")  # corpus_releases_name_check와 같다
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DOCUMENT_COLUMNS = (
    "document_id", "corpus", "source_uri", "source_name", "edition", "effective_from",
    "effective_to", "effective_period_status", "published_month", "content_hash",
    "parser_version", "storage_sha256", "storage_bytes", "storage_relation", "usage_review",
    "reviewed_by",
)
_DOCUMENT_REQUIRED = (
    "source_name", "effective_period_status", "storage_sha256", "storage_bytes",
    "storage_relation", "usage_review",
)
_CHUNK_COLUMNS = (
    "corpus", "chunk_id", "parent_id", "document_id", "page_start", "page_end", "text",
    "raw_text", "display_text", "metadata", "embedding_model", "embedding_dimensions",
    "embedding_version", "embedding_content_hash",
)
_RECORD_COLUMNS = (
    "corpus", "kind", "record_id", "parent_id", "is_parent", "document_id", "chart_id",
    "page_start", "page_end", "body",
)
_JSON_COLUMNS = frozenset({"usage_review", "metadata", "body"})


def _is_iso_date(value: object) -> bool:
    """YYYY-MM-DD이고 실제 날짜인지. SQL이 date로 바꿔 비교하므로 로더보다 좁게 본다."""
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_rows(corpus: CorpusRows) -> None:
    """DB에 넣기 전에 본다: 없으면 안 되는 칼럼, SQL이 날짜로 비교하는 값의 모양.

    `read_corpus`가 이미 본 것(타입·연결·중복·벡터 길이)은 다시 보지 않는다.
    """
    for document_id, row in corpus.documents.items():
        missing = [name for name in _DOCUMENT_REQUIRED if row.get(name) is None]
        if missing:
            raise CorpusDataError(f"B: 문서 {document_id}에 칼럼이 없습니다: {', '.join(missing)}")
        for name in ("effective_from", "effective_to"):
            if row[name] and not _is_iso_date(row[name]):
                raise CorpusDataError(
                    f"B: 문서 {document_id}의 {name}가 YYYY-MM-DD가 아닙니다: {row[name]}"
                )
    for row in corpus.chunks:
        if not isinstance(row.get("embedding_content_hash"), str):
            raise CorpusDataError(f"B: 청크 {row['chunk_id']}에 embedding_content_hash가 없습니다")
        for name in ("accident_date_from", "accident_date_to"):
            value = row["metadata"].get(name)
            if value is not None and value != "" and not _is_iso_date(value):
                raise CorpusDataError(
                    f"B: 청크 {row['chunk_id']}의 metadata.{name}가 YYYY-MM-DD가 아닙니다: {value}"
                )


def _values(row: dict, columns: Sequence[str]) -> list[object]:
    return [
        Jsonb(row[name]) if name in _JSON_COLUMNS and row.get(name) is not None else row.get(name)
        for name in columns
    ]


def _copy(
    cursor: psycopg.Cursor, table: str, columns: Sequence[str], rows: Sequence[Sequence]
) -> None:
    with cursor.copy(f"copy public.{table} ({', '.join(columns)}) from stdin") as copy:
        for row in rows:
            copy.write_row(row)


def load_release(
    conn: psycopg.Connection,
    name: str,
    corpus: CorpusRows,
    *,
    source_sha256: dict[str, str],
    tokenizers: Sequence[Tokenizer],
    note: str | None = None,
) -> dict[str, int]:
    """읽은 행을 판 `name`으로 넣고 표마다 넣은 행 수를 돌려준다. commit은 호출하는 쪽이 한다.

    tokenizers: 키워드 검색용 낱말 수를 만들 토큰화. 서버가 쓰는 토큰화(Kiwi가 없으면 simple)가
      들어 있어야 그 서버에서 키워드 검색이 된다.
    """
    if not RELEASE_NAME.fullmatch(name):
        raise ValueError(f"판 이름은 소문자·숫자·점·밑줄·붙임표 64자 이내입니다: {name!r}")
    names = [tokenizer.name for tokenizer in tokenizers]
    if len(set(names)) != len(names):
        raise ValueError(f"토큰화 이름이 겹칩니다: {names}")
    check_rows(corpus)
    specs = {
        (row["embedding_model"], row["embedding_dimensions"], row["embedding_version"])
        for row in corpus.chunks
    }
    if len(specs) != 1:  # read_corpus가 이미 보지만, 직접 만든 CorpusRows도 받으므로 다시 본다
        raise CorpusDataError(f"B: 임베딩 모델·차원·버전이 하나가 아닙니다: {sorted(specs)}")
    model, dimensions, version = specs.pop()
    counts = {
        "corpus_documents": len(corpus.documents),
        "corpus_source_pages": len(corpus.pages),
        "corpus_records": len(corpus.records),
        "corpus_chunks": len(corpus.chunks),
    }
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "insert into public.corpus_releases (release, embedding_model, embedding_dimensions,"
            " embedding_version, keyword_tokenizers, source_sha256, row_counts, note)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (name, model, dimensions, version, names, Jsonb(source_sha256), Jsonb(counts), note),
        )
        _copy(
            cursor, "corpus_documents", ("release", *_DOCUMENT_COLUMNS),
            [[name, *_values(row, _DOCUMENT_COLUMNS)] for row in corpus.documents.values()],
        )
        _copy(
            cursor, "corpus_source_pages", ("release", "document_id", "page", "content_hash"),
            [[name, row["document_id"], row["page"], row["content_hash"]] for row in corpus.pages],
        )
        _copy(
            cursor, "corpus_records", ("release", "seq", *_RECORD_COLUMNS),
            [[name, seq, *_values(row, _RECORD_COLUMNS)] for seq, row in enumerate(corpus.records)],
        )
        _copy(
            cursor, "corpus_chunks", ("release", "seq", *_CHUNK_COLUMNS, "embedding"),
            [
                [name, seq, *_values(row, _CHUNK_COLUMNS), vector_literal(row["embedding"])]
                for seq, row in enumerate(corpus.chunks)
            ],
        )
        for tokenizer in tokenizers:
            rows = []
            for row in corpus.chunks:
                terms = Counter(tokenizer.tokenize(row["text"]))
                rows.append([
                    name, tokenizer.name, row["corpus"], row["chunk_id"],
                    sum(terms.values()), Jsonb(dict(terms)),
                ])
            _copy(
                cursor, "corpus_chunk_tokens",
                ("release", "tokenizer", "corpus", "chunk_id", "token_count", "term_counts"), rows,
            )
        for table, expected in counts.items():
            cursor.execute(
                f"select count(*) as n from public.{table} where release = %s", (name,)
            )
            found = cursor.fetchone()["n"]
            if found != expected:
                raise CorpusDataError(f"B: {table}에 {found}행이 들어갔습니다(기대 {expected})")
    return counts


def delete_release(conn: psycopg.Connection, name: str) -> bool:
    """판을 지운다(그 판의 모든 표 행이 함께 지워진다). 있었으면 True.

    commit은 호출하는 쪽이 한다.
    """
    with conn.cursor() as cursor:
        cursor.execute("delete from public.corpus_releases where release = %s", (name,))
        return cursor.rowcount > 0


def list_releases(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "select release, embedding_model, embedding_dimensions, keyword_tokenizers,"
            " row_counts, note, loaded_at from public.corpus_releases order by loaded_at, release"
        )
        return cursor.fetchall()


def _float32(values: Sequence[float]) -> array:
    return array("f", values)


async def verify_release(
    database_url: str,
    name: str,
    corpus: CorpusRows,
    *,
    source_sha256: dict[str, str],
    tokenizers: Sequence[Tokenizer],
) -> dict[str, int]:
    """DB의 판 `name`을 서버가 쓰는 코드로 다시 읽어 파일 행과 대조한다. 다르면 CorpusDataError.

    보는 것: 머리 정보(임베딩·해시·행 수), 청크·부모 레코드·관련 레코드·페이지(근거 저장소의 네
    메서드 결과가 파일로 만든 로컬 어댑터와 같은지), 벡터(단정밀도로 같은지), 낱말 수.
    돌려주는 값은 대조한 건수다.
    """
    release = read_release(name, database_url)
    first = corpus.chunks[0]
    expected_spec = (
        first["embedding_model"], first["embedding_dimensions"], first["embedding_version"]
    )
    if (release.spec.model, release.spec.dimensions, release.spec.version) != expected_spec:
        raise CorpusDataError(
            f"B: 판 {name}의 임베딩 {release.spec}이 파일 {expected_spec}과 다릅니다"
        )
    if dict(release.source_sha256) != source_sha256:
        raise CorpusDataError(f"B: 판 {name}에 적힌 파일 해시가 지금 파일과 다릅니다")
    if set(release.documents) != set(corpus.documents):
        raise CorpusDataError(f"B: 판 {name}의 문서 목록이 파일과 다릅니다")

    def connect():
        return db.connect(database_url)

    store, local = PgEvidenceStore(release, connect=connect), LocalCorpusEvidenceStore(corpus)
    checked = {"chunks": 0, "parents": 0, "related": 0, "pages": 0, "embeddings": 0, "tokens": 0}
    for row in corpus.chunks:
        key = (row["corpus"], row["chunk_id"])
        if await store.get_chunk(*key) != await local.get_chunk(*key):
            raise CorpusDataError(f"B: 청크 {row['chunk_id']}가 파일과 다릅니다")
        checked["chunks"] += 1
    for row in corpus.records:
        related = (row["corpus"], row["kind"], row["record_id"])
        if await store.get_related(*related) != await local.get_related(*related):
            raise CorpusDataError(f"B: 레코드 {row['kind']} {row['record_id']}가 파일과 다릅니다")
        checked["related"] += 1
        if row["is_parent"]:
            parent = (row["corpus"], row["parent_id"])
            if await store.get_parent(*parent) != await local.get_parent(*parent):
                raise CorpusDataError(f"B: 부모 레코드 {row['parent_id']}가 파일과 다릅니다")
            checked["parents"] += 1
    for row in corpus.pages:
        page = (row["document_id"], row["page"])
        if await store.get_source_page(*page) != await local.get_source_page(*page):
            raise CorpusDataError(f"B: 페이지 {page}가 파일과 다릅니다")
        checked["pages"] += 1

    async with connect() as conn:
        counts = {}
        for table in ("corpus_documents", "corpus_source_pages", "corpus_records", "corpus_chunks"):
            cursor = await conn.execute(
                f"select count(*) as n from public.{table} where release = %s", (name,)
            )
            counts[table] = (await cursor.fetchone())["n"]
        expected_counts = {
            "corpus_documents": len(corpus.documents), "corpus_source_pages": len(corpus.pages),
            "corpus_records": len(corpus.records), "corpus_chunks": len(corpus.chunks),
        }
        if counts != expected_counts or dict(release.row_counts) != expected_counts:
            raise CorpusDataError(
                f"B: 판 {name}의 행 수 {counts}가 파일 {expected_counts}과 다릅니다"
            )
        vectors = {
            (row["corpus"], row["chunk_id"]): (seq, row["embedding"])
            for seq, row in enumerate(corpus.chunks)
        }
        cursor = await conn.execute(
            "select corpus, chunk_id, seq, embedding::text as embedding"
            " from public.corpus_chunks where release = %s",
            (name,),
        )
        async for row in cursor:
            seq, expected = vectors[(row["corpus"], row["chunk_id"])]
            stored = _float32([float(x) for x in row["embedding"][1:-1].split(",")])
            if row["seq"] != seq or stored != _float32(expected):
                raise CorpusDataError(f"B: 청크 {row['chunk_id']}의 벡터나 순서가 파일과 다릅니다")
            checked["embeddings"] += 1
        if checked["embeddings"] != len(corpus.chunks):
            raise CorpusDataError(f"B: 벡터 {checked['embeddings']}개만 대조했습니다")
        if release.keyword_tokenizers != {tokenizer.name for tokenizer in tokenizers}:
            raise CorpusDataError(
                f"B: 판 {name}의 토큰화 {sorted(release.keyword_tokenizers)}가 요청과 다릅니다"
            )
        texts = {(row["corpus"], row["chunk_id"]): row["text"] for row in corpus.chunks}
        for tokenizer in tokenizers:
            cursor = await conn.execute(
                "select corpus, chunk_id, token_count, term_counts"
                " from public.corpus_chunk_tokens where release = %s and tokenizer = %s",
                (name, tokenizer.name),
            )
            seen = 0
            async for row in cursor:
                terms = Counter(tokenizer.tokenize(texts[(row["corpus"], row["chunk_id"])]))
                if dict(terms) != row["term_counts"] or sum(terms.values()) != row["token_count"]:
                    raise CorpusDataError(
                        f"B: 청크 {row['chunk_id']}의 {tokenizer.name} 낱말 수가 파일과 다릅니다"
                    )
                seen += 1
            if seen != len(corpus.chunks):
                raise CorpusDataError(f"B: {tokenizer.name} 낱말 수가 {seen}개 청크에만 있습니다")
            checked["tokens"] += seen
    return checked
