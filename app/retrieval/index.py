"""D: 온라인 검색 색인 인터페이스와 메모리 구현.

VectorIndex가 교체 지점이다. Supabase(pgvector) 구현은 필터와 정렬을 DB에서 하도록
같은 메서드만 채우면 된다.
InMemoryIndex는 테스트·C 데이터 연결 전 확인용이다. 색인 산출물(EmbeddedChunk)은 C 소유.
"""

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, get_args

from app.retrieval.embedding import (
    EmbeddingMismatch,
    EmbeddingSpec,
    RetrievalNotConfigured,
    check_spec,
)
from app.retrieval.keyword import BM25Index, Tokenizer, default_tokenizer
from app.schemas.retrieval import Corpus, EmbeddedChunk, SearchChunk, SourceDocument

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchFilter:
    """구조 필터. 조건을 자동으로 완화하지 않는다. 문서 기간을 모를 때만 날짜 필터를 통과한다."""

    corpus: Corpus
    # 적용 기간 필터. None이면 적용하지 않음(도표에만 값을 넣는다)
    accident_date: date | None = None
    # document.edition 일치. None이면 적용하지 않음
    edition: str | None = None
    # document.document_id 일치. None이면 적용하지 않음
    document_id: str | None = None
    # chunk.metadata 비교. 필터 값이 목록이면 그중 하나와 일치, 청크 값이 목록이면 그 안에 포함되면
    # 통과. 키가 없으면 제외, 빈 목록은 아무것도 통과하지 않는다
    metadata: dict[str, object] = field(default_factory=dict)
    # 청크 metadata의 적용 사고일 범위(accident_date_from/to, 별표3 과실상계 등). None이면 적용하지
    # 않음. 범위 값이 없는 청크는 통과한다. 문서 적용 기간(accident_date)과 달리 모든 코퍼스에 쓴다
    accident_window: date | None = None


class VectorIndex(Protocol):
    @property
    def spec(self) -> EmbeddingSpec | None:
        """색인의 임베딩 모델·차원·버전. 비어 있으면 None."""
        ...

    async def search_vector(
        self, search_filter: SearchFilter, query_vector: list[float], limit: int
    ) -> list[tuple[SearchChunk, float]]:
        """필터를 통과한 청크를 코사인 점수 내림차순으로 최대 limit개."""
        ...

    @property
    def keyword_version(self) -> str:
        """키워드 검색 방식 이름. 예: bm25-v1:kiwi"""
        ...

    async def search_keyword(
        self, search_filter: SearchFilter, query_text: str, limit: int
    ) -> list[tuple[SearchChunk, float]]:
        """필터를 통과하고 질의 단어가 있는 청크를 BM25 점수 내림차순으로 최대 limit개.
        키워드 색인을 만들지 않은 코퍼스면 빈 목록이 아니라 RetrievalNotConfigured."""
        ...

    async def fetch_by_parent(
        self, corpus: Corpus, parent_ids: list[str], *, edition: str | None = None
    ) -> list[SearchChunk]:
        """parent_id가 일치하는 청크. 넘긴 parent_ids 순서대로, 같은 조항 안에서는 페이지 순.
        edition이 None이면 판으로 거르지 않는다. 없는 parent_id는 건너뛴다."""
        ...


def _metadata_matches(metadata: dict[str, object], key: str, value: object) -> bool:
    if key not in metadata:
        return False
    wanted = value if isinstance(value, list) else [value]
    actual = metadata[key]
    actual_values = actual if isinstance(actual, list) else [actual]
    return any(item in actual_values for item in wanted)


def within_accident_window(accident_date: date, metadata: dict[str, object]) -> bool:
    """metadata의 accident_date_from ≤ 사고 일자 ≤ accident_date_to(양끝 포함, 빈 값은 열린 끝)."""
    start, end = metadata.get("accident_date_from"), metadata.get("accident_date_to")
    if start and accident_date < date.fromisoformat(start):  # type: ignore[arg-type]
        return False
    return not (end and accident_date > date.fromisoformat(end))  # type: ignore[arg-type]


def matches(chunk: SearchChunk, search_filter: SearchFilter) -> bool:
    document = chunk["document"]
    if document["corpus"] != search_filter.corpus:
        return False
    if search_filter.edition is not None and document["edition"] != search_filter.edition:
        return False
    if (
        search_filter.document_id is not None
        and document["document_id"] != search_filter.document_id
    ):
        return False
    # effective_from이 빈 문자열이면 기간 모름: 날짜 필터 통과(적재 때 경고)
    if search_filter.accident_date is not None and document["effective_from"]:
        if search_filter.accident_date < date.fromisoformat(document["effective_from"]):
            return False
        effective_to = document["effective_to"]
        if effective_to is not None and search_filter.accident_date > date.fromisoformat(
            effective_to
        ):
            return False
    metadata = chunk["metadata"]
    if search_filter.accident_window is not None and not within_accident_window(
        search_filter.accident_window, metadata
    ):
        return False
    return all(_metadata_matches(metadata, k, v) for k, v in search_filter.metadata.items())


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise EmbeddingMismatch(f"벡터 길이 불일치: {len(a)} ≠ {len(b)}")
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


class InMemoryIndex:
    """한 색인에는 한 임베딩 모델·차원·버전만 넣는다. 같은 chunk_id를 다시 넣으면 교체한다.

    keyword_corpora: BM25 통계를 만들 코퍼스. None이면 전체, 빈 목록이면 하나도 만들지 않는다.
    벡터만 쓰는 코퍼스를 빼면 적재 때 토큰화를 건너뛴다(Kiwi 적재 시간 절약).
    keyword_corpora_for(검색 설정)로 구한다.
    """

    def __init__(
        self,
        embedded: list[EmbeddedChunk] | None = None,
        *,
        tokenizer: Tokenizer | None = None,
        keyword_corpora: Iterable[Corpus] | None = None,
    ) -> None:
        self._tokenizer = tokenizer  # 키워드 색인이 처음 필요할 때 기본 토큰화를 읽는다
        if keyword_corpora is None:
            self._keyword_corpora: frozenset[str] = frozenset(get_args(Corpus))
        else:
            self._keyword_corpora = frozenset(keyword_corpora)
            unknown = self._keyword_corpora - set(get_args(Corpus))
            if unknown:
                raise ValueError(f"알 수 없는 코퍼스: {sorted(unknown)}")
        self._keyword: dict[str, BM25Index] = {}  # 코퍼스별 BM25 통계
        self._spec: EmbeddingSpec | None = None
        self._rows: dict[str, EmbeddedChunk] = {}
        self._unknown_period_documents: set[str] = set()
        self.add(embedded or [])

    @property
    def spec(self) -> EmbeddingSpec | None:
        return self._spec

    @property
    def keyword_corpora(self) -> frozenset[str]:
        """BM25 통계를 만드는 코퍼스."""
        return self._keyword_corpora

    @property
    def tokenizer(self) -> Tokenizer:
        if self._tokenizer is None:
            self._tokenizer = default_tokenizer()
        return self._tokenizer

    @property
    def keyword_version(self) -> str:
        return f"bm25-v1:{self.tokenizer.name}"

    def add(self, embedded: list[EmbeddedChunk]) -> None:
        for row in embedded:
            spec = EmbeddingSpec(
                row["embedding_model"], row["embedding_dimensions"], row["embedding_version"]
            )
            if self._spec is not None:
                check_spec(self._spec, spec, where=f"청크 {row['chunk']['chunk_id']}")
            if len(row["embedding"]) != spec.dimensions:
                raise EmbeddingMismatch(
                    f"청크 {row['chunk']['chunk_id']}: "
                    f"벡터 길이 {len(row['embedding'])} ≠ 차원 {spec.dimensions}"
                )
            self._check_period(row["chunk"]["document"])
            for key in ("accident_date_from", "accident_date_to"):  # 잘못된 날짜는 적재 때 드러낸다
                if row["chunk"]["metadata"].get(key):
                    date.fromisoformat(row["chunk"]["metadata"][key])  # type: ignore[arg-type]
            self._spec = spec
            self._rows[row["chunk"]["chunk_id"]] = row
            corpus = row["chunk"]["document"]["corpus"]
            if corpus in self._keyword_corpora:
                bm25 = self._keyword.get(corpus)
                if bm25 is None:
                    bm25 = self._keyword[corpus] = BM25Index(self.tokenizer)
                bm25.add(row["chunk"]["chunk_id"], row["chunk"]["text"])

    def _check_period(self, document: SourceDocument) -> None:
        """잘못된 날짜는 검색 때가 아니라 적재 때 드러나게 한다. 빈 effective_from은 기간 모름."""
        if not document["effective_from"]:
            if document["document_id"] not in self._unknown_period_documents:
                self._unknown_period_documents.add(document["document_id"])
                logger.warning(
                    "문서 %s의 적용 기간을 모릅니다 — 날짜 필터를 통과시킵니다",
                    document["document_id"],
                )
        else:
            date.fromisoformat(document["effective_from"])
        if document["effective_to"] is not None:
            date.fromisoformat(document["effective_to"])

    async def fetch_by_parent(
        self, corpus: Corpus, parent_ids: list[str], *, edition: str | None = None
    ) -> list[SearchChunk]:
        out: list[SearchChunk] = []
        for parent_id in dict.fromkeys(parent_ids):
            found = [
                row["chunk"]
                for row in self._rows.values()
                if row["chunk"]["parent_id"] == parent_id
                and matches(row["chunk"], SearchFilter(corpus=corpus, edition=edition))
            ]
            found.sort(key=lambda chunk: chunk["page_start"])  # 같은 페이지는 적재 순서 유지
            out += found
        return out

    async def search_keyword(
        self, search_filter: SearchFilter, query_text: str, limit: int
    ) -> list[tuple[SearchChunk, float]]:
        if search_filter.corpus not in self._keyword_corpora:
            raise RetrievalNotConfigured(
                f"D: {search_filter.corpus}는 키워드 색인을 만들지 않았습니다 — "
                "keyword_corpora에 넣거나 벡터만 쓰세요"
            )
        bm25 = self._keyword.get(search_filter.corpus)
        if bm25 is None:  # 이 코퍼스 청크가 아직 없음
            return []
        scored = [
            (self._rows[chunk_id]["chunk"], score)
            for chunk_id, score in bm25.scores(query_text).items()
            if matches(self._rows[chunk_id]["chunk"], search_filter)
        ]
        scored.sort(key=lambda item: (-item[1], item[0]["chunk_id"]))  # 동점은 chunk_id 순(결정적)
        return scored[:limit]

    async def search_vector(
        self, search_filter: SearchFilter, query_vector: list[float], limit: int
    ) -> list[tuple[SearchChunk, float]]:
        scored = [
            (row["chunk"], cosine(query_vector, row["embedding"]))
            for row in self._rows.values()
            if matches(row["chunk"], search_filter)
        ]
        scored.sort(key=lambda item: (-item[1], item[0]["chunk_id"]))  # 동점은 chunk_id 순(결정적)
        return scored[:limit]
