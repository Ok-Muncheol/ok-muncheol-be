-- B(구준휘): 판단 실행·확인 질문/답변·검증 로그·요청 멱등성, LangGraph 체크포인트 스키마.
--
-- 계약 원천. 값을 바꾸면 이 파일(새 마이그레이션)과 tests/migrations/test_migration_files.py가 함께 바뀌어야 한다.
--   app/schemas/common.py    TaskStatus → judgment_runs.status, JudgmentStatus → judgment_runs.judgment_status
--   app/workflow/graph.py    STAGES → judgment_runs.stage, MAX_REBUTTAL_RETRIES → 재실행 상한(1)
--   app/schemas/judgment.py  ConfirmationQuestion·QuestionAnswer·ValidationCheck·JudgmentResult
--
-- 쓰기는 BE만 한다. 브라우저(authenticated)는 담당 사건의 판단 상태·질문·답변·검증 로그를 읽기만 한다
-- (Realtime 변경 알림과 새로고침 뒤 조회 복원). 멱등성 키와 체크포인트는 브라우저가 볼 수 없다.
-- 사실 카드 버전 외래키는 A의 카드 버전 테이블이 생기면 새 마이그레이션으로 추가한다.

-- 판단 실행 1건 = 확정 카드 버전 1개에 대한 고정 그래프 실행 1회.
-- 같은 카드 버전의 확인/재개는 같은 실행을 이어 가고, 답변으로 카드 버전이 바뀌면 새 실행을 만들어
-- resumed_from_run_id로 잇고 이전 실행에 superseded_at을 찍는다.
create table public.judgment_runs (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases (id),
  judgment_version integer not null
    constraint judgment_runs_judgment_version_check check (judgment_version >= 1),
  fact_card_version integer not null
    constraint judgment_runs_fact_card_version_check check (fact_card_version >= 1),
  criteria_version text not null,
  rule_version text not null,
  status text not null default 'queued'
    constraint judgment_runs_status_check
    check (status in ('queued', 'running', 'waiting_for_confirmation', 'completed', 'failed')),
  stage text not null default 'require_confirmed_card'
    constraint judgment_runs_stage_check
    check (stage in (
      'require_confirmed_card', 'check_chart_scope', 'retrieve_chart_candidates', 'ask_or_resume',
      'select_chart_and_modifiers', 'check_evidence', 'review_rebuttal',
      'calculate_and_validate_fault', 'build_judgment_result'
    )),
  rebuttal_retry_count smallint not null default 0
    constraint judgment_runs_retry_cap check (rebuttal_retry_count between 0 and 1),
  -- JudgmentResult 원본. 아래 생성 칼럼이 여기서 값을 꺼내 제약을 건다(원천은 하나)
  result jsonb
    constraint judgment_runs_result_object check (result is null or jsonb_typeof(result) = 'object'),
  judgment_status text generated always as (result ->> 'status') stored
    constraint judgment_runs_judgment_status_check
    check (judgment_status in ('draft_ready', 'needs_confirmation', 'deferred')),
  chart_id text generated always as (result ->> 'chart_id') stored,
  edition text generated always as (result ->> 'edition') stored,
  fault_a smallint generated always as ((result ->> 'fault_A')::smallint) stored,
  fault_b smallint generated always as ((result ->> 'fault_B')::smallint) stored,
  -- ApiError(code·message·retryable)
  error jsonb
    constraint judgment_runs_error_shape
    check (error is null or (jsonb_typeof(error) = 'object' and error ?& array['code', 'message', 'retryable'])),
  resumed_from_run_id uuid,
  superseded_at timestamptz,
  requested_by uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  constraint judgment_runs_case_version_key unique (case_id, judgment_version),
  constraint judgment_runs_id_case_key unique (id, case_id),
  constraint judgment_runs_resumed_once unique (resumed_from_run_id),
  constraint judgment_runs_fault_range check (fault_a between 0 and 100 and fault_b between 0 and 100),
  constraint judgment_runs_fault_sum
    check ((fault_a is null) = (fault_b is null) and (fault_a is null or fault_a + fault_b = 100)),
  constraint judgment_runs_completed_has_result check ((status = 'completed') = (result is not null)),
  constraint judgment_runs_result_has_status check (result is null or judgment_status is not null),
  constraint judgment_runs_draft_ready_has_fault
    check (judgment_status is distinct from 'draft_ready' or (chart_id is not null and fault_a is not null)),
  constraint judgment_runs_result_versions check (result is null or (
    result -> 'versions' ->> 'case_id' is not distinct from case_id::text
    and (result -> 'versions' ->> 'fact_card_version')::integer is not distinct from fact_card_version
    and (result -> 'versions' ->> 'judgment_version')::integer is not distinct from judgment_version
  )),
  constraint judgment_runs_failed_has_error check (status <> 'failed' or error is not null),
  constraint judgment_runs_finished_at check (status not in ('completed', 'failed') or completed_at is not null)
);

alter table public.judgment_runs
  add constraint judgment_runs_resumed_same_case
  foreign key (resumed_from_run_id, case_id) references public.judgment_runs (id, case_id);

comment on table public.judgment_runs is '판단 실행. 작업 상태(status)와 판정 상태(judgment_status)를 분리한다. 쓰기는 BE만';
comment on column public.judgment_runs.judgment_version is '사건별 판단 버전. 보고서(D)는 (case_id, judgment_version)을 참조한다';
comment on column public.judgment_runs.fact_card_version is '입력 카드 버전. 담당자가 확정한 버전만 들어온다';
comment on column public.judgment_runs.result is 'JudgmentResult. versions는 이 행의 사건·카드·판단 버전과 같아야 한다';
comment on column public.judgment_runs.superseded_at is '새 카드 버전의 실행으로 대체된 시각. 대체된 실행은 진행 중으로 세지 않는다';

-- 사건마다 진행 중인 판단은 하나뿐이다(중복 실행 방지)
create unique index judgment_runs_one_active_per_case on public.judgment_runs (case_id)
  where status in ('queued', 'running', 'waiting_for_confirmation') and superseded_at is null;

-- 같은 입력(카드·인정기준·규칙 버전)으로는 실패하지 않은 판단이 하나뿐이다
create unique index judgment_runs_one_per_input
  on public.judgment_runs (case_id, fact_card_version, criteria_version, rule_version)
  where status <> 'failed';

create trigger judgment_runs_set_updated_at
before update on public.judgment_runs
for each row execute function private.set_updated_at();

-- 구분 질문(ConfirmationQuestion). case_id는 RLS용 사본이며 실행의 사건과 같아야 한다(복합 외래키)
create table public.judgment_questions (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null,
  case_id uuid not null,
  field_key text not null,
  text text not null,
  options text[] not null default '{}',
  allow_unknown boolean not null default true,
  created_at timestamptz not null default now(),
  constraint judgment_questions_run_field_key unique (run_id, field_key),
  constraint judgment_questions_id_case_key unique (id, case_id),
  constraint judgment_questions_run_fk
    foreign key (run_id, case_id) references public.judgment_runs (id, case_id) on delete cascade
);

comment on table public.judgment_questions is '도표 구분 질문. 실행마다 사실 카드 칸 하나에 질문 하나';

create index judgment_questions_case_id_idx on public.judgment_questions (case_id);

-- 질문 응답(QuestionAnswer). 질문마다 한 번. 카드 반영은 A의 카드 수정/확정 경로가 한다
create table public.judgment_answers (
  question_id uuid primary key,
  case_id uuid not null,
  expected_card_version integer not null
    constraint judgment_answers_expected_card_version_check check (expected_card_version >= 1),
  value text,
  unknown boolean not null default false,
  resulting_card_version integer,
  answered_by uuid not null,
  answered_at timestamptz not null default now(),
  constraint judgment_answers_value_or_unknown check (unknown = (value is null)),
  constraint judgment_answers_card_advances
    check (resulting_card_version is null or resulting_card_version > expected_card_version),
  constraint judgment_answers_question_fk
    foreign key (question_id, case_id) references public.judgment_questions (id, case_id) on delete cascade
);

comment on table public.judgment_answers is '구분 질문 응답. 모른다(unknown)면 value는 비운다';
comment on column public.judgment_answers.resulting_card_version is '응답을 반영해 A가 만든 카드 버전. 반영 전이거나 카드가 바뀌지 않았으면 비운다';

create index judgment_answers_case_id_idx on public.judgment_answers (case_id);

-- 검증 3겹 로그(ValidationCheck). attempt 0은 첫 도표, 1은 반박 뒤 차순위 도표 재실행
create table public.validation_checks (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null,
  case_id uuid not null,
  attempt smallint not null
    constraint validation_checks_attempt_check check (attempt between 0 and 1),
  layer text not null
    constraint validation_checks_layer_check check (layer in ('evidence', 'rebuttal', 'numerical')),
  passed boolean not null,
  reasons text[] not null default '{}',
  created_at timestamptz not null default now(),
  constraint validation_checks_run_attempt_layer_key unique (run_id, attempt, layer),
  constraint validation_checks_failed_has_reason check (passed or cardinality(reasons) > 0),
  constraint validation_checks_run_fk
    foreign key (run_id, case_id) references public.judgment_runs (id, case_id) on delete cascade
);

comment on table public.validation_checks is '근거·반박·수치 검증 결과. 실패하면 이유를 남긴다';

create index validation_checks_case_id_idx on public.validation_checks (case_id);

-- 요청 멱등성(Idempotency-Key 헤더). A의 분석 접수와 B의 판단 접수가 함께 쓴다
create table public.api_idempotency_keys (
  actor_id uuid not null,
  scope text not null,
  key text not null
    constraint api_idempotency_keys_key_length check (char_length(key) between 8 and 128),
  request_hash text not null,
  case_id uuid references public.cases (id) on delete cascade,
  resource_id uuid,
  response_status smallint,
  response_body jsonb,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default (now() + interval '24 hours'),
  constraint api_idempotency_keys_pkey primary key (actor_id, scope, key),
  constraint api_idempotency_keys_response_pair check ((response_status is null) = (response_body is null))
);

comment on table public.api_idempotency_keys is '요청 멱등성 키. 같은 키에 다른 요청 본문(request_hash)이 오면 거절한다. BE 전용';
comment on column public.api_idempotency_keys.scope is '메서드와 경로 틀. 예: POST /cases/{case_id}/judgments';

create index api_idempotency_keys_expires_at_idx on public.api_idempotency_keys (expires_at);

-- LangGraph 체크포인트(langgraph-checkpoint-postgres). PostgresSaver.setup()이 이 스키마에 테이블을 만들고
-- thread_id로 judgment_runs.id를 쓴다. Data API에 노출하지 않고 브라우저 역할에는 권한을 주지 않는다.
create schema if not exists langgraph;
revoke all on schema langgraph from public;

comment on schema langgraph is 'LangGraph 체크포인트. BE 직접 연결 전용';

-- RLS: 브라우저 역할은 담당 사건의 행만 읽는다. 멱등성 키에는 정책이 없어 어떤 행도 보이지 않는다
alter table public.judgment_runs enable row level security;
alter table public.judgment_questions enable row level security;
alter table public.judgment_answers enable row level security;
alter table public.validation_checks enable row level security;
alter table public.api_idempotency_keys enable row level security;

create policy judgment_runs_select_assignee on public.judgment_runs
for select to authenticated using (private.can_access_case(case_id));

create policy judgment_questions_select_assignee on public.judgment_questions
for select to authenticated using (private.can_access_case(case_id));

create policy judgment_answers_select_assignee on public.judgment_answers
for select to authenticated using (private.can_access_case(case_id));

create policy validation_checks_select_assignee on public.validation_checks
for select to authenticated using (private.can_access_case(case_id));

revoke all on table
  public.judgment_runs, public.judgment_questions, public.judgment_answers,
  public.validation_checks, public.api_idempotency_keys
from anon, authenticated;

grant select on table
  public.judgment_runs, public.judgment_questions, public.judgment_answers, public.validation_checks
to authenticated;

grant all on table
  public.judgment_runs, public.judgment_questions, public.judgment_answers,
  public.validation_checks, public.api_idempotency_keys
to service_role;

-- Realtime 변경 알림: 진행 상태와 새 질문. 다시 접속하면 조회 API로 현재 상태를 복원한다
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime' and not puballtables) then
    alter publication supabase_realtime add table public.judgment_runs, public.judgment_questions;
  end if;
end;
$$;
