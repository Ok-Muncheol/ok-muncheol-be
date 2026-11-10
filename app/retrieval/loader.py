"""D: C의 코퍼스 DB 적재 파일(db_import/*.jsonl)을 읽어 InMemoryIndex와 로컬 EvidenceStore를 만든다.

Supabase(pgvector)로 옮기기 전 로컬 확인용이다. 파일은 C가 `scripts/export_corpus_index_rows.py`로
만들어 비공개로 전달한다(저장소에 올리지 않음). 테이블·칼럼 뜻은 `docs/contracts/corpus-index.md`.

    corpus = read_corpus()                          # 파일이 없으면 RetrievalNotConfigured
    configure_retrieval(
        index=build_index(corpus, keyword_corpora=keyword_corpora_for(options)),
        embedder=OpenAIEmbedder.from_env(),
        options=options,
        default_terms_edition=corpus.terms_edition(),
    )

검색 결과의 청크와 EvidenceStore.get_chunk는 같은 조인(`search_chunk`)으로 만든다.
C의 validate_hits가 두 값을 필드별로 비교하기 때문이다.
"""

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.retrieval.embedding import EmbeddingSpec, RetrievalNotConfigured
from app.retrieval.index import InMemoryIndex
from app.retrieval.keyword import Tokenizer
from app.schemas.retrieval import Corpus, EmbeddedChunk, SearchChunk, SourceDocument

ROOT = Path(__file__).resolve().parents[2]
DB_IMPORT = ROOT / "data/private/corpus-db-import/db_import"
TABLES = ("corpus_documents", "corpus_chunks", "corpus_records", "corpus_source_pages")
# SourceDocument 8개 필드. corpus_documents의 나머지 칼럼(source_name·usage_review 등)은 넣지 않는다
DOCUMENT_FIELDS: tuple[str, ...] = tuple(SourceDocument.__annotations__)


def read_handoff(root: Path = ROOT) -> dict:
    return json.loads((root / "data/private/handoff.json").read_text(encoding="utf-8"))


def handoff_embedding_spec(root: Path = ROOT) -> EmbeddingSpec:
    """질의 임베더 기대값. handoff.json은 저장소에 있어 코퍼스 파일이 없는 운영에서도 읽힌다."""
    embedding = read_handoff(root)["embedding"]
    return EmbeddingSpec(embedding["model"], embedding["dimensions"], embedding["version"])


def source_document(row: dict) -> SourceDocument:
    """corpus_documents 행 → SourceDocument(8개 필드)."""
    return {name: row[name] for name in DOCUMENT_FIELDS}  # type: ignore[return-value]


def search_chunk(row: dict, documents: dict[str, dict]) -> SearchChunk:
    """corpus_chunks 행 + corpus_documents 조인 → SearchChunk.

    raw_text·display_text·임베딩 칼럼은 뺀다.
    """
    return {
        "chunk_id": row["chunk_id"],
        "parent_id": row["parent_id"],
        "document": source_document(documents[row["document_id"]]),
        "page_start": row["page_start"],
        "page_end": row["page_end"],
        "text": row["text"],
        "metadata": copy.deepcopy(row["metadata"]),  # 색인·어댑터가 같은 사전을 나눠 쓰지 않게
    }


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as lines:
        return [json.loads(line) for line in lines if line.strip()]


@dataclass(frozen=True)
class CorpusRows:
    """db_import 네 파일의 행. 읽을 때 조인에 필요한 연결을 검사한다."""

    documents: dict[str, dict]  # document_id → corpus_documents 행
    chunks: list[dict]  # corpus_chunks 행(embedding 포함)
    records: list[dict]  # corpus_records 행
    pages: list[dict]  # corpus_source_pages 행

    def terms_edition(self) -> str:
        """약관 판. 판이 하나가 아니면 고를 수 없어 오류."""
        editions = {d["edition"] for d in self.documents.values() if d["corpus"] == "policy_terms"}
        if len(editions) != 1:
            raise ValueError(f"약관 판을 하나로 정할 수 없습니다: {sorted(editions)}")
        return editions.pop()


def read_corpus(base: Path = DB_IMPORT) -> CorpusRows:
    """db_import/*.jsonl을 읽는다.

    파일이 없으면 RetrievalNotConfigured, 행이 어긋나면 ValueError.
    """
    missing = [name for name in TABLES if not (base / f"{name}.jsonl").is_file()]
    if missing:
        raise RetrievalNotConfigured(
            f"D: 코퍼스 적재 파일이 없습니다({base}): {', '.join(missing)}"
        )
    rows = {name: _read_jsonl(base / f"{name}.jsonl") for name in TABLES}
    documents: dict[str, dict] = {}
    for doc in rows["corpus_documents"]:
        if doc["document_id"] in documents:
            raise ValueError(f"corpus_documents에 같은 document_id가 두 번: {doc['document_id']}")
        documents[doc["document_id"]] = doc
    seen: set[str] = set()
    for chunk in rows["corpus_chunks"]:
        chunk_id = chunk["chunk_id"]
        doc = documents.get(chunk["document_id"])
        if doc is None:
            raise ValueError(f"청크 {chunk_id}의 문서가 corpus_documents에 없습니다")
        if doc["corpus"] != chunk["corpus"]:
            raise ValueError(f"청크 {chunk_id}의 코퍼스가 문서와 다릅니다")
        if chunk_id in seen:  # 색인은 chunk_id 하나로 행을 찾는다
            raise ValueError(f"corpus_chunks에 같은 chunk_id가 두 번: {chunk_id}")
        seen.add(chunk_id)
    return CorpusRows(
        documents=documents,
        chunks=rows["corpus_chunks"],
        records=rows["corpus_records"],
        pages=rows["corpus_source_pages"],
    )


def embedded_chunks(
    corpus: CorpusRows, corpora: Iterable[Corpus] | None = None
) -> list[EmbeddedChunk]:
    wanted = None if corpora is None else set(corpora)
    return [
        {
            "chunk": search_chunk(row, corpus.documents),
            "embedding": row["embedding"],
            "embedding_model": row["embedding_model"],
            "embedding_dimensions": row["embedding_dimensions"],
            "embedding_version": row["embedding_version"],
        }
        for row in corpus.chunks
        if wanted is None or row["corpus"] in wanted
    ]


def build_index(
    corpus: CorpusRows,
    corpora: Iterable[Corpus] | None = None,
    *,
    tokenizer: Tokenizer | None = None,
    keyword_corpora: Iterable[Corpus] | None = None,
) -> InMemoryIndex:
    """읽은 행으로 색인을 만든다(기본은 세 코퍼스 전체).

    keyword_corpora: BM25 통계를 만들 코퍼스(None이면 전체). 벡터만 쓰는 코퍼스는 빼면 된다.
    """
    index = InMemoryIndex(tokenizer=tokenizer, keyword_corpora=keyword_corpora)
    index.add(embedded_chunks(corpus, corpora))
    return index


def load_index(
    corpora: Iterable[Corpus] | None = None,
    *,
    base: Path = DB_IMPORT,
    tokenizer: Tokenizer | None = None,
    keyword_corpora: Iterable[Corpus] | None = None,
) -> InMemoryIndex:
    """read_corpus + build_index. 어댑터도 필요하면 read_corpus 결과를 함께 쓴다(한 번만 읽기)."""
    return build_index(
        read_corpus(base), corpora, tokenizer=tokenizer, keyword_corpora=keyword_corpora
    )


def corpus_terms_edition(base: Path = DB_IMPORT) -> str:
    """corpus_documents의 약관 판(handoff.json 대신). 문서 파일만 읽는다."""
    path = base / "corpus_documents.jsonl"
    if not path.is_file():
        raise RetrievalNotConfigured(f"D: 코퍼스 적재 파일이 없습니다: {path}")
    documents = {d["document_id"]: d for d in _read_jsonl(path)}
    return CorpusRows(documents=documents, chunks=[], records=[], pages=[]).terms_edition()
