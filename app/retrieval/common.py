"""D: C의 세 도구가 사용하는 공통 온라인 검색 진입점.

교체 지점은 configure_retrieval(index=..., embedder=...) 하나다.
연결 전에는 빈 목록이 아니라 RetrievalNotConfigured(NotImplementedError)를 낸다("근거 없음"과 구분).
"""

from app.retrieval.embedding import (
    Embedder,
    EmbeddingMismatch,
    RetrievalNotConfigured,
    configure_embedder,
    embed_query,
    get_embedder,
)
from app.retrieval.hybrid import (
    CorpusOptions,
    RetrievalOptions,
    build_filter,
    fetch_clauses,
    options_by_corpus,
    retrieve,
)
from app.retrieval.index import VectorIndex
from app.schemas.retrieval import Corpus, EvidenceHit, SearchRequest

__all__ = [
    "EmbeddingMismatch",
    "RetrievalNotConfigured",
    "configure_retrieval",
    "fetch_by_parent",
    "retrieval_options",
    "search",
]

_UNSET = object()
_index: VectorIndex | None = None
_options: dict[Corpus, RetrievalOptions] = options_by_corpus()
_default_terms_edition: str | None = None


def configure_retrieval(
    *,
    index: VectorIndex | None | object = _UNSET,
    embedder: Embedder | None | object = _UNSET,
    options: RetrievalOptions | CorpusOptions | None | object = _UNSET,
    default_terms_edition: str | None | object = _UNSET,
) -> None:
    """넘긴 값만 교체한다. None을 넘기면 연결을 끊는다.

    options: RetrievalOptions 하나면 세 코퍼스 공통, {코퍼스: RetrievalOptions}면 코퍼스별.
      사전에 없는 코퍼스와 None은 기본값(RRF). 넘길 때마다 세 코퍼스 설정을 모두 새로 정한다.
    default_terms_edition: 약관 검색에 filters["edition"]이 없을 때 쓸 판. 없으면 빈 결과.
    """
    global _index, _options, _default_terms_edition
    if options is not _UNSET:
        _options = options_by_corpus(options)  # type: ignore[arg-type]  # 잘못된 값은 먼저 거절
    if index is not _UNSET:
        _index = index  # type: ignore[assignment]
    if embedder is not _UNSET:
        configure_embedder(embedder)  # type: ignore[arg-type]
    if default_terms_edition is not _UNSET:
        _default_terms_edition = default_terms_edition  # type: ignore[assignment]


async def fetch_by_parent(
    corpus: Corpus, parent_ids: list[str], *, terms_edition: str | None = None
) -> list[EvidenceHit]:
    """조항 ID로 청크를 직접 가져온다. parent_ids 순서대로, 같은 조항은 페이지 순, score=1.0.

    임베더가 필요 없다. 판 규칙은 search와 같다(약관은 terms_edition, 없으면 기본 판, 그것도 없으면
    빈 목록). 없는 parent_id는 건너뛴다.
    """
    if _index is None:
        raise RetrievalNotConfigured("D: 색인 미연결 — configure_retrieval(index=...)")
    return await fetch_clauses(
        corpus,
        parent_ids,
        index=_index,
        terms_edition=terms_edition,
        default_terms_edition=_default_terms_edition,
    )


async def search(request: SearchRequest) -> list[EvidenceHit]:
    """코퍼스·적용기간·속성 필터 후 검색, 출처/페이지를 보존해 반환. 결과가 없으면 빈 목록.

    약관은 계약 적용판을 filters에 별도로 전달한다. 사고 일자로 대체하지 않는다.
    적용판이 없으면 default_terms_edition을 쓰고, 그것도 없으면 빈 결과다.
    필터를 자동 완화하거나 다른 코퍼스로 폴백하지 않는다.
    """
    # 모르는 코퍼스·잘못된 top_k는 연결 여부보다 먼저 ValueError
    search_filter = build_filter(request, default_terms_edition=_default_terms_edition)
    options = _options[request["corpus"]]
    if _index is None or (options.needs_vector and get_embedder() is None):
        raise RetrievalNotConfigured(
            "D: 색인·임베더 미연결 — configure_retrieval(index=..., embedder=...)"
        )
    spec = _index.spec
    # 약관 판 없음·빈 색인이면 임베딩 비용을 쓰지 않는다
    if search_filter is None or spec is None:
        return []
    query_vector = None
    if options.needs_vector:
        query_vector = await embed_query(request["text"], spec.model, spec.dimensions, spec.version)
    return await retrieve(
        request,
        query_vector,
        index=_index,
        options=options,
        default_terms_edition=_default_terms_edition,
    )


def retrieval_options(corpus: Corpus) -> RetrievalOptions:
    """지금 설정된 코퍼스의 검색 방식."""
    if corpus not in _options:
        raise ValueError(f"알 수 없는 코퍼스: {corpus}")
    return _options[corpus]
