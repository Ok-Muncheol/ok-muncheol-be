"""D: 1단계 검색(벡터·BM25·RRF) 기준선과 ③④용 후보 풀.

운영 검색 함수(app.retrieval.hybrid.retrieve)를 그대로 부른다. 질의 벡터는 무시 경로의 캐시
(evals/results/rerank_compare/query_vectors.json, {질의 문장: 벡터})에서 읽고, 기본은 오프라인이다
(캐시에 없으면 API를 부르지 않고 멈춘다).

기준선 두 가지
- "<방식>-legacy": 상위 10개 **청크**를 parent로 합친 것(10개 이하). 예전 숫자 재현 확인용
- "<방식>": 고유 parent 10개가 찰 때까지 청크를 더 가져온다. 비교표에는 이쪽을 쓴다(③④와 같은 10개)
후보 풀(candidate_parents): 기본은 RRF(CANDIDATE_MODE)의 고유 parent 50개.
RRF는 운영과 같이 벡터·BM25 각각 max(50, 요청 청크 수)개를 합친다.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from evals.rerank_compare.common import (
    CANDIDATE_MODE,
    CANDIDATE_PARENTS,
    CORPUS,
    TOP_K,
    Passage,
    PolicyParents,
)
from evals.rerank_compare.harness import RESULTS, Meter, Question

QUERY_VECTORS = RESULTS / "query_vectors.json"
MODES = ("vector", "bm25", "rrf")


class QueryVectorMissing(RuntimeError):
    """오프라인 실행인데 캐시에 없는 질의."""


@dataclass
class CachedEmbedder:
    """질의 벡터 캐시. allow_api면 없는 질의만 실제 임베더로 받고 api_calls를 센다(상한 있음)."""

    cache: dict[str, list[float]]
    inner: object | None = None  # app.retrieval.embedding.OpenAIEmbedder
    allow_api: bool = False
    max_api_calls: int = 0
    api_calls: int = 0
    added: dict[str, list[float]] = field(default_factory=dict)

    async def embed(self, text: str) -> list[float]:
        if text in self.cache:
            return self.cache[text]
        if not self.allow_api or self.inner is None:
            raise QueryVectorMissing(
                "캐시에 없는 질의입니다(오프라인). --allow-embedding-api로 받을 수 있습니다"
            )
        if self.api_calls >= self.max_api_calls:
            raise QueryVectorMissing(f"임베딩 API 상한 {self.max_api_calls}회를 넘습니다")
        self.api_calls += 1
        vector = await self.inner.embed(text)  # type: ignore[attr-defined]
        self.cache[text] = self.added[text] = vector
        return vector


def load_vector_cache(path: Path = QUERY_VECTORS) -> dict[str, list[float]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_vector_cache(cache: dict[str, list[float]], path: Path = QUERY_VECTORS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache), encoding="utf-8")


class PolicySearch:
    """약관 색인 + 질의 벡터 캐시. 한 번 만들어 여러 방식에 같이 쓴다."""

    def __init__(self, embedder: CachedEmbedder, *, tokenizer=None) -> None:
        from app.retrieval.loader import build_index, read_corpus

        self.corpus = read_corpus()
        self.index = build_index(self.corpus, [CORPUS], tokenizer=tokenizer)
        self.terms_edition = self.corpus.terms_edition()
        self.embedder = embedder
        self.parents = PolicyParents(self.corpus)
        # 청크 수를 더 늘릴 수 없는 한계
        self.chunk_count = sum(c["corpus"] == CORPUS for c in self.corpus.chunks)
        spec = self.index.spec
        self.embedding = f"{spec.model}:{spec.dimensions}:{spec.version}" if spec else None

    def describe(self) -> dict:
        return {
            "keyword_version": self.index.keyword_version,
            "embedding": self.embedding,
            "terms_edition": self.terms_edition,
        }

    async def chunks(self, question: Question, mode: str, top_k: int) -> list[dict]:
        """운영 retrieve 그대로: 필터(사고일·판·document_id) → 방식 → 상위 top_k 청크."""
        from app.retrieval.hybrid import RetrievalOptions, retrieve

        if mode not in MODES:
            raise ValueError(f"알 수 없는 방식: {mode}")
        request = {
            "corpus": CORPUS,
            "text": question.text,
            "accident_date": question.accident_date,
            "filters": dict(question.filters),
            "top_k": top_k,
        }
        vector = await self.embedder.embed(question.text) if mode != "bm25" else None
        hits = await retrieve(
            request,  # type: ignore[arg-type]
            vector,
            index=self.index,
            options=RetrievalOptions(mode=mode),  # type: ignore[arg-type]
            default_terms_edition=self.terms_edition,
        )
        return [hit["chunk"] for hit in hits]  # type: ignore[misc]

    async def unique_parents(self, question: Question, mode: str, n: int) -> list[str]:
        """고유 parent n개가 찰 때까지 청크 수를 두 배씩 늘린다. 후보가 모자라면 있는 만큼."""
        top_k = n
        while True:
            chunks = await self.chunks(question, mode, top_k)
            parents = list(dict.fromkeys(c["parent_id"] for c in chunks))
            if len(parents) >= n or len(chunks) < top_k or top_k >= self.chunk_count:
                return parents[:n]
            top_k *= 2

    async def candidate_parents(
        self, question: Question, mode: str = CANDIDATE_MODE, n: int = CANDIDATE_PARENTS
    ) -> list[str]:
        """③④ 재정렬 후보: 1단계 검색의 고유 parent n개(기본 RRF 50개)."""
        return await self.unique_parents(question, mode, n)

    async def candidate_passages(
        self, question: Question, mode: str = CANDIDATE_MODE, n: int = CANDIDATE_PARENTS
    ) -> list[Passage]:
        """후보 parent의 모든 청크를 모델 입력 글로. 점수를 매긴 뒤 common.max_p로 parent 순위."""
        out: list[Passage] = []
        for parent_id in await self.candidate_parents(question, mode, n):
            out += self.parents.passages(parent_id)
        return out

    def legacy_retriever(self, mode: str):
        async def retrieve(question: Question, meter: Meter) -> list[str]:
            chunks = await self.chunks(question, mode, TOP_K)
            return list(dict.fromkeys(c["parent_id"] for c in chunks))

        return retrieve

    def parent_retriever(self, mode: str):
        async def retrieve(question: Question, meter: Meter) -> list[str]:
            return await self.unique_parents(question, mode, TOP_K)

        return retrieve


def baseline_methods(search: PolicySearch) -> dict[str, object]:
    """방식 이름 → 끼우는 함수. 비교표용(<방식>)과 재현 확인용(<방식>-legacy)."""
    methods: dict[str, object] = {}
    for mode in MODES:
        methods[mode] = search.parent_retriever(mode)
        methods[f"{mode}-legacy"] = search.legacy_retriever(mode)
    return methods
