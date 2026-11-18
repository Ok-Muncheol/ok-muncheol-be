"""B(통합): Postgres(pgvector) 코퍼스 색인. `VectorIndex`(app/retrieval/index.py)의 DB 구현이다.

표는 `supabase/migrations/20261118180000_corpus_index.sql`, 적재는 `scripts/load_corpus_release.py`.
BE는 적재판 하나(`CORPUS_DB_RELEASE`)만 읽는다. 판은 한 번 넣으면 고치지 않으므로 읽은 값을
프로세스가 사는 동안 그대로 들고 있어도 된다.

    release = read_release("20261116-1742")          # 없거나 못 읽으면 RetrievalNotConfigured
    configure_retrieval(index=PgCorpusIndex(release), embedder=..., default_terms_edition=...)

메모리 색인(`InMemoryIndex`)과 같은 결과를 내도록 맞춘 점:
- 필터(`SearchFilter`)는 SQL로 옮겼다. 규칙은 `index.matches`와 같다(기간 모름 통과, 목록 포함).
- 벡터: 코사인 유사도(`1 − 거리`) 내림차순, 동점은 chunk_id 순. 근사 색인 없이 전부 비교한다.
  pgvector는 단정밀도로 더해서 점수가 메모리 색인과 소수 일곱째 자리쯤에서 다를 수 있다.
- 키워드: 적재할 때 세어 둔 청크별 낱말 수(`corpus_chunk_tokens`)로 SQL이 BM25를 계산한다.
  식·상수(K1·B)·더하는 순서(질의 낱말 순)가 `keyword.BM25Index.scores`와 같다. 통계(문서 수·평균
  길이·문서 빈도)는 필터와 상관없이 코퍼스 전체로 낸다. 질의는 BE가 같은 토큰화로 나눈다.
- 정렬의 chunk_id 비교는 `collate "C"`(코드 포인트 순)로 해서 Python 문자열 비교와 같다.
- 조항 직접 조회(`fetch_by_parent`)는 순위를 매기지 않으므로 코퍼스 청크를 한 번 읽어 두고 거기서
  찾는다(보고서가 조항마다 한 번씩 부른다). 벡터는 읽지 않는다.

DB 주소가 없거나 연결하지 못하면 `app.db.connect`의 503(`ApiException`)이 그대로 올라간다.
"""

import copy
import json
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from app import db
from app.config import get_settings
from app.retrieval.embedding import EmbeddingMismatch, EmbeddingSpec, RetrievalNotConfigured
from app.retrieval.index import SearchFilter, matches
from app.retrieval.keyword import K1, B, Tokenizer, default_tokenizer
from app.retrieval.loader import CorpusDataError, search_chunk
from app.schemas.retrieval import Corpus, SearchChunk

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection]]
# 토큰화가 적재 때와 같은지 볼 청크 수(키워드 검색을 처음 할 때 한 번)
TOKENIZER_SAMPLE = 5
_JSON_SCALARS = (str, int, float, bool, type(None), list, dict)


@dataclass(frozen=True)
class CorpusRelease:
    """적재판 하나의 머리 정보(`corpus_releases` 한 줄 + 그 판의 문서 + 약관 수치 레코드)."""

    name: str
    spec: EmbeddingSpec
    keyword_tokenizers: frozenset[str]
    documents: Mapping[str, dict]  # document_id → corpus_documents 행
    row_counts: Mapping[str, int] = field(default_factory=dict)
    source_sha256: Mapping[str, str] = field(default_factory=dict)
    # corpus_records의 schedule_value 본문(적재 순서). 대물·대인 산정이 쓴다(app/api/reports.py)
    schedule_values: tuple[dict, ...] = ()

    def terms_edition(self) -> str:
        """약관 판. 판이 하나가 아니면 고를 수 없어 CorpusDataError.

        규칙은 `loader.CorpusRows.terms_edition`과 같다.
        """
        editions = {d["edition"] for d in self.documents.values() if d["corpus"] == "policy_terms"}
        if len(editions) != 1:
            raise CorpusDataError(
                f"B: 적재판 {self.name}: 약관 판을 하나로 정할 수 없습니다: {sorted(editions)}"
            )
        return editions.pop()


def read_release(name: str, database_url: str | None = None) -> CorpusRelease:
    """적재판 머리 정보를 읽는다(서버가 지식베이스를 켤 때 한 번, 동기 연결).

    DB 주소가 없거나 연결하지 못하면 RetrievalNotConfigured, 표나 판이 없으면 CorpusDataError.
    메시지에 주소·비밀번호를 넣지 않는다.
    """
    url = database_url or get_settings().database_url
    if not url:
        raise RetrievalNotConfigured("B: 코퍼스 색인을 읽을 DATABASE_URL이 없습니다")
    try:
        with psycopg.connect(
            db.normalize_database_url(url), autocommit=True, row_factory=dict_row,
            prepare_threshold=None, connect_timeout=10,
        ) as conn:
            head = conn.execute(
                "select release, embedding_model, embedding_dimensions, embedding_version,"
                " keyword_tokenizers, source_sha256, row_counts"
                " from public.corpus_releases where release = %s",
                (name,),
            ).fetchone()
            if head is None:
                raise CorpusDataError(f"B: DB에 없는 적재판입니다(corpus_releases): {name}")
            documents = conn.execute(
                "select * from public.corpus_documents where release = %s order by document_id",
                (name,),
            ).fetchall()
            values = conn.execute(
                "select body from public.corpus_records"
                " where release = %s and kind = 'schedule_value' order by seq",
                (name,),
            ).fetchall()
    except psycopg.errors.UndefinedTable as exc:
        raise CorpusDataError(
            "B: 코퍼스 색인 표가 없습니다(마이그레이션 20261118180000 적용 전)"
        ) from exc
    except psycopg.OperationalError as exc:  # 원문에 주소가 섞일 수 있어 오류 종류만 남긴다
        raise RetrievalNotConfigured(
            f"B: 코퍼스 색인 DB에 연결하지 못했습니다: {type(exc).__name__}"
        ) from None
    if not documents:
        raise CorpusDataError(f"B: 문서가 없는 적재판입니다(corpus_documents): {name}")
    return CorpusRelease(
        name=head["release"],
        spec=EmbeddingSpec(
            head["embedding_model"], head["embedding_dimensions"], head["embedding_version"]
        ),
        keyword_tokenizers=frozenset(head["keyword_tokenizers"]),
        documents={row["document_id"]: row for row in documents},
        row_counts=dict(head["row_counts"]),
        source_sha256=dict(head["source_sha256"]),
        schedule_values=tuple(row["body"] for row in values),
    )


async def load_corpus_chunks(
    connect: Connect, release: CorpusRelease, corpus: str
) -> list[SearchChunk]:
    """한 코퍼스의 청크를 적재 순서대로(벡터 없이). 색인의 조항 조회와 근거 저장소가 따로 부른다."""
    async with connect() as conn:
        cursor = await conn.execute(
            f"select {_CHUNK_COLUMNS} from public.corpus_chunks c"
            " where c.release = %(release)s and c.corpus = %(corpus)s order by c.seq",
            {"release": release.name, "corpus": corpus},
        )
        rows = await cursor.fetchall()
    return [search_chunk(row, release.documents) for row in rows]  # type: ignore[arg-type]


def vector_literal(vector: list[float]) -> str:
    """pgvector 입력 글자열. repr은 값을 그대로 되살리는 가장 짧은 표기다."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _wanted_json(value: object) -> str:
    """metadata 필터 값 → JSON 배열. 목록이면 그중 하나, 아니면 그 값 하나와 비교한다.

    JSON으로 옮길 수 없는 값(튜플·집합 등)은 metadata(JSON에서 온 값)와 같을 수 없으므로 뺀다.
    남은 것이 없으면 빈 배열이라 아무것도 통과하지 않는다(`index._metadata_matches`와 같다).
    """
    wanted = value if isinstance(value, list) else [value]
    kept = []
    for item in wanted:
        if not isinstance(item, _JSON_SCALARS):
            continue
        try:
            json.dumps(item, allow_nan=False)
        except (TypeError, ValueError):
            continue
        kept.append(item)
    return json.dumps(kept, ensure_ascii=False, allow_nan=False)


def filter_sql(search_filter: SearchFilter) -> tuple[str, dict[str, object]]:
    """`index.matches`를 SQL 조건으로 옮긴다(c = corpus_chunks, d = corpus_documents).

    - 문서 적용 기간: effective_from이 빈 값이면 기간 모름이라 통과, 아니면 양끝 포함.
    - 청크의 적용 사고일 범위: metadata의 값이 없거나 비면 열린 끝.
    - metadata: 키가 있어야 하고, 필터 값(목록이면 그중 하나)이 청크 값(목록이면 그 안의
      하나)과 같다.
      JSON 비교라 참거짓과 숫자는 서로 다르다(Python은 True == 1). 필터 값은 실제로는 글자열이다.
    """
    clauses: list[str] = []
    params: dict[str, object] = {}
    if search_filter.edition is not None:
        clauses.append("d.edition = %(edition)s")
        params["edition"] = search_filter.edition
    if search_filter.document_id is not None:
        clauses.append("d.document_id = %(document_id)s")
        params["document_id"] = search_filter.document_id
    if search_filter.accident_date is not None:
        clauses.append(
            "(d.effective_from = '' or ("
            "%(accident_date)s::date >= nullif(d.effective_from, '')::date"
            " and (d.effective_to is null or %(accident_date)s::date <= d.effective_to::date)))"
        )
        params["accident_date"] = search_filter.accident_date.isoformat()
    if search_filter.accident_window is not None:
        for key, compare in (("accident_date_from", ">="), ("accident_date_to", "<=")):
            value = f"nullif(c.metadata ->> '{key}', '')"
            clauses.append(
                f"({value} is null or %(accident_window)s::date {compare} {value}::date)"
            )
        params["accident_window"] = search_filter.accident_window.isoformat()
    for number, (key, value) in enumerate(search_filter.metadata.items()):
        name, wanted = f"metadata_key_{number}", f"metadata_wanted_{number}"
        clauses.append(
            f"(c.metadata ? %({name})s and exists ("
            f"select 1 from jsonb_array_elements(%({wanted})s::jsonb) as wanted(item)"
            f" where case when jsonb_typeof(c.metadata -> %({name})s) = 'array'"
            f" then exists (select 1 from jsonb_array_elements(c.metadata -> %({name})s)"
            " as actual(item) where actual.item = wanted.item)"
            f" else c.metadata -> %({name})s = wanted.item end))"
        )
        params[name] = key
        params[wanted] = _wanted_json(value)
    return " and ".join(clauses) or "true", params


_CHUNK_COLUMNS = (
    "c.corpus, c.document_id, c.chunk_id, c.parent_id, c.page_start, c.page_end, c.text, c.metadata"
)
_FROM_CHUNKS = (
    " from public.corpus_chunks c"
    " join public.corpus_documents d on d.release = c.release and d.document_id = c.document_id"
)
# 영벡터면 pgvector 거리가 NaN이다. 메모리 색인처럼 점수 0으로 본다
_VECTOR_SQL = (
    f"select {_CHUNK_COLUMNS}, s.score"
    + _FROM_CHUNKS
    + " cross join lateral (select (c.embedding operator(extensions.<=>)"
    " %(vector)s::extensions.vector)::float8 as distance) as measured"
    " cross join lateral (select case when measured.distance = 'NaN'::float8 then 0.0::float8"
    " else 1.0::float8 - measured.distance end as score) as s"
    " where c.release = %(release)s and c.corpus = %(corpus)s and {where}"
    ' order by s.score desc, c.chunk_id collate "C"'
    " limit %(limit)s"
)
# keyword.BM25Index.scores와 같은 식. idf·tf·길이 보정을 같은 순서로 곱하고 질의 낱말 순으로 더한다.
# matches는 (청크, 질의 낱말)마다 한 줄이고 좁은 값만 담는다(낱말 수 jsonb를 정렬까지 끌고 가면
# 디스크 정렬이 된다). 문서 빈도도 여기서 센다. docs·matches는 필터 전, 코퍼스 전체다.
# docs의 `|| '{}'`는 값을 바꾸지 않는다. 압축돼 저장된 jsonb를 여기서 한 번만 풀어 두려는 것이다
# (그대로 두면 낱말마다 다시 푼다. 실제 코퍼스에서 질의 하나가 25ms → 10ms)
_KEYWORD_SQL = (
    "with query as ("
    " select term, ord from unnest(%(terms)s::text[]) with ordinality as q(term, ord)"
    "), docs as materialized ("
    " select t.chunk_id, t.token_count, t.term_counts || '{{}}'::jsonb as term_counts"
    " from public.corpus_chunk_tokens t"
    " where t.release = %(release)s and t.tokenizer = %(tokenizer)s and t.corpus = %(corpus)s"
    "), stats as ("
    " select count(*)::float8 as doc_count, coalesce(sum(token_count), 0)::float8 as total_length"
    " from docs"
    "), matches as materialized ("
    " select docs.chunk_id, docs.token_count, query.ord,"
    " (docs.term_counts ->> query.term)::float8 as tf"
    " from docs join query on docs.term_counts ? query.term"
    "), frequency as ("
    " select ord, count(*)::float8 as doc_frequency from matches group by ord"
    "), scored as ("
    " select matches.chunk_id, sum("
    "   ln(1 + (stats.doc_count - frequency.doc_frequency + 0.5)"
    "          / (frequency.doc_frequency + 0.5))"
    "   * matches.tf * (%(k1)s::float8 + 1)"
    "   / (matches.tf + %(k1)s::float8 * (1 - %(b)s::float8"
    "      + %(b)s::float8 * matches.token_count / (stats.total_length / stats.doc_count)))"
    "   order by matches.ord) as score"
    " from matches"
    " join frequency on frequency.ord = matches.ord"
    " cross join stats"
    " group by matches.chunk_id"
    f") select {_CHUNK_COLUMNS}, scored.score"
    + _FROM_CHUNKS
    + " join scored on scored.chunk_id = c.chunk_id"
    " where c.release = %(release)s and c.corpus = %(corpus)s and {where}"
    ' order by scored.score desc, c.chunk_id collate "C"'
    " limit %(limit)s"
)
_TOKENIZER_SAMPLE_SQL = (
    "select c.chunk_id, c.text, t.term_counts, t.token_count"
    " from public.corpus_chunk_tokens t"
    " join public.corpus_chunks c"
    " on c.release = t.release and c.corpus = t.corpus and c.chunk_id = t.chunk_id"
    " where t.release = %(release)s and t.tokenizer = %(tokenizer)s"
    " order by md5(c.chunk_id), c.chunk_id limit %(limit)s"
)


class PgCorpusIndex:
    """적재판 하나를 읽는 색인. 검색은 호출마다 연결 하나를 열고 닫는다(Session pooler)."""

    def __init__(
        self,
        release: CorpusRelease,
        *,
        connect: Connect | None = None,
        tokenizer: Tokenizer | None = None,
    ) -> None:
        self._release = release
        self._connect: Connect = connect or db.connect
        self._tokenizer = tokenizer  # 키워드 검색이 처음 필요할 때 기본 토큰화를 읽는다
        self._checked_tokenizer: str | None = None
        self._chunks: dict[str, dict[str, list[SearchChunk]]] = {}  # 코퍼스 → parent_id → 청크

    @property
    def release(self) -> CorpusRelease:
        return self._release

    @property
    def spec(self) -> EmbeddingSpec | None:
        return self._release.spec

    @property
    def tokenizer(self) -> Tokenizer:
        if self._tokenizer is None:
            self._tokenizer = default_tokenizer()
        return self._tokenizer

    @property
    def keyword_version(self) -> str:
        return f"bm25-v1:{self.tokenizer.name}"

    async def _fetch(self, sql: str, params: dict[str, object]) -> list[dict]:
        async with self._connect() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchall()

    def _hits(self, rows: list[dict]) -> list[tuple[SearchChunk, float]]:
        documents = self._release.documents
        return [(search_chunk(row, documents), float(row["score"])) for row in rows]  # type: ignore[arg-type]

    async def search_vector(
        self, search_filter: SearchFilter, query_vector: list[float], limit: int
    ) -> list[tuple[SearchChunk, float]]:
        dimensions = self._release.spec.dimensions
        if len(query_vector) != dimensions:
            raise EmbeddingMismatch(f"벡터 길이 불일치: {len(query_vector)} ≠ {dimensions}")
        if limit <= 0:
            return []
        where, params = filter_sql(search_filter)
        params.update(
            release=self._release.name,
            corpus=search_filter.corpus,
            vector=vector_literal(query_vector),
            limit=limit,
        )
        return self._hits(await self._fetch(_VECTOR_SQL.format(where=where), params))

    async def search_keyword(
        self, search_filter: SearchFilter, query_text: str, limit: int
    ) -> list[tuple[SearchChunk, float]]:
        tokenizer = self.tokenizer
        if tokenizer.name not in self._release.keyword_tokenizers:
            raise RetrievalNotConfigured(
                f"B: 적재판 {self._release.name}에 {tokenizer.name} 토큰화의 키워드 색인이 없습니다"
                f"(있는 것: {sorted(self._release.keyword_tokenizers)})"
                " — 그 토큰화로 다시 적재하세요"
            )
        await self._check_tokenizer(tokenizer)
        terms = list(dict.fromkeys(tokenizer.tokenize(query_text)))
        if not terms or limit <= 0:
            return []
        where, params = filter_sql(search_filter)
        params.update(
            release=self._release.name,
            corpus=search_filter.corpus,
            tokenizer=tokenizer.name,
            terms=terms,
            k1=K1,
            b=B,
            limit=limit,
        )
        return self._hits(await self._fetch(_KEYWORD_SQL.format(where=where), params))

    async def _check_tokenizer(self, tokenizer: Tokenizer) -> None:
        """적재할 때의 토큰화와 지금 토큰화가 같은 결과를 내는지 청크 몇 개로 본다(한 번).

        낱말 수는 적재할 때 세어 두므로, 그 뒤 토큰화 코드나 형태소 모델이 바뀌면 질의와 색인이
        어긋난다. 조용히 틀린 순위를 내지 않게 CorpusDataError로 멈춘다.
        """
        if self._checked_tokenizer == tokenizer.name:
            return
        rows = await self._fetch(
            _TOKENIZER_SAMPLE_SQL,
            {"release": self._release.name, "tokenizer": tokenizer.name, "limit": TOKENIZER_SAMPLE},
        )
        for row in rows:
            counts = Counter(tokenizer.tokenize(row["text"]))
            if dict(counts) != row["term_counts"] or sum(counts.values()) != row["token_count"]:
                raise CorpusDataError(
                    f"B: 적재판 {self._release.name}의 낱말 수가 지금 {tokenizer.name} 토큰화와"
                    f" 다릅니다(청크 {row['chunk_id']}) — 다시 적재해야 합니다"
                )
        self._checked_tokenizer = tokenizer.name

    async def fetch_by_parent(
        self, corpus: Corpus, parent_ids: list[str], *, edition: str | None = None
    ) -> list[SearchChunk]:
        by_parent = self._chunks.get(corpus)
        if by_parent is None:
            by_parent = {}
            for chunk in await load_corpus_chunks(self._connect, self._release, corpus):
                by_parent.setdefault(chunk["parent_id"], []).append(chunk)
            self._chunks[corpus] = by_parent
        search_filter = SearchFilter(corpus=corpus, edition=edition)
        out: list[SearchChunk] = []
        for parent_id in dict.fromkeys(parent_ids):
            found = [c for c in by_parent.get(parent_id, []) if matches(c, search_filter)]
            found.sort(key=lambda chunk: chunk["page_start"])  # 같은 페이지는 적재 순서 유지
            out += found
        return copy.deepcopy(out)  # 들고 있는 값을 호출한 쪽이 고치지 못하게
