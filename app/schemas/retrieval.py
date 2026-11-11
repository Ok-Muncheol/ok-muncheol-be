"""C 소유, D 공동 검토: 오프라인 색인과 온라인 검색의 경계."""

from typing import Literal, NotRequired, TypedDict

Corpus = Literal["fault_standards", "dispute_cases", "policy_terms"]


class SourceDocument(TypedDict):
    document_id: str
    corpus: Corpus
    source_uri: str
    edition: str
    effective_from: str
    effective_to: str | None
    content_hash: str
    parser_version: str
    # 원문에서 확인한 발행월 "YYYY-MM"(판권면 등). 시행일과 다르다. 확인 못 했거나
    # 이전 파일·합성 행이면 None이거나 키가 없다. 판단 쪽이 발행월 이전 사고를 막는 데 쓴다
    published_month: NotRequired[str | None]


class ParsedBlock(TypedDict):
    document: SourceDocument
    page: int
    kind: Literal["text", "table", "heading"]
    text: str


class SearchChunk(TypedDict):
    chunk_id: str
    parent_id: str
    document: SourceDocument
    page_start: int
    page_end: int
    text: str
    metadata: dict[str, object]


class EmbeddedChunk(TypedDict):
    chunk: SearchChunk
    embedding: list[float]
    embedding_model: str
    embedding_dimensions: int
    embedding_version: str


class SearchQuery(TypedDict):
    text: str
    accident_date: str
    filters: dict[str, object]
    top_k: int


class SearchRequest(SearchQuery):
    corpus: Corpus


class EvidenceHit(TypedDict):
    chunk: SearchChunk
    score: float
    retrieval_version: str
