"""D 테스트 전용: C의 validate_hits(비동기, EvidenceStore 기준)를 D 테스트에서 쓰기 위한 도구.

- `skip_validation`: 합성 청크 시험에서 검증을 끄는 가짜 validate_hits.
- `has_corpus`: 실제 코퍼스 적재 파일(db_import)이 이 PC에 있는지.
  없으면 실제 코퍼스 테스트는 건너뛴다.
실제 코퍼스 검증에는 운영 코드의 로컬 어댑터(`app.retrieval.evidence_store`)를 쓴다.
"""

from app.retrieval.loader import DB_IMPORT, TABLES

__all__ = ["has_corpus", "skip_validation"]


async def skip_validation(request, hits):
    return hits


def has_corpus() -> bool:
    """코퍼스 원문은 저장소에 두지 않는다(#46). db_import 네 파일이 모두 있어야 한다."""
    return all((DB_IMPORT / f"{name}.jsonl").is_file() for name in TABLES)
