-- B(통합): 담당 사건 접근 기반. 사건 단위 테이블은 모두 이 사건 ID와 접근 함수로 RLS를 건다.
--
-- 원칙
--   - 쓰기는 BE만 한다(서버 비밀키 또는 직접 연결). 브라우저(authenticated)는 담당 사건 행의 SELECT만 받는다.
--   - 권한은 Supabase 기본 권한에 기대지 않고 테이블마다 명시한다: anon 없음, authenticated는 SELECT, service_role 전체.
--   - RLS 도우미·트리거 함수는 Data API에 노출되지 않는 private 스키마에 둔다(security definer 함수 노출 금지).
--
-- cases는 최소 골격이다. A(정지원)가 사건 범위·당사자·상태 칼럼과 생성 경로를 새 마이그레이션의 ADD COLUMN으로 확장한다.
-- 머지된 마이그레이션 파일은 고치지 않는다. 바꿀 것이 생기면 새 파일을 추가한다.

create schema if not exists private;
revoke all on schema private from public;
grant usage on schema private to authenticated, service_role;

comment on schema private is 'RLS 도우미·트리거 함수. Data API에 노출하지 않는다';

create function private.set_updated_at() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

revoke all on function private.set_updated_at() from public;

create table public.cases (
  id uuid primary key default gen_random_uuid(),
  assignee_id uuid not null references auth.users (id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.cases is '사건. 담당자만 조회한다. 사건 범위·당사자·상태 칼럼은 A 담당이 확장한다';
comment on column public.cases.assignee_id is '담당 손해사정사(auth.users.id). 모든 사건 단위 RLS의 기준';

create index cases_assignee_id_idx on public.cases (assignee_id);

create trigger cases_set_updated_at
before update on public.cases
for each row execute function private.set_updated_at();

-- 현재 로그인 사용자가 사건 담당자인지. 다른 담당의 RLS 정책도 이 함수로 사건 접근을 판정한다.
-- security definer라 cases의 RLS를 다시 거치지 않으므로 정책 안에서 재귀하지 않는다.
create function private.can_access_case(target_case_id uuid) returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.cases c
    where c.id = target_case_id
      and c.assignee_id = (select auth.uid())
  );
$$;

revoke all on function private.can_access_case(uuid) from public;
grant execute on function private.can_access_case(uuid) to authenticated, service_role;

alter table public.cases enable row level security;

create policy cases_select_assignee on public.cases
for select to authenticated
using (assignee_id = (select auth.uid()));

revoke all on table public.cases from anon, authenticated;
grant select on table public.cases to authenticated;
grant all on table public.cases to service_role;
