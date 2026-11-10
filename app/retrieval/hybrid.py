"""D: 코퍼스별 검색 전략. 재순위는 하지 않는다.

지금 동작: 구조 필터 → 코퍼스별 방식(벡터·BM25·RRF, 기본 RRF) → top_k.
코퍼스별 필터
- fault_standards: effective_from ≤ 사고 일자 ≤ effective_to (양끝 포함).
  effective_from이 빈 문자열이면 기간 모름으로 보고 통과(적재 때 경고 로그).
  사고 일자가 빈 문자열이면 날짜 필터 없음(C 도구 규칙: ISO 날짜 또는 빈 값)
- dispute_cases: 사고 일자 필터 없음(사고 이후 결정도 참고 대상).
  filters의 chart_id 등은 metadata로 거른다
- policy_terms: filters의 edition = document.edition. 사고 일자로 대체 금지.
  edition이 없거나 None이면 설정한 기본 판(default_terms_edition)을 쓴다.
  기본 판도 없으면 판이 섞이지 않게 빈 결과(경고 로그)
공통: filters의 document_id는 document.document_id와 비교, 나머지는 metadata와 비교한다
(필터 값이 목록이면 그중 하나와 일치, metadata 값이 목록이면 그 안에 포함되면 통과).
약관 주 조항만 쓸지는 호출하는 쪽이 정한다(retrieval_role 필터). 여기서 자동으로 거르지 않는다.
C 도구(app/tools)는 허용한 필터 키와 문자열 값만 받으므로, retrieval_role·목록 값은 이 함수를
직접 부를 때만 쓸 수 있다.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal, get_args

from app.retrieval.index import SearchFilter, VectorIndex
from app.schemas.retrieval import Corpus, EvidenceHit, SearchChunk, SearchRequest

logger = logging.getLogger(__name__)

STRATEGY_VERSION = "vector-v1"
RRF_VERSION = "rrf-v1"
DIRECT_VERSION = "direct-v1"
_EDITION_KEY = "edition"
_DOCUMENT_KEY = "document_id"
_DATE_FILTERED: tuple[Corpus, ...] = ("fault_standards",)


class CorpusMixError(RuntimeError):
    """색인 구현이 요청 코퍼스 밖의 청크를 돌려줌."""


RetrievalMode = Literal["vector", "bm25", "rrf"]
CORPORA: tuple[Corpus, ...] = get_args(Corpus)


@dataclass(frozen=True)
class RetrievalOptions:
    """검색 방식. 기본은 RRF(벡터 + BM25).

    mode: "vector"(벡터만), "bm25"(BM25만, 임베더 불필요), "rrf"(벡터와 BM25를 RRF로 합침).
    재순위는 아직 없다. 켜면 조용히 무시하지 않고 설정 시점에 멈춘다.
    """

    mode: RetrievalMode = "rrf"
    use_rerank: bool = False
    rrf_k: int = 60  # RRF 점수 1/(k + 순위)의 k. 평가셋에 맞춰 조정하지 않은 기본값
    candidate_limit: int = 50  # RRF에 넣는 벡터·BM25 각각의 후보 수(top_k보다 작으면 top_k)

    def __post_init__(self) -> None:
        if self.use_rerank:
            raise NotImplementedError("D: 재순위는 아직 구현하지 않음")
        if self.mode not in get_args(RetrievalMode):
            raise ValueError(f"알 수 없는 검색 방식: {self.mode}")
        if self.rrf_k < 1 or self.candidate_limit < 1:
            raise ValueError("rrf_k와 candidate_limit는 1 이상이어야 합니다")

    @property
    def needs_vector(self) -> bool:
        return self.mode != "bm25"

    @property
    def needs_keyword(self) -> bool:
        return self.mode != "vector"


CorpusOptions = Mapping[Corpus, RetrievalOptions]


def options_by_corpus(
    options: RetrievalOptions | CorpusOptions | None = None,
) -> dict[Corpus, RetrievalOptions]:
    """하나를 넘기면 세 코퍼스 공통, 사전을 넘기면 코퍼스별. 빠진 코퍼스는 기본값(RRF)."""
    if options is None or isinstance(options, RetrievalOptions):
        common = options or RetrievalOptions()
        return {corpus: common for corpus in CORPORA}
    unknown = set(options) - set(CORPORA)
    if unknown:
        raise ValueError(f"알 수 없는 코퍼스: {sorted(unknown)}")
    for corpus, value in options.items():
        if not isinstance(value, RetrievalOptions):
            raise TypeError(f"{corpus}: RetrievalOptions가 아닙니다: {value!r}")
    return {corpus: options.get(corpus, RetrievalOptions()) for corpus in CORPORA}


def keyword_corpora_for(options: RetrievalOptions | CorpusOptions | None = None) -> set[Corpus]:
    """BM25 통계가 필요한 코퍼스(BM25·RRF를 쓰는 코퍼스). load_index(keyword_corpora=)에 넘긴다."""
    return {corpus for corpus, value in options_by_corpus(options).items() if value.needs_keyword}


def _resolve_edition(
    corpus: str, terms_edition: object, default_terms_edition: str | None
) -> object:
    """약관은 판이 없으면 기본 판을 쓴다. 다른 코퍼스에는 기본 판을 적용하지 않는다."""
    if terms_edition is None and corpus == "policy_terms":
        return default_terms_edition
    return terms_edition


def _warn_no_edition(corpus: str) -> None:
    logger.warning(
        "%s 검색에 %s 없음 — 판이 섞이지 않게 빈 결과를 돌려줍니다", corpus, _EDITION_KEY
    )


def _check_corpus(corpus: str) -> None:
    if corpus not in get_args(Corpus):
        raise ValueError(f"알 수 없는 코퍼스: {corpus}")


def build_filter(
    request: SearchRequest, *, default_terms_edition: str | None = None
) -> SearchFilter | None:
    """요청을 구조 필터로 바꾼다(요청 값은 그대로). 조건을 만족할 수 없으면(약관 판 없음) None."""
    corpus = request["corpus"]
    _check_corpus(corpus)
    if request["top_k"] <= 0:
        raise ValueError(f"top_k는 1 이상이어야 합니다: {request['top_k']}")
    filters = dict(request["filters"])
    edition = _resolve_edition(corpus, filters.pop(_EDITION_KEY, None), default_terms_edition)
    document_id = filters.pop(_DOCUMENT_KEY, None)
    if edition is None and (corpus == "policy_terms" or _EDITION_KEY in request["filters"]):
        _warn_no_edition(corpus)
        return None
    raw_date = request["accident_date"]
    accident_date = date.fromisoformat(raw_date) if raw_date and corpus in _DATE_FILTERED else None
    return SearchFilter(
        corpus=corpus,
        accident_date=accident_date,
        edition=edition,  # type: ignore[arg-type]
        document_id=document_id,  # type: ignore[arg-type]
        metadata=filters,
    )


def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[SearchChunk, float]]], *, k: int
) -> list[tuple[SearchChunk, float]]:
    """순위만 써서 합친다: 점수 = Σ 1/(k + 순위). 순위는 1부터. 동점은 chunk_id 순(결정적)."""
    chunks: dict[str, SearchChunk] = {}
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (chunk, _) in enumerate(ranked, 1):
            chunk_id = chunk["chunk_id"]
            chunks[chunk_id] = chunk
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (k + rank)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return [(chunks[chunk_id], scores[chunk_id]) for chunk_id in ordered]


async def fetch_clauses(
    corpus: Corpus,
    parent_ids: list[str],
    *,
    index: VectorIndex,
    terms_edition: str | None = None,
    default_terms_edition: str | None = None,
) -> list[EvidenceHit]:
    """parent_id로 직접 조회한다(임베딩 없음). 판 규칙은 search와 같다. 점수는 항상 1.0."""
    _check_corpus(corpus)
    edition = _resolve_edition(corpus, terms_edition, default_terms_edition)
    if edition is None and corpus == "policy_terms":
        _warn_no_edition(corpus)
        return []
    spec = index.spec
    if spec is None or not parent_ids:
        return []
    version = f"{DIRECT_VERSION}:{spec.model}:{spec.version}"
    chunks = await index.fetch_by_parent(corpus, parent_ids, edition=edition)  # type: ignore[arg-type]
    hits: list[EvidenceHit] = []
    for chunk in chunks:
        found = chunk["document"]["corpus"]
        if found != corpus:
            raise CorpusMixError(f"{corpus} 조회에 {found} 청크가 섞임: {chunk['chunk_id']}")
        hits.append({"chunk": chunk, "score": 1.0, "retrieval_version": version})
    return hits


async def retrieve(
    request: SearchRequest,
    query_vector: list[float] | None,
    *,
    index: VectorIndex,
    options: RetrievalOptions | None = None,
    default_terms_edition: str | None = None,
) -> list[EvidenceHit]:
    """필터 → 벡터·BM25·RRF → top_k. 청크(document·페이지·parent_id)는 그대로 돌려준다.

    TODO(D·C): 재순위는 평가 후 여기서 결합한다.
    """
    options = options or RetrievalOptions()
    search_filter = build_filter(request, default_terms_edition=default_terms_edition)
    spec = index.spec
    if search_filter is None or spec is None:
        return []
    top_k = request["top_k"]
    if options.needs_keyword and not request["text"].strip():
        raise ValueError("빈 질의는 검색할 수 없습니다")
    if options.mode == "rrf":
        limit = max(options.candidate_limit, top_k)
        version = (
            f"{RRF_VERSION}:k{options.rrf_k}:{spec.model}:{spec.version}:{index.keyword_version}"
        )
        rows = reciprocal_rank_fusion(
            [
                await index.search_vector(search_filter, query_vector, limit),
                await index.search_keyword(search_filter, request["text"], limit),
            ],
            k=options.rrf_k,
        )
    elif options.mode == "bm25":
        version = index.keyword_version
        rows = await index.search_keyword(search_filter, request["text"], top_k)
    else:
        version = f"{STRATEGY_VERSION}:{spec.model}:{spec.version}"
        rows = await index.search_vector(search_filter, query_vector, top_k)
    hits: list[EvidenceHit] = []
    for chunk, score in rows[:top_k]:
        found = chunk["document"]["corpus"]
        if found != request["corpus"]:
            raise CorpusMixError(
                f"{request['corpus']} 검색에 {found} 청크가 섞임: {chunk['chunk_id']}"
            )
        hits.append({"chunk": chunk, "score": score, "retrieval_version": version})
    return hits
