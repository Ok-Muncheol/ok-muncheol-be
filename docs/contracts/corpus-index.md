# 코퍼스 색인 DB 계약 (김지은 초안 + 구준휘 통합, 옥석현·김지은 검토 필요)

저장 위치: 원본 PDF는 비공개 Storage, 중간 파일·평가셋은 로컬 `data/private/`, 검색·검증은 DB.
Git에는 코드·`data/corpora/*/manifest.json`·합성 견본만 둔다.

## 테이블

마이그레이션은 `supabase/migrations/20261118180000_corpus_index.sql`이다. 김지은의 초안 표에 구준휘(통합)가 적재판·벡터 칼럼·키워드 낱말 수를 더해 올렸다(아래 「적재판과 검색」).
브라우저 권한은 주지 않고 BE만 읽는다. 머지된 마이그레이션은 고칠 수 없으므로 칼럼을 바꾸려면 새 마이그레이션을 더한다.

모든 표의 키 맨 앞에 `release`(적재판)가 있다. 아래 키는 `release`를 뺀 나머지다.

- `corpus_releases`: `release`(PK), `embedding_model`, `embedding_dimensions`, `embedding_version`, `keyword_tokenizers`(text[]), `source_sha256`(jsonb), `row_counts`(jsonb), `note`, `loaded_at`
- `corpus_documents`: `document_id`, `corpus`, `source_uri`, `source_name`, `edition`, `effective_from`, `effective_to`, `effective_period_status`, `published_month`(선택, `YYYY-MM`), `content_hash`, `parser_version`, `storage_sha256`, `storage_bytes`, `storage_relation`(`original`·`lossless_recompressed_copy`), `usage_review`(jsonb), `reviewed_by`
- `corpus_source_pages`: `document_id`, `page`, `content_hash`
- `corpus_records`: `corpus`, `kind`, `record_id`, `seq`, `parent_id`, `is_parent`, `document_id`, `chart_id`, `page_start`, `page_end`, `body`(jsonb)
- `corpus_chunks`: `corpus`, `chunk_id`, `seq`, `parent_id`, `document_id`, `page_start`, `page_end`, `text`, `raw_text`, `display_text`, `metadata`(jsonb), `embedding_model`, `embedding_dimensions`, `embedding_version`, `embedding_content_hash`, `embedding`(`vector(1536)`)
- `corpus_chunk_tokens`: `tokenizer`, `corpus`, `chunk_id`, `token_count`, `term_counts`(jsonb)

| 테이블 | 키(`release` 다음) | 내용 |
| --- | --- | --- |
| `corpus_releases` | - | 적재판 한 줄. 임베딩 모델·차원·버전, 낱말 수를 만든 토큰화, 적재한 파일의 SHA-256과 행 수 |
| `corpus_documents` | `document_id` | manifest의 문서 값(적용판·source_uri·이용조건·검수자·저장본 해시). manifest와 같은 값 |
| `corpus_source_pages` | `(document_id, page)` | 파싱된 PDF 페이지와 `content_hash`. 텍스트 없음 |
| `corpus_records` | `(corpus, kind, record_id)` | 구조 레코드. `is_parent`면 `(corpus, parent_id)`도 유일. `seq`는 적재 파일의 줄 순서 |
| `corpus_chunks` | `(corpus, chunk_id)` | 검색 청크·임베딩 메타데이터·벡터. `seq`는 적재 파일의 줄 순서(같은 조항·같은 페이지 청크의 순서) |
| `corpus_chunk_tokens` | `(tokenizer, corpus, chunk_id)` | 키워드 검색(BM25)용 청크별 낱말 수 |

김지은 초안과 달라진 점: 키에 `release`를 더했다. `corpus_documents.created_at`은 뺐다(`corpus_releases.loaded_at`이 있다). `effective_from`·`effective_to`가 `YYYY-MM-DD`(시작일은 빈 값도)인지 제약으로 본다. `seq`·`embedding`·`corpus_releases`·`corpus_chunk_tokens`를 더했다.

## 적재판과 검색

- **적재판**: 김지은이 보낸 적재 파일 묶음(db_import 네 파일) 하나가 한 판이다. 이름은 묶음 이름의 날짜·시각(예: `20261116-1742`)을 쓴다. BE는 환경 변수 `CORPUS_DB_RELEASE`로 정한 판만 읽는다. 판은 넣은 뒤 고치지 않는다. 새 자료는 새 판으로 넣고 변수를 바꾸며, 되돌릴 때도 변수만 바꾼다. 판을 지우면(`corpus_releases` 한 줄) 그 판의 행이 모두 지워진다.
- **벡터**: `text-embedding-3-small` 1536차원, 코사인. 근사 색인(HNSW·IVFFlat)은 만들지 않고 전부 비교한다(코퍼스마다 청크 천 개 안팎). 점수는 `1 − 코사인 거리`다. pgvector가 단정밀도로 계산해 메모리 색인과 소수 일곱째 자리쯤에서 다를 수 있다.
- **키워드**: 적재할 때 BE 토큰화(`app/retrieval/keyword.py`)로 청크마다 낱말 수를 세어 `corpus_chunk_tokens`에 넣는다. 검색할 때 BE가 질의를 같은 토큰화로 나누고 SQL이 BM25를 계산한다(식·상수는 메모리 색인과 같다). 토큰화 이름(`simple`·`kiwi`)마다 한 벌이다. 서버의 토큰화가 판에 없으면 키워드 검색은 `RetrievalNotConfigured`이고, 이름은 같은데 낱말을 다르게 나누면(코드·형태소 모델이 바뀐 경우) `CorpusDataError`로 멈춘다. 그때는 다시 적재한다.
- **필터**: 문서 적용 기간, 청크의 적용 사고일 범위, `metadata` 비교를 SQL로 한다. 규칙은 `app/retrieval/index.py`의 `matches`와 같다. SQL이 날짜로 비교하므로 `effective_from`·`effective_to`와 `metadata.accident_date_from`·`accident_date_to`는 `YYYY-MM-DD`만 넣는다(다르면 적재가 멈춘다).
- 구현은 `app/retrieval/pg_index.py`(`PgCorpusIndex`), 메모리 색인과 결과를 맞대는 검사는 `tests/test_pg_corpus_index.py`다.

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

DB 어댑터는 `app/retrieval/pg_evidence_store.py`의 `PgEvidenceStore`다. `CORPUS_DB_RELEASE`가 있으면 BE가 지식베이스를 켤 때 `configure_evidence_store(factory)`로 등록한다. 등록 전에는 결과가 있으면 `RuntimeError`(fail-closed).
검증은 결과 한 건마다 어댑터를 열 번 넘게 부르므로, 어댑터는 처음 필요할 때 그 코퍼스의 행을 한 번에 읽어 두고 그 뒤로는 메모리에서 답한다(판은 고치지 않으므로 낡지 않는다).
파일을 읽는 로컬 어댑터(`app/retrieval/evidence_store.py`)는 DB 없이 개발할 때와 `CORPUS_DB_RELEASE`가 없는 서버가 쓴다.

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

DB에는 통합 담당(구준휘)이 묶음 폴더(`SHA256SUMS.txt` + `db_import/`)를 한 판으로 넣는다.

```bash
python scripts/load_corpus_release.py <묶음 폴더>   # 파일을 SHA256SUMS.txt와 대조 → 한 트랜잭션으로 적재 → 다시 읽어 파일과 대조
```

적재 파일의 모양은 그대로다. `SHA256SUMS.txt`에 db_import 네 줄이 있어야 한다. 같은 이름의 판은 덮어쓰지 않는다.

## 원본과 저장본

비공개 버킷 `corpus-sources`, 객체 이름은 `<corpus>/<document_id>.pdf`. `source_uri`는 버킷 이름을 붙인 `corpus-sources/<corpus>/<document_id>.pdf`.

- `content_hash`: 파싱에 쓴 원본 PDF의 SHA-256. 청크·페이지가 이 값에 묶인다.
- `storage_object.sha256`(DB `storage_sha256`): 버킷 파일의 SHA-256. 인정기준·약관은 원본 그대로라 `content_hash`와 같다.
  심의사례는 업로드 한도 때문에 qpdf 무손실 재압축 사본(51,360,998바이트, 472쪽 동일)이라 값이 다르다. 재압축은 재현되지 않으므로 이 값이 저장본 기준이다.

## 미정

- 원문 보기 서명 URL API 경로·만료 시간
