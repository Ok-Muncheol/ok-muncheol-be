-- B(통합): 접수와 배정(사건 관제). 접수 건(case_intakes)과 배정 이력(case_assignments).
--
-- 흐름: 접수 등록 → 배정(에이전트·규칙·관리자) → 담당자가 처리 → 종결.
--   - 접수 건은 배정되기 전에도 있다. 대물 담당자를 구하지 못하면 unassigned로 남고 관리자가 직접 배정한다.
--   - 실제 접수는 배정되는 순간 cases 행을 만들고 case_id로 잇는다. 사건 접근 권한의 기준은 여전히
--     cases.assignee_id·injury_assignee_id다. 이 표의 담당자 칼럼은 관제 화면이 읽는 사본이고,
--     배정·이관 서비스(app/dispatch)가 한 트랜잭션에서 함께 바꾼다.
--   - 가상 접수(simulated)는 관제 화면을 채우는 시연용이다. cases 행을 만들지 않고 분석·판단·보고서 표에도
--     쓰지 않는다. 배정할 때 단계별 일정(facts_at … closes_at)을 한 번 정해 두고, 화면은 현재 시각과
--     비교해 단계를 보인다. closes_at이 지나면 종결로 본다(행을 고치지 않는다 — status는 assigned 그대로).
--     종결된 자리에 들어올 다음 접수도 미리 만들어 둔다(received_at = 앞 건의 closes_at). 그래서
--     received_at이 아직 오지 않은 가상 접수가 표에 있고, 조회는 늘 received_at <= 지금인 것만 본다.
-- 담당자·역할의 원천은 Supabase Auth의 app_metadata다(staff_role·coverage·region·auto_assign).
-- 이 표는 계정 ID만 가진다. 지역 목록은 app/schemas/admin.py Region과 같아야 한다
-- (tests/migrations/test_migration_files.py가 대조한다).
-- 읽기·쓰기 모두 BE만 한다(관리자 API). 브라우저 역할에는 권한도 정책도 주지 않는다.

create table public.case_intakes (
  id uuid primary key default gen_random_uuid(),
  -- 접수 내용
  received_at timestamptz not null default now(),
  accident_at timestamptz,
  location text not null
    constraint case_intakes_location_length check (char_length(location) between 1 and 500),
  description text not null
    constraint case_intakes_description_length check (char_length(description) between 1 and 4000),
  injured_count smallint not null default 0
    constraint case_intakes_injured_count_check check (injured_count between 0 and 20),
  -- 배정
  region text
    constraint case_intakes_region_check check (region in (
      '서울', '부산', '대구', '인천', '광주', '대전', '울산', '세종', '경기',
      '강원', '충북', '충남', '전북', '전남', '경북', '경남', '제주'
    )),
  status text not null default 'received'
    constraint case_intakes_status_check
    check (status in ('received', 'assigned', 'unassigned', 'closed')),
  property_assignee_id uuid references auth.users (id),
  injury_assignee_id uuid references auth.users (id),
  unassigned_reason text,
  case_id uuid references public.cases (id),
  registered_by uuid,
  -- 가상 접수의 단계 일정
  simulated boolean not null default false,
  facts_at timestamptz,
  judgment_at timestamptz,
  report_at timestamptz,
  closes_at timestamptz,
  closed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint case_intakes_case_key unique (case_id),
  constraint case_intakes_assigned_has_property
    check (status not in ('assigned', 'closed') or property_assignee_id is not null),
  constraint case_intakes_assignees_only_after_assignment
    check (status in ('assigned', 'closed')
           or (property_assignee_id is null and injury_assignee_id is null)),
  constraint case_intakes_unassigned_has_reason
    check (status <> 'unassigned' or unassigned_reason is not null),
  constraint case_intakes_closed_has_time check ((status = 'closed') = (closed_at is not null)),
  -- 가상 접수는 일정이 모두 있고 순서가 맞다. 실제 접수에는 일정이 없다
  constraint case_intakes_plan_shape check (
    (simulated and facts_at is not null and judgment_at is not null and report_at is not null
      and closes_at is not null and received_at <= facts_at and facts_at <= judgment_at
      and judgment_at <= report_at and report_at <= closes_at)
    or (not simulated and facts_at is null and judgment_at is null and report_at is null
      and closes_at is null)
  ),
  constraint case_intakes_simulated_has_no_case check (not simulated or case_id is null),
  constraint case_intakes_real_assigned_has_case
    check (simulated or status not in ('assigned', 'closed') or case_id is not null)
);

comment on table public.case_intakes is '접수 건. 배정 전에도 있고, 실제 접수는 배정되면 cases와 이어진다. 쓰기·읽기는 BE만';
comment on column public.case_intakes.injured_count is '접수 때 들은 다친 사람 수. 0이면 대물 담당자만 배정한다';
comment on column public.case_intakes.region is '사고 장소에서 판별한 지역(17개 시·도). 판별 전·실패면 null';
comment on column public.case_intakes.property_assignee_id is '지금의 대물 담당자. 실제 접수는 cases.assignee_id와 같아야 한다';
comment on column public.case_intakes.injury_assignee_id is '지금의 대인 담당자(없으면 null). 실제 접수는 cases.injury_assignee_id와 같아야 한다';
comment on column public.case_intakes.unassigned_reason is '자동 배정이 담당자를 구하지 못한 까닭. 관리자 대기열에 보인다';
comment on column public.case_intakes.registered_by is '접수를 등록한 관리자(auth.users.id). 가상 접수와 접수 기능 전의 사건은 null';
comment on column public.case_intakes.simulated is '시연용 가상 접수. cases 행이 없고 단계는 일정 칼럼으로만 정한다';
comment on column public.case_intakes.closes_at is '가상 접수의 종결 시각. 지나면 종결로 본다(행은 고치지 않는다)';
comment on column public.case_intakes.closed_at is '실제 접수를 종결 처리한 시각. 가상 접수는 closes_at으로 판정하고 이 칸을 쓰지 않는다';

create index case_intakes_received_idx on public.case_intakes (received_at desc);
create index case_intakes_status_received_idx on public.case_intakes (status, received_at desc);
-- 담당자별 진행 중 건수(배정 후보의 업무량, 관제 화면)
create index case_intakes_property_open_idx on public.case_intakes (property_assignee_id)
  where status = 'assigned';
create index case_intakes_injury_open_idx on public.case_intakes (injury_assignee_id)
  where status = 'assigned' and injury_assignee_id is not null;
-- 가상 접수의 종결 시각(오늘 종결 수, 그 시각에 진행 중이던 건 세기)
create index case_intakes_closes_idx on public.case_intakes (closes_at)
  where simulated and status = 'assigned';

create trigger case_intakes_set_updated_at
before update on public.case_intakes
for each row execute function private.set_updated_at();

-- 배정 이력. 한 줄 = 한 분야(대물·대인)의 담당자를 정하거나 바꾼 일 한 번. 지우거나 고치지 않는다
create table public.case_assignments (
  id uuid primary key default gen_random_uuid(),
  intake_id uuid not null references public.case_intakes (id) on delete cascade,
  coverage text not null
    constraint case_assignments_coverage_check check (coverage in ('property', 'injury')),
  from_assignee_id uuid references auth.users (id),
  to_assignee_id uuid not null references auth.users (id),
  method text not null
    constraint case_assignments_method_check
    check (method in ('agent', 'rule', 'manual', 'transfer')),
  reason text not null
    constraint case_assignments_reason_length check (char_length(reason) between 1 and 1000),
  decided_by uuid references auth.users (id),
  detail jsonb
    constraint case_assignments_detail_object
    check (detail is null or jsonb_typeof(detail) = 'object'),
  created_at timestamptz not null default now(),
  -- 관리자가 한 일(직접 배정·이관)에는 관리자가, 자동 배정에는 없다
  constraint case_assignments_decider_matches_method
    check ((method in ('manual', 'transfer')) = (decided_by is not null)),
  -- 이관만 이전 담당자가 있다
  constraint case_assignments_transfer_has_from
    check ((method = 'transfer') = (from_assignee_id is not null)),
  constraint case_assignments_transfer_changes_assignee
    check (from_assignee_id is distinct from to_assignee_id)
);

comment on table public.case_assignments is '배정 이력. 누가(에이전트·규칙·관리자) 어느 분야 담당자를 왜 정했는지. 쓰기·읽기는 BE만';
comment on column public.case_assignments.method is 'agent: 배정 에이전트 / rule: 에이전트 없이 규칙 / manual: 관리자 직접 배정 / transfer: 관리자 이관';
comment on column public.case_assignments.detail is '자동 배정이 본 것(판별한 지역, 후보 수와 업무량, 모델 이름 등)';

create index case_assignments_intake_created_idx
  on public.case_assignments (intake_id, created_at desc);
create index case_assignments_to_assignee_idx on public.case_assignments (to_assignee_id);

-- RLS: 켜 두고 정책을 주지 않는다. 브라우저 역할은 어떤 행도 보지 못한다(api_idempotency_keys와 같은 방식)
alter table public.case_intakes enable row level security;
alter table public.case_assignments enable row level security;

revoke all on table public.case_intakes, public.case_assignments from anon, authenticated;
grant all on table public.case_intakes, public.case_assignments to service_role;

-- 접수 기능이 생기기 전에 담당자가 직접 만든 사건도 관제 화면에 보이도록 접수 건을 만들어 둔다.
-- 이 뒤에 접수 없이 생긴 사건은 관리자 조회가 같은 방식으로 채운다(app/dispatch/repository.py).
insert into public.case_intakes
  (received_at, accident_at, location, description, status,
   property_assignee_id, injury_assignee_id, case_id)
select c.created_at, c.accident_date, '기록 없음', '접수 기능 전에 담당자가 직접 만든 사건', 'assigned',
       c.assignee_id, c.injury_assignee_id, c.id
from public.cases c;
