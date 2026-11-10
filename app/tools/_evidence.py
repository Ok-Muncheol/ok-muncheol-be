"""C: 세 검색 도구의 결과를 DB 색인과 대조한다. 저장소·data/private 파일을 읽지 않는다.

DB 조회는 `EvidenceStore`로 추상화한다.
D(옥석현)가 DB 연결과 함께 `configure_evidence_store`로 어댑터를 등록한다.
테이블·칼럼 계약은 `docs/contracts/corpus-index.md`(초안, D 합의 전)에 있다.

| 메서드 | 테이블 | 조회 키 |
| --- | --- | --- |
| `get_chunk` | `corpus_chunks` | `(corpus, chunk_id)` |
| `get_parent` | `corpus_records` (`is_parent`) | `(corpus, parent_id)` |
| `get_source_page` | `corpus_source_pages` | `(document_id, page)` |
| `get_related` | `corpus_records` | `(corpus, kind, record_id)` |

파일에서 하던 적재 시점 무결성 검사(중복 ID, 청크 없는 부모 등)는 적재 행 생성 단계
(`scripts/export_corpus_index_rows.py`)와 DB 제약이 맡는다. 여기서는 결과 한 건마다 출처를 확인한다.
"""

import math
from collections.abc import Callable
from typing import Protocol

from app.schemas.retrieval import EvidenceHit, SearchRequest

# SearchChunk 계약 필드. DB 행의 raw_text·display_text·임베딩 칼럼은 비교하지 않는다.
CHUNK_FIELDS = ("chunk_id", "parent_id", "document", "page_start", "page_end", "text", "metadata")


class EvidenceStore(Protocol):
    async def get_chunk(self, corpus: str, chunk_id: str) -> dict | None: ...

    async def get_parent(self, corpus: str, parent_id: str) -> dict | None: ...

    async def get_source_page(self, document_id: str, page: int) -> dict | None: ...

    async def get_related(self, corpus: str, kind: str, record_id: str) -> dict | None: ...


_store_factory: Callable[[], EvidenceStore] | None = None


def configure_evidence_store(factory: Callable[[], EvidenceStore] | None) -> None:
    """D가 DB 연결 시점에 어댑터를 등록한다. None이면 해제한다(테스트용)."""
    global _store_factory
    _store_factory = factory


def crosswalk_key(link: dict) -> str:
    """결정례 도표 대조표(`chart_mapping`) 행의 record_id. 적재 스크립트와 같은 규칙을 쓴다."""
    return f"{link['legacy_chart_id']}|{link['chart_id']}|{link['source_pdf_page']}"


async def _check_parent(store: EvidenceStore, corpus: str, chunk: dict, parent: dict) -> None:
    document_id = chunk["document"]["document_id"]
    parent_id = chunk["parent_id"]
    if parent.get("document", {}).get("document_id") != document_id:
        raise ValueError(f"Source document differs for parent_id {parent_id}")
    if chunk["page_start"] < parent["page_start"] or chunk["page_end"] > parent["page_end"]:
        raise ValueError(f"Chunk page outside parent record: {parent_id}")
    metadata = chunk["metadata"]
    if corpus == "fault_standards":
        if not parent_id.endswith(":" + parent["chart_id"]):
            raise ValueError(f"Chart ID differs from parent_id {parent_id}")
        for modifier_id in parent.get("modifier_ids", []):
            modifier = await store.get_related(corpus, "modifier", modifier_id)
            if modifier is None or modifier.get("chart_id") != parent["chart_id"]:
                raise ValueError("Chart references missing modifier IDs")
    elif corpus == "dispute_cases":
        if metadata.get("case_id") != parent["case_id"]:
            raise ValueError(f"Case ID differs from parent_id {parent_id}")
        for link in parent.get("mapping_evidence", []):
            if await store.get_related(corpus, "chart_mapping", crosswalk_key(link)) is None:
                raise ValueError("Case chart link is absent from source crosswalk")
        chart_id = parent.get("chart_id")
        if chart_id:
            if await store.get_related("fault_standards", "chart", chart_id) is None:
                raise ValueError(f"Linked chart does not exist: {parent_id}")
            if not any(
                link.get("chart_id") == chart_id for link in parent.get("mapping_evidence", [])
            ):
                raise ValueError(f"Linked chart lacks crosswalk evidence: {parent_id}")
            if metadata.get("chart_id") != chart_id:
                raise ValueError(f"Chunk chart ID differs from case record: {parent_id}")
    elif corpus == "policy_terms" and "clause_id" in parent:
        if metadata.get("clause_id") != parent["clause_id"]:
            raise ValueError(f"Clause ID differs from parent_id {parent_id}")
        if metadata.get("coverage") != parent.get("coverage"):
            raise ValueError(f"Coverage differs from clause record: {parent_id}")
        for note_id in parent.get("side_note_parent_ids", []):
            if await store.get_parent(corpus, note_id) is None:
                raise ValueError("Clause references missing source note")


def _matches(filters: dict, document: dict, parent: dict | None) -> bool:
    for name, expected in filters.items():
        if name in {"document_id", "edition"}:
            actual = document.get(name)
        else:
            actual = parent.get(name) if parent else None
        if actual != expected:
            return False
    return True


async def validate_hits(request: SearchRequest, hits: list[EvidenceHit]) -> list[EvidenceHit]:
    """색인 청크·구조 레코드·PDF 페이지와 연결되지 않은 결과를 거절한다."""
    if not isinstance(hits, list):
        raise TypeError("Search results must be a list of EvidenceHit")
    if not hits:
        return hits
    if len(hits) > request["top_k"]:
        raise ValueError("Search returned more hits than top_k")
    if _store_factory is None:
        raise RuntimeError("DB evidence store is not configured")
    store = _store_factory()
    corpus = request["corpus"]
    for hit in hits:
        if not isinstance(hit, dict) or not isinstance(hit.get("chunk"), dict):
            raise ValueError("Search hit has no source chunk")
        chunk = hit["chunk"]
        chunk_id = chunk.get("chunk_id")
        indexed = await store.get_chunk(corpus, chunk_id) if isinstance(chunk_id, str) else None
        if indexed is None:
            raise ValueError("Search hit has an unknown chunk_id")
        for field in CHUNK_FIELDS:
            if chunk.get(field) != indexed.get(field):
                raise ValueError(f"Search hit {field} differs from indexed source")
        document = indexed["document"]
        if document.get("corpus") != corpus:
            raise ValueError("Search hit belongs to another corpus")
        for page in range(indexed["page_start"], indexed["page_end"] + 1):
            source = await store.get_source_page(document["document_id"], page)
            if source is None or source.get("content_hash") != document["content_hash"]:
                raise ValueError("Search hit page is absent from parsed PDF source")
        parent_id = indexed["parent_id"]
        parent = None
        if corpus == "fault_standards" and indexed["metadata"].get("section") == (
            "common_modifier_guide"
        ):
            # 공통 수정요소 안내는 도표 행이 없다. 같은 문서의 안내 묶음 ID인지만 본다.
            if not parent_id.startswith(f"{document['document_id']}:modifier-guide:"):
                raise ValueError("Modifier guide parent ID does not match its source document")
        else:
            parent = await store.get_parent(corpus, parent_id)
            if parent is None:
                raise ValueError(f"No structured source record for parent_id {parent_id}")
            await _check_parent(store, corpus, indexed, parent)
        if not _matches(request["filters"], document, parent):
            raise ValueError("Search hit does not satisfy the requested filters")
        score = hit.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not isinstance(hit.get("retrieval_version"), str)
            or not hit["retrieval_version"]
        ):
            raise ValueError("Search hit score or retrieval_version is invalid")
    return hits
