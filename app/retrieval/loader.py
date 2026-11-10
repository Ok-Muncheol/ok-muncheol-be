"""D: C의 색인 산출물(data/private/handoff.json이 가리키는 파일)을 InMemoryIndex로 읽는다.

Supabase(pgvector)로 옮기기 전 확인용이다.

    options = {"policy_terms": RetrievalOptions(mode="bm25")}  # 빠진 코퍼스는 RRF
    configure_retrieval(
        index=load_index(keyword_corpora=keyword_corpora_for(options)),
        embedder=OpenAIEmbedder.from_env(),
        options=options,
        default_terms_edition=handoff_terms_edition(),
    )
"""

import json
from collections.abc import Iterable
from pathlib import Path

from app.retrieval.embedding import EmbeddingSpec
from app.retrieval.index import InMemoryIndex
from app.retrieval.keyword import Tokenizer
from app.schemas.retrieval import Corpus, EmbeddedChunk

ROOT = Path(__file__).resolve().parents[2]
_ROLE_CHUNKS = "searchable chunks"
_ROLE_VECTORS = "precomputed document vectors"


def read_handoff(root: Path = ROOT) -> dict:
    return json.loads((root / "data/private/handoff.json").read_text(encoding="utf-8"))


def handoff_embedding_spec(root: Path = ROOT) -> EmbeddingSpec:
    embedding = read_handoff(root)["embedding"]
    return EmbeddingSpec(embedding["model"], embedding["dimensions"], embedding["version"])


def handoff_terms_edition(root: Path = ROOT) -> str:
    """handoff.json의 약관 판 문자열을 그대로 돌려준다. 판이 하나가 아니면 고를 수 없어 오류."""
    terms = next(c for c in read_handoff(root)["corpora"] if c["corpus"] == "policy_terms")
    editions = {d["edition"] for d in terms["documents"]}
    if len(editions) != 1:
        raise ValueError(f"약관 판을 하나로 정할 수 없습니다: {sorted(editions)}")
    return editions.pop()


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as lines:
        return [json.loads(line) for line in lines if line.strip()]


def _load_corpus(entry: dict, root: Path) -> list[EmbeddedChunk]:
    paths = {f["role"]: root / f["path"] for f in entry["files"]}
    chunks = _read_jsonl(paths[_ROLE_CHUNKS])
    rows = _read_jsonl(paths[_ROLE_VECTORS])
    corpus = entry["corpus"]
    if len(chunks) != entry["chunks"] or len(rows) != entry["vectors"]:
        raise ValueError(f"{corpus}: handoff.json과 파일의 청크·벡터 개수가 다릅니다")
    by_id = {c["chunk_id"]: c for c in chunks}
    if len(by_id) != len(chunks) or {r["chunk"]["chunk_id"] for r in rows} != set(by_id):
        raise ValueError(f"{corpus}: chunks.jsonl과 embeddings.jsonl의 chunk_id가 다릅니다")
    for row in rows:
        if row["chunk"] != by_id[row["chunk"]["chunk_id"]]:
            raise ValueError(
                f"{corpus}: 청크 {row['chunk']['chunk_id']}가 임베딩 이후 바뀌었습니다"
            )
        if row["chunk"]["document"]["corpus"] != corpus:
            raise ValueError(f"{corpus}: 다른 코퍼스 청크가 섞였습니다: {row['chunk']['chunk_id']}")
    return rows


def load_index(
    corpora: Iterable[Corpus] | None = None,
    *,
    root: Path = ROOT,
    tokenizer: Tokenizer | None = None,
    keyword_corpora: Iterable[Corpus] | None = None,
) -> InMemoryIndex:
    """handoff.json의 코퍼스(기본은 전체)를 읽어 색인을 만든다. 파일이 어긋나면 ValueError.

    keyword_corpora: BM25 통계를 만들 코퍼스(None이면 전체). 벡터만 쓰는 코퍼스는 빼면 된다:
    load_index(keyword_corpora=keyword_corpora_for(options))
    """
    wanted = None if corpora is None else set(corpora)
    index = InMemoryIndex(tokenizer=tokenizer, keyword_corpora=keyword_corpora)
    for entry in read_handoff(root)["corpora"]:
        if wanted is None or entry["corpus"] in wanted:
            index.add(_load_corpus(entry, root))
    return index
