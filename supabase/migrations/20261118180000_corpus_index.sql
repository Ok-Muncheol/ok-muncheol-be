-- B(통합): 세 코퍼스 색인을 Postgres에 둔다(pgvector). 계약: docs/contracts/corpus-index.md
-- 표·칼럼은 C(김지은)의 초안(20261110180000_corpus_index.sql, 합의 전)을 따랐고, 세 가지를 더했다.
--   1. 적재판(release): C가 보낸 적재 파일 묶음 하나가 한 판이다. 모든 표의 키 맨 앞에 둔다.
--      BE는 CORPUS_DB_RELEASE로 정한 판만 읽는다. 새 판을 넣어도 지금 읽는 판은 바뀌지 않고,
--      판을 지우면(corpus_releases 한 줄) 그 판의 행이 모두 지워진다.
--   2. 벡터 칼럼(corpus_chunks.embedding, 1536차원)과 적재 순서(seq).
--   3. 키워드 검색(BM25)용 청크별 낱말 수(corpus_chunk_tokens). 토큰화는 BE 코드
--      (app/retrieval/keyword.py)가 적재할 때 하고, 점수는 검색할 때 SQL이 계산한다.
--
-- 원칙
--   - 원문 이용조건이 확인되지 않았으므로 브라우저(anon·authenticated)에는 어떤 권한도 주지 않는다.
--     BE만 읽고, 원문 보기는 BE가 비공개 Storage 서명 URL을 내준다.
--   - 행은 scripts/load_corpus_release.py가 한 트랜잭션으로 넣는다. 넣은 뒤에는 고치지 않는다.
--   - 벡터에는 근사 색인(HNSW·IVFFlat)을 만들지 않는다. 코퍼스마다 청크가 천 개 안팎이라 전부 비교해도
--     몇 밀리초이고, 근사 색인은 필터 뒤에 결과가 모자라거나 순위가 달라질 수 있다.

create extension if not exists vector with schema extensions;

create table public.corpus_releases (
  release text primary key,
  embedding_model text not null,
  embedding_dimensions integer not null,
  embedding_version text not null,
  keyword_tokenizers text[] not null default '{}',
  source_sha256 jsonb not null,
  row_counts jsonb not null,
  note text,
  loaded_at timestamptz not null default now(),
  constraint corpus_releases_name_check check (release ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
  constraint corpus_releases_dimensions_check check (embedding_dimensions = 1536),
  constraint corpus_releases_sums_check check (jsonb_typeof(source_sha256) = 'object'),
  constraint corpus_releases_counts_check check (jsonb_typeof(row_counts) = 'object')
);

comment on table public.corpus_releases is
  '코퍼스 적재판. C가 보낸 적재 파일 묶음(db_import 네 파일) 하나가 한 줄이다';
comment on column public.corpus_releases.release is
  '판 이름(예: 20261116-1742). BE의 CORPUS_DB_RELEASE와 같은 값';
comment on column public.corpus_releases.keyword_tokenizers is
  'corpus_chunk_tokens를 만든 토큰화 이름(simple·kiwi). BE는 자기 토큰화가 여기 있을 때만 키워드 검색을 한다';
comment on column public.corpus_releases.source_sha256 is
  '적재한 db_import 파일의 SHA-256({표 이름: 해시}). C의 SHA256SUMS.txt와 같은 값';
comment on column public.corpus_releases.row_counts is '표마다 넣은 행 수({표 이름: 행 수})';

create table public.corpus_documents (
  release text not null references public.corpus_releases (release) on delete cascade,
  document_id text not null,
  corpus text not null,
  source_uri text,
  source_name text not null,
  edition text not null,
  effective_from text not null default '',
  effective_to text,
  effective_period_status text not null,
  published_month text,
  content_hash text not null,
  parser_version text not null,
  storage_sha256 text not null,
  storage_bytes bigint not null,
  storage_relation text not null,
  usage_review jsonb not null,
  reviewed_by text,
  primary key (release, document_id),
  unique (release, document_id, corpus),
  constraint corpus_documents_corpus_check
    check (corpus in ('fault_standards', 'dispute_cases', 'policy_terms')),
  constraint corpus_documents_effective_from_check
    check (effective_from = '' or effective_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
  constraint corpus_documents_effective_to_check
    check (effective_to is null or effective_to ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
  constraint corpus_documents_published_month_check
    check (published_month is null or published_month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
  constraint corpus_documents_hash_check check (content_hash ~ '^[0-9a-f]{64}$'),
  constraint corpus_documents_storage_hash_check check (storage_sha256 ~ '^[0-9a-f]{64}$'),
  constraint corpus_documents_storage_relation_check
    check (storage_relation in ('original', 'lossless_recompressed_copy')),
  constraint corpus_documents_storage_original_check
    check (storage_relation <> 'original' or storage_sha256 = content_hash)
);

comment on table public.corpus_documents is
  '코퍼스 원문 문서. 값은 data/corpora/*/manifest.json과 같게 둔다';
comment on column public.corpus_documents.source_uri is
  '비공개 Storage 경로 <bucket>/<path>. 공개 URL을 넣지 않는다';
comment on column public.corpus_documents.effective_from is
  '적용 시작일 YYYY-MM-DD. 빈 문자열이면 기간 모름(검색의 날짜 필터를 통과한다)';
comment on column public.corpus_documents.published_month is
  '원문에서 확인한 발행월 YYYY-MM(판권면 등). 시행일이 아니다. 확인 못 했으면 null';
comment on column public.corpus_documents.content_hash is
  '파싱에 쓴 원본 PDF의 SHA-256. 청크·페이지는 이 해시에 묶인다';
comment on column public.corpus_documents.storage_sha256 is
  '버킷에 올린 파일의 SHA-256. 무손실 재압축 사본이면 content_hash와 다르다';

create table public.corpus_source_pages (
  release text not null,
  document_id text not null,
  page integer not null,
  content_hash text not null,
  primary key (release, document_id, page),
  foreign key (release, document_id)
    references public.corpus_documents (release, document_id) on delete cascade,
  constraint corpus_source_pages_page_check check (page >= 1)
);

comment on table public.corpus_source_pages is
  '파싱된 PDF 페이지 목록. 근거의 페이지가 원문에 실재하는지 확인한다. 페이지 텍스트는 두지 않는다';

create table public.corpus_records (
  release text not null references public.corpus_releases (release) on delete cascade,
  corpus text not null,
  kind text not null,
  record_id text not null,
  seq integer not null,
  parent_id text,
  is_parent boolean not null,
  document_id text,
  chart_id text,
  page_start integer,
  page_end integer,
  body jsonb not null,
  primary key (release, corpus, kind, record_id),
  unique (release, seq),
  foreign key (release, document_id)
    references public.corpus_documents (release, document_id) on delete cascade,
  constraint corpus_records_corpus_check
    check (corpus in ('fault_standards', 'dispute_cases', 'policy_terms')),
  constraint corpus_records_kind_check
    check (kind in ('chart', 'modifier', 'attribute', 'case', 'chart_mapping', 'clause', 'appendix',
      'schedule_value')),
  constraint corpus_records_parent_check check (not is_parent or parent_id is not null),
  constraint corpus_records_body_check check (jsonb_typeof(body) = 'object')
);

create unique index corpus_records_parent_idx
  on public.corpus_records (release, corpus, parent_id) where is_parent;

comment on table public.corpus_records is
  '검수용 구조 레코드(도표·수정요소·속성·결정례·대조표·조항·부칙·약관 수치). body는 원본 행 그대로';
comment on column public.corpus_records.seq is '적재 파일의 줄 순서(판 안에서 0부터)';

create table public.corpus_chunks (
  release text not null references public.corpus_releases (release) on delete cascade,
  corpus text not null,
  chunk_id text not null,
  seq integer not null,
  parent_id text not null,
  document_id text not null,
  page_start integer not null,
  page_end integer not null,
  text text not null,
  raw_text text,
  display_text text,
  metadata jsonb not null,
  embedding_model text not null,
  embedding_dimensions integer not null,
  embedding_version text not null,
  embedding_content_hash text not null,
  embedding extensions.vector(1536) not null,
  primary key (release, corpus, chunk_id),
  unique (release, seq),
  foreign key (release, document_id, corpus)
    references public.corpus_documents (release, document_id, corpus) on delete cascade,
  constraint corpus_chunks_page_start_check check (page_start >= 1),
  constraint corpus_chunks_page_check check (page_end >= page_start),
  constraint corpus_chunks_metadata_check check (jsonb_typeof(metadata) = 'object'),
  constraint corpus_chunks_dimensions_check
    check (embedding_dimensions = extensions.vector_dims(embedding))
);

create index corpus_chunks_parent_idx on public.corpus_chunks (release, corpus, parent_id);

comment on table public.corpus_chunks is
  '검색 청크와 임베딩. document는 corpus_documents와 조인해 SearchChunk로 돌려준다';
comment on column public.corpus_chunks.seq is
  '적재 파일의 줄 순서(판 안에서 0부터). 같은 조항·같은 페이지 청크의 순서를 정한다';
comment on column public.corpus_chunks.embedding is
  '문서 임베딩(코사인). 모델·차원·버전은 같은 행의 embedding_* 칼럼. 근사 색인을 만들지 않는다';

create table public.corpus_chunk_tokens (
  release text not null,
  tokenizer text not null,
  corpus text not null,
  chunk_id text not null,
  token_count integer not null,
  term_counts jsonb not null,
  primary key (release, tokenizer, corpus, chunk_id),
  foreign key (release, corpus, chunk_id)
    references public.corpus_chunks (release, corpus, chunk_id) on delete cascade,
  constraint corpus_chunk_tokens_count_check check (token_count >= 0),
  constraint corpus_chunk_tokens_terms_check check (jsonb_typeof(term_counts) = 'object')
);

comment on table public.corpus_chunk_tokens is
  '키워드 검색(BM25)용 청크별 낱말 수. 토큰화마다 한 벌이고 통계는 판·토큰화·코퍼스 단위로 낸다';
comment on column public.corpus_chunk_tokens.tokenizer is
  '토큰화 이름(app/retrieval/keyword.py의 name). 질의도 같은 토큰화로 나눠야 한다';
comment on column public.corpus_chunk_tokens.token_count is '청크의 낱말 수 합(BM25 문서 길이)';
comment on column public.corpus_chunk_tokens.term_counts is '{낱말: 나온 횟수}';

alter table public.corpus_releases enable row level security;
alter table public.corpus_documents enable row level security;
alter table public.corpus_source_pages enable row level security;
alter table public.corpus_records enable row level security;
alter table public.corpus_chunks enable row level security;
alter table public.corpus_chunk_tokens enable row level security;

revoke all on public.corpus_releases, public.corpus_documents, public.corpus_source_pages,
  public.corpus_records, public.corpus_chunks, public.corpus_chunk_tokens from anon, authenticated;
grant all on public.corpus_releases, public.corpus_documents, public.corpus_source_pages,
  public.corpus_records, public.corpus_chunks, public.corpus_chunk_tokens to service_role;
