"""D: 온라인 질의 임베딩. 문서 임베딩은 C의 ingestion/embed.py 소유.

임베더는 configure_embedder(common.configure_retrieval)로 바꿔 끼운다.
색인과 질의의 모델·차원·버전이 다르면 코사인 점수가 의미 없으므로 EmbeddingMismatch로 멈춘다.
OpenAIEmbedder는 C의 문서 임베딩과 같은 모델·차원·버전으로 질의를 임베딩한다. 키는 저장하지 않고
OpenAI 클라이언트가 환경 변수에서 직접 읽는다. 오류 메시지에도 키가 들어가지 않게 한다.
"""

import hashlib
import math
import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.ingestion.embed import validate_vector

ROOT = Path(__file__).resolve().parents[2]
_INSTALL_HINT = 'py -3.12 -m pip install -e ".[embedding]"로 설치하세요'


class RetrievalNotConfigured(NotImplementedError):
    """색인·임베더 미연결. 빈 결과("근거 없음")와 구분하려고 NotImplementedError로 둔다."""


class EmbeddingMismatch(ValueError):
    """색인과 질의의 임베딩 모델·차원·버전 불일치."""


class EmbeddingRequestFailed(RuntimeError):
    """임베딩 API 호출 실패. 메시지에는 오류 종류만 담고 원본 예외를 연결하지 않는다."""


@dataclass(frozen=True)
class EmbeddingSpec:
    model: str
    dimensions: int
    version: str


class Embedder(Protocol):
    """질의 임베더. C의 문서 임베딩과 같은 모델·차원·버전이어야 한다."""

    model: str
    dimensions: int
    version: str

    async def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class FakeEmbedder:
    """테스트용 가짜 임베더. 글자 2-gram 해시 벡터(결정적, 외부 패키지 없음). 운영에 쓰지 않는다."""

    model: str = "fake-hash"
    dimensions: int = 64
    version: str = "test"

    async def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        compact = "".join(text.split())
        for i in range(max(len(compact) - 1, 0)):
            digest = hashlib.sha256(compact[i : i + 2].encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


def _openai_client_factory() -> Callable[[], object]:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RetrievalNotConfigured(f"openai 패키지가 없습니다. {_INSTALL_HINT}") from None
    return lambda: AsyncOpenAI(timeout=90, max_retries=4)


class OpenAIEmbedder:
    """OpenAI 질의 임베더. 같은 질문은 메모리에 캐시한다(가장 오래 안 쓴 것부터 버림)."""

    def __init__(
        self,
        *,
        model: str,
        dimensions: int,
        version: str,
        client_factory: Callable[[], object],
        cache_size: int = 1024,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self.version = version
        self._client_factory = client_factory
        self._cache_size = cache_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    def __repr__(self) -> str:
        return f"OpenAIEmbedder(model={self.model!r}, dimensions={self.dimensions})"

    @classmethod
    def from_env(
        cls,
        *,
        expected: EmbeddingSpec | None = None,
        dotenv_path: Path | None = None,
        client_factory: Callable[[], object] | None = None,
    ) -> "OpenAIEmbedder":
        """.env와 환경 변수에서 읽는다. expected(기본은 handoff.json)와 다르면 EmbeddingMismatch."""
        try:
            from dotenv import load_dotenv
        except ImportError:
            raise RetrievalNotConfigured(f"python-dotenv가 없습니다. {_INSTALL_HINT}") from None
        load_dotenv(dotenv_path or ROOT / ".env", override=False)
        if not os.environ.get("OPENAI_API_KEY"):
            raise RetrievalNotConfigured(
                ".env에 OPENAI_API_KEY가 없습니다. 키 값은 출력하지 않습니다."
            )
        model = os.environ.get("OPENAI_EMBEDDING_MODEL")
        raw_dimensions = os.environ.get("OPENAI_EMBEDDING_DIMENSIONS")
        if not model or not raw_dimensions:
            raise RetrievalNotConfigured(
                ".env에 OPENAI_EMBEDDING_MODEL, OPENAI_EMBEDDING_DIMENSIONS가 필요합니다."
            )
        try:
            dimensions = int(raw_dimensions)
        except ValueError:
            raise EmbeddingMismatch("OPENAI_EMBEDDING_DIMENSIONS는 정수여야 합니다.") from None
        if expected is None:
            from app.retrieval.loader import handoff_embedding_spec  # 순환 import 방지

            expected = handoff_embedding_spec()
        # 버전은 환경 변수에 없으므로 기대값을 쓴다. 모델·차원이 다르면 여기서 멈춘다.
        check_spec(
            expected, EmbeddingSpec(model, dimensions, expected.version), where="OPENAI_EMBEDDING_*"
        )
        return cls(
            model=model,
            dimensions=dimensions,
            version=expected.version,
            client_factory=client_factory or _openai_client_factory(),
        )

    async def embed(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            self._cache.move_to_end(text)
            return list(cached)
        failure = None
        rows: list[object] = []
        try:
            async with self._client_factory() as client:  # type: ignore[attr-defined]
                response = await client.embeddings.create(
                    model=self.model,
                    dimensions=self.dimensions,
                    input=[text],
                    encoding_format="float",
                )
            rows = list(response.data)
        except Exception as exc:  # 원문에 키 일부가 섞일 수 있어 오류 종류만 남긴다
            failure = type(exc).__name__
        if failure:  # except 밖에서 올려 원본 예외가 __context__에 남지 않게 한다
            raise EmbeddingRequestFailed(f"임베딩 요청 실패: {failure}")
        if len(rows) != 1:
            raise EmbeddingRequestFailed(f"임베딩 응답 개수 {len(rows)} ≠ 1")
        vector = list(rows[0].embedding)  # type: ignore[attr-defined]
        validate_vector(vector, self.dimensions)
        self._cache[text] = vector
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return list(vector)


_embedder: Embedder | None = None


def configure_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


def get_embedder() -> Embedder | None:
    return _embedder


def check_spec(expected: EmbeddingSpec, actual: EmbeddingSpec, *, where: str) -> None:
    if expected != actual:
        raise EmbeddingMismatch(f"{where}: 임베딩 불일치 — 색인 {expected}, 입력 {actual}")


async def embed_query(text: str, model: str, dimensions: int, version: str) -> list[float]:
    """model·dimensions·version은 색인 값. 임베더나 반환 벡터 길이가 다르면 EmbeddingMismatch."""
    if _embedder is None:
        raise RetrievalNotConfigured("D: 질의 임베더 미연결 — configure_retrieval(embedder=...)")
    if not text.strip():
        raise ValueError("빈 질의는 검색할 수 없습니다")
    check_spec(
        EmbeddingSpec(model, dimensions, version),
        EmbeddingSpec(_embedder.model, _embedder.dimensions, _embedder.version),
        where="질의 임베더",
    )
    vector = await _embedder.embed(text)
    if len(vector) != dimensions:
        raise EmbeddingMismatch(f"질의 벡터 길이 {len(vector)} ≠ 색인 차원 {dimensions}")
    return vector
