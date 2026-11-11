"""D: C의 EvidenceStore(`app/tools/_evidence.py`)를 db_import 파일로 채우는 로컬 어댑터.

DB 색인 어댑터가 오기 전 로컬 개발용이다. 김지은 님(C)과 합의한 조건:
- db_import/*.jsonl을 읽는다(`app.retrieval.loader.read_corpus`).
- APP_ENV=local이고 Vercel이 아닐 때만 등록한다. 운영에서는 등록하지 않는다.
- 파일이 없으면 등록하지 않는다 → validate_hits는 지금처럼 "not configured"로 실패(fail-closed).
- get_chunk는 검색 색인과 같은 조인(`search_chunk`)으로 SearchChunk를 만든다.

| 메서드 | 행 | 키 | 반환 |
| --- | --- | --- | --- |
| `get_chunk` | corpus_chunks + corpus_documents | (corpus, chunk_id) | SearchChunk |
| `get_parent` | corpus_records where is_parent | (corpus, parent_id) | body |
| `get_source_page` | corpus_source_pages | (document_id, page) | {"content_hash"} |
| `get_related` | corpus_records | (corpus, kind, record_id) | body |
"""

import os
from collections.abc import Mapping

from app.retrieval.loader import CorpusRows, search_chunk
from app.schemas.retrieval import SearchChunk
from app.tools._evidence import configure_evidence_store


class LocalCorpusEvidenceStore:
    """읽은 행을 메모리 사전으로 들고 있는다.

    색인과 다른 객체로 만들어 검색 결과를 고쳐도 검증 기준은 바뀌지 않는다.
    """

    def __init__(self, corpus: CorpusRows) -> None:
        self._chunks: dict[tuple[str, str], SearchChunk] = {
            (row["corpus"], row["chunk_id"]): search_chunk(row, corpus.documents)
            for row in corpus.chunks
        }
        self._parents: dict[tuple[str, str], dict] = {}
        self._related: dict[tuple[str, str, str], dict] = {}
        for record in corpus.records:
            self._related[(record["corpus"], record["kind"], record["record_id"])] = record["body"]
            if record["is_parent"]:
                self._parents[(record["corpus"], record["parent_id"])] = record["body"]
        self._pages: dict[tuple[str, int], str] = {
            (page["document_id"], page["page"]): page["content_hash"] for page in corpus.pages
        }

    async def get_chunk(self, corpus: str, chunk_id: str) -> dict | None:
        return self._chunks.get((corpus, chunk_id))  # type: ignore[return-value]

    async def get_parent(self, corpus: str, parent_id: str) -> dict | None:
        return self._parents.get((corpus, parent_id))

    async def get_source_page(self, document_id: str, page: int) -> dict | None:
        content_hash = self._pages.get((document_id, page))
        return None if content_hash is None else {"content_hash": content_hash}

    async def get_related(self, corpus: str, kind: str, record_id: str) -> dict | None:
        return self._related.get((corpus, kind, record_id))


def local_store_allowed(env: Mapping[str, str] | None = None) -> bool:
    """APP_ENV=local이고 Vercel이 아닐 때만. 규칙은 app/config.py의 로컬 인증 우회와 같다."""
    env = os.environ if env is None else env
    on_vercel = bool(env.get("VERCEL") or env.get("VERCEL_ENV"))
    return env.get("APP_ENV", "production") == "local" and not on_vercel


def local_evidence_store(
    corpus: CorpusRows, env: Mapping[str, str] | None = None
) -> LocalCorpusEvidenceStore | None:
    """로컬이면 어댑터를 만들어 돌려주고(등록은 하지 않음), 운영이면 None.

    연결 함수가 다른 준비를 모두 끝낸 뒤 한꺼번에 등록할 수 있게 만들기와 등록을 나눈다.
    """
    return LocalCorpusEvidenceStore(corpus) if local_store_allowed(env) else None


def register_local_evidence_store(corpus: CorpusRows, env: Mapping[str, str] | None = None) -> bool:
    """로컬이면 어댑터를 등록하고 True. 운영이면 아무것도 바꾸지 않고 False."""
    store = local_evidence_store(corpus, env)
    if store is None:
        return False
    configure_evidence_store(lambda: store)
    return True
