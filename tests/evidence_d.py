"""D 테스트 전용: C의 validate_hits(비동기, EvidenceStore 기준)를 D 테스트에서 쓰기 위한 도구.

- `skip_validation`: 합성 청크 시험에서 검증을 끄는 가짜 validate_hits.
- `LocalFileEvidenceStore`: data/private/ 코퍼스 산출물을 읽어 EvidenceStore 메서드를 채운다.
  실제 코퍼스 테스트에서만 쓴다. 운영 어댑터(DB)가 아니다 — 로컬 파일 어댑터를 실제 코드에 둘지는
  C(김지은 님)와 정한다. 레코드 키 규칙은 scripts/export_corpus_index_rows.py(DB 적재 행)를 따른다.
"""

from app.tools import _evidence
from scripts.export_corpus_index_rows import FOLDERS, PARENT_FILES, PRIVATE, read

__all__ = ["FOLDERS", "LocalFileEvidenceStore", "has_corpus", "skip_validation"]


async def skip_validation(request, hits):
    return hits


def has_corpus(*corpora: str) -> bool:
    """코퍼스 원문은 저장소에 두지 않는다(#46). handoff.json은 추적되므로 청크 파일로 판단한다."""
    names = corpora or tuple(FOLDERS)
    return all(
        (PRIVATE / FOLDERS[name] / "processed" / "chunks.jsonl").is_file() for name in names
    )


class LocalFileEvidenceStore:
    """corpus_chunks·corpus_records·corpus_source_pages를 로컬 산출물로 흉내 낸다."""

    def __init__(self, corpora=None):
        self.chunks, self.parents, self.related, self.pages = {}, {}, {}, {}
        for corpus in corpora or FOLDERS:
            base = PRIVATE / FOLDERS[corpus]
            processed = base / "processed"
            for row in read(processed / "chunks.jsonl"):
                self.chunks[(corpus, row["chunk_id"])] = row
            for name, kind in PARENT_FILES[corpus]:
                for row in read(processed / name):
                    self.parents[(corpus, row["parent_id"])] = row
                    record_id = row.get("chart_id") if kind == "chart" else row["parent_id"]
                    self.related[(corpus, kind, record_id)] = row
            if corpus == "fault_standards":
                for row in read(processed / "modifiers.jsonl"):
                    self.related[(corpus, "modifier", row["modifier_id"])] = row
            elif corpus == "dispute_cases":
                for row in read(processed / "chart_mappings.jsonl"):
                    key = (corpus, "chart_mapping", _evidence.crosswalk_key(row))
                    self.related.setdefault(key, row)
            for block in read(base / "parsed" / "blocks.jsonl"):
                doc = block["document"]
                self.pages[(doc["document_id"], block["page"])] = doc["content_hash"]

    async def get_chunk(self, corpus, chunk_id):
        return self.chunks.get((corpus, chunk_id))

    async def get_parent(self, corpus, parent_id):
        return self.parents.get((corpus, parent_id))

    async def get_source_page(self, document_id, page):
        content_hash = self.pages.get((document_id, page))
        return None if content_hash is None else {"content_hash": content_hash}

    async def get_related(self, corpus, kind, record_id):
        return self.related.get((corpus, kind, record_id))
