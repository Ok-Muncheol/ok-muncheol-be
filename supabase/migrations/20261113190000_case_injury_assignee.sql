-- B(통합): 대인 담당자. 사건마다 대물 담당자(assignee_id) 옆에 대인 담당자 한 명을 둘 수 있다.
--
-- 2026-11-13 결정(구준휘): 접수된 사건은 대물 담당자에게, 다친 사람이 있으면 대인 담당자에게도 배정한다.
-- 두 담당자는 같은 사건 화면을 쓰고 권한이 같다(조회·검토·수정·확정). 누가 했는지는 지금처럼 각 행의
-- requested_by·created_by와 카드·보고서의 confirmed_by·approved_by에 남는다.
--   - cases.assignee_id         대물 담당자(사건 주 담당). 지금까지의 담당자 그대로이고 비울 수 없다.
--   - cases.injury_assignee_id  대인 담당자. 다친 사람이 없거나 아직 배정하지 않았으면 null.
--
-- 사건 단위 테이블의 RLS는 모두 private.can_access_case(case_id)로 판정하므로 함수만 바꾸면 전부 따라온다.
-- cases 자체의 정책(cases_select_assignee)은 함수를 쓰지 않아 대인 담당자용 정책을 하나 더한다(정책은 OR로 합친다).
-- 쓰기는 여전히 BE만 한다. 배정·이관(이 두 칼럼을 바꾸는 일)도 BE가 한다.
-- BE의 같은 판정은 app/workflow/runs.py case_assignees다. 둘은 함께 바뀌어야 한다.
--
-- 적용 순서: 이 파일과 BE 코드는 어느 쪽이 먼저여도 된다. 칼럼이 없을 때 BE는 대물 담당자만 본다
-- (runs.INJURY_ASSIGNEE_SQL — 칼럼을 직접 참조하지 않는다). 칼럼이 먼저 생겨도 이전 코드는 그대로 돈다.

alter table public.cases
  add column injury_assignee_id uuid references auth.users (id);

comment on table public.cases is '사건. 대물·대인 담당자만 조회한다. 사건 범위·당사자·상태 칼럼은 A 담당이 확장한다';
comment on column public.cases.assignee_id is
  '대물 담당 손해사정사(auth.users.id, 사건 주 담당). 대인 담당자(injury_assignee_id)와 함께 사건 단위 RLS의 기준';
comment on column public.cases.injury_assignee_id is
  '대인 담당 손해사정사(auth.users.id). 없으면 null. 대물 담당자와 권한이 같다';

-- 대인 담당자의 사건 목록 조회용. 대부분 null이라 값이 있는 행만 색인한다
create index cases_injury_assignee_id_idx on public.cases (injury_assignee_id)
  where injury_assignee_id is not null;

-- 현재 로그인 사용자가 사건의 대물 또는 대인 담당자인지. 권한(authenticated·service_role 실행)은 그대로 남는다.
-- injury_assignee_id가 null이면 in 목록의 null은 일치하지 않으므로 대물 담당자만 통과한다.
create or replace function private.can_access_case(target_case_id uuid) returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.cases c
    where c.id = target_case_id
      and (select auth.uid()) in (c.assignee_id, c.injury_assignee_id)
  );
$$;

create policy cases_select_injury_assignee on public.cases
for select to authenticated
using (injury_assignee_id = (select auth.uid()));
