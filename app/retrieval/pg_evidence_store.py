"""B(통합): C의 EvidenceStore(`app/tools/_evidence.py`)를 DB 색인으로 채우는 어댑터.

계약은 `docs/contracts/corpus-index.md` 「검증 어댑터」다.
적재판 하나(`CORPUS_DB_RELEASE`)만 읽는다.

| 메서드 | 표 | 키 | 반환 |
| --- | --- | --- | --- |
| `get_chunk` | corpus_chunks + corpus_documents | (corpus, chunk_id) | SearchChunk |
| `get_parent` | corpus_records where is_parent | (corpus, parent_id) | body |
| `get_source_page` | corpus_source_pages | (document_id, page) | {"content_hash"} |
| `get_related` | corpus_records | (corpus, kind, record_id) | body |

`validate_hits`는 결과 한 건마다 이 메서드를 열 번 넘게 부른다(청크·페이지·부모·수정요소). 도표 후보
검색은 100건을 검증하므로 부를 때마다 DB에 가면 천 번이 넘는다. 그래서 처음 필요할 때 그 코퍼스의
행을 한 번에 읽어 두고(벡터는 읽지 않는다) 그 뒤로는 메모리에서 답한다. 판은 넣은 뒤 고치지 않으므로
읽어 둔 값이 낡지 않는다.

청크는 검색 색인과 같은 조인(`loader.search_chunk`)으로 만들되 색인과 다른 객체다. 검색 결과를
고쳐도 검증 기준은 바뀌지 않는다.
"""

from app import db
from app.retrieval.pg_index import Connect, CorpusRelease, load_corpus_chunks
from app.schemas.retrieval import SearchChunk


class PgEvidenceStore:
    def __init__(self, release: CorpusRelease, *, connect: Connect | None = None) -> None:
        self._release = release
        self._connect: Connect = connect or db.connect
        self._chunks: dict[str, dict[str, SearchChunk]] = {}  # 코퍼스 → chunk_id → 청크
        self._parents: dict[str, dict[str, dict]] = {}  # 코퍼스 → parent_id → body
        self._related: dict[str, dict[tuple[str, str], dict]] = {}  # 코퍼스 → (kind, id) → body
        self._pages: dict[tuple[str, int], str] | None = None

    async def _load_records(self, corpus: str) -> None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "select kind, record_id, parent_id, is_parent, body from public.corpus_records"
                " where release = %(release)s and corpus = %(corpus)s order by seq",
                {"release": self._release.name, "corpus": corpus},
            )
            rows = await cursor.fetchall()
        parents: dict[str, dict] = {}
        related: dict[tuple[str, str], dict] = {}
        for row in rows:
            related[(row["kind"], row["record_id"])] = row["body"]
            if row["is_parent"]:
                parents[row["parent_id"]] = row["body"]
        self._related[corpus] = related
        self._parents[corpus] = parents  # 두 사전을 다 채운 뒤에 넣어야 읽는 쪽이 반만 보지 않는다

    async def get_chunk(self, corpus: str, chunk_id: str) -> dict | None:
        chunks = self._chunks.get(corpus)
        if chunks is None:
            loaded = await load_corpus_chunks(self._connect, self._release, corpus)
            chunks = self._chunks[corpus] = {chunk["chunk_id"]: chunk for chunk in loaded}
        return chunks.get(chunk_id)  # type: ignore[return-value]

    async def get_parent(self, corpus: str, parent_id: str) -> dict | None:
        if corpus not in self._parents:
            await self._load_records(corpus)
        return self._parents[corpus].get(parent_id)

    async def get_source_page(self, document_id: str, page: int) -> dict | None:
        if self._pages is None:
            async with self._connect() as conn:
                cursor = await conn.execute(
                    "select document_id, page, content_hash from public.corpus_source_pages"
                    " where release = %(release)s",
                    {"release": self._release.name},
                )
                rows = await cursor.fetchall()
            self._pages = {(row["document_id"], row["page"]): row["content_hash"] for row in rows}
        content_hash = self._pages.get((document_id, page))
        return None if content_hash is None else {"content_hash": content_hash}

    async def get_related(self, corpus: str, kind: str, record_id: str) -> dict | None:
        if corpus not in self._parents:
            await self._load_records(corpus)
        return self._related[corpus].get((kind, record_id))
