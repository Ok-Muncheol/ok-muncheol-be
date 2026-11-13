# 코퍼스 색인 DB 계약 (초안 — 김지은 작성, 옥석현 합의 필요)

저장 위치: 원본 PDF는 비공개 Storage, 중간 파일·평가셋은 로컬 `data/private/`, 검색·검증은 DB.
Git에는 코드·`data/corpora/*/manifest.json`·합성 견본만 둔다.

## 테이블

마이그레이션은 옥석현과 칼럼을 합의한 뒤 별도 PR로 올린다(머지된 마이그레이션은 고칠 수 없으므로). 브라우저 권한은 주지 않고 service_role만 읽는다.
아래 칼럼은 그 마이그레이션의 제안이다.

- `corpus_documents`: `document_id`(PK), `corpus`, `source_uri`, `source_name`, `edition`, `effective_from`, `effective_to`, `effective_period_status`, `published_month`(선택, `YYYY-MM`), `content_hash`, `parser_version`, `storage_sha256`, `storage_bytes`, `storage_relation`(`original`·`lossless_recompressed_copy`), `usage_review`(jsonb), `reviewed_by`
- `corpus_source_pages`: `document_id`, `page`, `content_hash`
- `corpus_records`: `corpus`, `kind`, `record_id`, `parent_id`, `is_parent`, `document_id`, `chart_id`, `page_start`, `page_end`, `body`(jsonb)
- `corpus_chunks`: `corpus`, `chunk_id`, `parent_id`, `document_id`, `page_start`, `page_end`, `text`, `raw_text`, `display_text`, `metadata`(jsonb), `embedding_model`, `embedding_dimensions`, `embedding_version`, `embedding_content_hash` (+ 벡터 칼럼은 옥석현)

| 테이블 | 키 | 내용 |
| --- | --- | --- |
| `corpus_documents` | `document_id` | manifest의 문서 값(적용판·source_uri·이용조건·검수자·저장본 해시). manifest와 같은 값 |
| `corpus_source_pages` | `(document_id, page)` | 파싱된 PDF 페이지와 `content_hash`. 텍스트 없음 |
| `corpus_records` | `(corpus, kind, record_id)` | 구조 레코드. `is_parent`면 `(corpus, parent_id)`도 유일 |
| `corpus_chunks` | `(corpus, chunk_id)` | 검색 청크·임베딩 메타데이터. 벡터 칼럼·검색 인덱스는 옥석현 |

`corpus_records.kind`와 `record_id`:

| kind | 원본 파일 | record_id | 부모 |
| --- | --- | --- | --- |
| `chart` | `charts.jsonl` | `chart_id` | 예 |
| `modifier` | `modifiers.jsonl` | `modifier_id` | 아니오 |
| `attribute` | `attributes.jsonl` | `<chart_id>:attr:<순번>` | 아니오 |
| `case` | `cases.jsonl` | `parent_id` | 예 |
| `chart_mapping` | `chart_mappings.jsonl` | `<legacy_chart_id>\|<chart_id>\|<source_pdf_page>` (같은 키 여러 줄은 `body.links`로 묶음) | 아니오 |
| `clause` / `appendix` | `clauses.jsonl` / `appendices.jsonl` | `parent_id` | 예 |
| `schedule_value` | `schedule_values.jsonl` | `record_id` (예: `<document_id>:별표4:base:1`) | 아니오 |

약관 `appendices.jsonl`의 `record_type`:

- `payment_standard_item`: 보험금 지급기준 별표1~5의 항목. 부모 ID는 `<document_id>:payment_standard:별표N[:절]:<항목>`이에요(예: `별표3:1:from-2023-01-01`). 청크 `metadata.clause_type`은 `payment_standard`이고, `schedule`·`section`·`item`이 들어 있어요. 일부 항목에는 `accident_date_from`/`accident_date_to`도 있어요.
- `cited_law_schedule`: 자배법 시행령 별표1·2(p.106–119). 쪽마다 청크가 하나예요.
- `side_note`·`appendix_page`(p.264–293 인용 법조문): 보조 근거예요.

검색 규칙:

- 약관 검색은 `filters.schedule`(예: `"별표3"`)을 받아요.
- 청크의 적용 사고일 범위가 요청 `accident_date` 밖이면 `validate_hits`가 거절해요. 그래서 공통 검색이 먼저 걸러야 해요.
- 계산에 쓰는 수치는 청크가 아니라 `schedule_value` 레코드에서 가져와요.
  - 별표4 감액 비율, 별표5 급별 금액(119개): 원문과 기계 대조만 했기 때문에 `calculation_ready=false`예요.
  - 대인·대물 1차 판정 숫자(별표1 부상 한도·위자료, 별표2 시세하락·전부손해, 별표3 과실상계, 52개, #159): PDF와 대조 검수해 `calculation_ready=true`예요. 원문 위치는 `source_parent_id`에 있어요(`parent_id`는 null).

## 검증 어댑터 (`app/tools/_evidence.py`의 `EvidenceStore`)

옥석현이 DB 연결과 함께 구현해 `configure_evidence_store(factory)`로 등록한다. 등록 전에는 결과가 있으면 `RuntimeError`(fail-closed).

| 메서드 | 조회 | 반환 |
| --- | --- | --- |
| `get_chunk(corpus, chunk_id)` | `corpus_chunks` + `corpus_documents` 조인 | `SearchChunk` 모양(`document`는 SourceDocument 9개 필드, `published_month`는 없으면 null) |
| `get_parent(corpus, parent_id)` | `corpus_records where is_parent` | `body` |
| `get_source_page(document_id, page)` | `corpus_source_pages` | `{"content_hash": ...}` |
| `get_related(corpus, kind, record_id)` | `corpus_records` | `body` |

`common.search`가 돌려주는 청크의 `document`도 같은 조인에서 만들어야 검증이 통과한다(필드별 동일 비교).

## 적재

```bash
python scripts/export_corpus_index_rows.py   # → data/private/corpus-db-import/db_import/<table>.jsonl (git 무시, D 로더 기본 경로. zip으로 받아 풀어 둔 파일을 덮어쓴다)
```

스크립트가 중복 ID·청크 없는 부모·끊긴 도표/수정요소/대조표/부칙 연결·페이지 누락·manifest 해시 불일치를 검사하고
실패하면 아무것도 쓰지 않는다. `corpus_chunks.jsonl`의 `embedding`은 벡터 칼럼용이다. 파일은 저장소에 올리지 않고 비공개로 전달한다.

## 원본과 저장본

비공개 버킷 `corpus-sources`, 객체 이름은 `<corpus>/<document_id>.pdf`. `source_uri`는 버킷 이름을 붙인 `corpus-sources/<corpus>/<document_id>.pdf`.

- `content_hash`: 파싱에 쓴 원본 PDF의 SHA-256. 청크·페이지가 이 값에 묶인다.
- `storage_object.sha256`(DB `storage_sha256`): 버킷 파일의 SHA-256. 인정기준·약관은 원본 그대로라 `content_hash`와 같다.
  심의사례는 업로드 한도 때문에 qpdf 무손실 재압축 사본(51,360,998바이트, 472쪽 동일)이라 값이 다르다. 재압축은 재현되지 않으므로 이 값이 저장본 기준이다.

## 미정

- 원문 보기 서명 URL API 경로·만료 시간
- 벡터 칼럼·인덱스 종류, 질의 임베딩과 같은 모델/차원(`text-embedding-3-small`, 1536) 확인
