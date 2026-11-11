-- A(정지원): 영상 분석 작업. POST /cases/{id}/analysis가 만들고 Modal 콜백(POST /internal/analysis/{task_id})이 끝낸다.
--
-- 흐름(2026-11-11 정지원·구준휘 합의): 권한·업로드 확인 → 작업 행(queued) → Modal 호출(running, call_id)
--   → Modal이 일회용 토큰으로 콜백 → BE가 카드 새 버전 저장 + completed, 또는 오류 그대로 failed.
-- 콜백 토큰은 원문을 저장하지 않고 SHA-256(hex)만 둔다. 끝난 작업(completed·failed)은 다시 바꾸지 않는다(같은 콜백은 한 번만).
-- 콜백이 오지 않으면 조회 때 15분 넘은 queued·running을 failed(analysis_timeout)로 바꾼다(app/api/analysis.py).
-- 쓰기는 BE만, 브라우저는 담당 사건 행을 읽기만 한다.

create table public.analysis_tasks (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases (id),
  storage_path text not null,
  idempotency_key text not null
    constraint analysis_tasks_idempotency_key_check check (char_length(idempotency_key) between 8 and 128),
  status text not null default 'queued'
    constraint analysis_tasks_status_check check (status in ('queued', 'running', 'completed', 'failed')),
  callback_token_hash text not null
    constraint analysis_tasks_token_hash_check check (callback_token_hash ~ '^[0-9a-f]{64}$'),
  modal_call_id text,
  fact_card_version integer,
  error jsonb
    constraint analysis_tasks_error_shape
    check (error is null or (jsonb_typeof(error) = 'object' and error ?& array['code', 'message', 'retryable'])),
  requested_by uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz,
  constraint analysis_tasks_case_key unique (case_id, idempotency_key),
  constraint analysis_tasks_id_case_key unique (id, case_id),
  -- 이 사건 폴더의 영상만(request_upload가 준 경로)
  constraint analysis_tasks_video_path check (storage_path like 'case-files/' || case_id::text || '/videos/%'),
  constraint analysis_tasks_card_fk
    foreign key (case_id, fact_card_version) references public.fact_cards (case_id, version),
  constraint analysis_tasks_completed_has_card check (status <> 'completed' or fact_card_version is not null),
  constraint analysis_tasks_failed_has_error check (status <> 'failed' or error is not null),
  constraint analysis_tasks_done_has_time check (status not in ('completed', 'failed') or completed_at is not null)
);

comment on table public.analysis_tasks is '영상 분석 작업. 사건마다 진행 중(queued·running) 하나. 쓰기는 BE만';
comment on column public.analysis_tasks.callback_token_hash is 'Modal 콜백 일회용 토큰의 SHA-256(hex). 원문은 저장하지 않는다';
comment on column public.analysis_tasks.error is 'VisionPipelineError 등 {stage?, code, message, retryable}. retryable이면 FE가 새 Idempotency-Key로 다시 접수';

-- 사건마다 진행 중인 분석은 하나
create unique index analysis_tasks_one_active_idx on public.analysis_tasks (case_id)
  where status in ('queued', 'running');
create index analysis_tasks_case_created_idx on public.analysis_tasks (case_id, created_at desc);

create trigger analysis_tasks_set_updated_at
before update on public.analysis_tasks
for each row execute function private.set_updated_at();

alter table public.analysis_tasks enable row level security;

create policy analysis_tasks_select_assignee on public.analysis_tasks
for select to authenticated using (private.can_access_case(case_id));

revoke all on table public.analysis_tasks from anon, authenticated;
grant select on table public.analysis_tasks to authenticated;
grant all on table public.analysis_tasks to service_role;

-- Realtime: 분석이 끝나거나 실패하면 화면이 사건을 다시 읽는다
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime' and not puballtables) then
    alter publication supabase_realtime add table public.analysis_tasks;
  end if;
end;
$$;
