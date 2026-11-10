-- D(옥석현): 검토 초안 보고서·서류·면책 입력·협의 과실·구상 청구서.
--
-- 계약 원천: app/schemas/reports.py(ReviewReport·ClaimPackage), app/reports/repository.py(CaseRecord)
--   CaseRecord.reports        → review_reports (보고서 버전마다 1행, 그때 쓴 사실 카드 포함)
--   CaseRecord.documents      → case_documents (견적서·계약·증빙, 넣은 순서 유지)
--   CaseRecord.case_facts     ┐
--   CaseRecord.fault_agreement┘→ report_case_inputs (사건당 1행)
--   CaseRecord.packages       → claim_packages (확인 완료 때 만든 구상 청구서)
--
-- 원본(report·fact_card·body·package)은 jsonb가 아니라 json으로 둔다. jsonb는 객체 키 순서를 바꿔
-- 보고서 본문·청구서 HTML의 항목 순서가 다시 불러올 때 달라진다. 검사에 연산자를 쓰는 입력값만 jsonb다.
-- 쓰기는 BE만 한다(app/reports/repository.py PostgresReportRepository). 브라우저(authenticated)는 담당 사건 행을 읽기만 한다.
-- 사실 카드 버전 테이블(A)이 생기면 review_reports의 (case_id, fact_card_version) 외래키를 새 마이그레이션으로 추가한다.

-- 보고서 1버전 = ReviewReport 1개. 판단(judgment_runs)의 (case_id, judgment_version)을 참조한다.
-- 서류 추가·협의 과실로 입력이 바뀌면 새 버전 행을 더한다. 확인 완료는 그 행의 report만 갱신한다.
create table public.review_reports (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases (id),
  report_version integer not null
    constraint review_reports_report_version_check check (report_version >= 1),
  judgment_version integer not null,
  fact_card_version integer not null
    constraint review_reports_fact_card_version_check check (fact_card_version >= 1),
  -- ReviewReport 원본. 아래 생성 칼럼이 여기서 값을 꺼낸다(원천은 하나)
  report json not null
    constraint review_reports_report_object check (json_typeof(report) = 'object'),
  -- 이 버전을 만들 때 쓴 FactCard(재산정 때 다시 쓴다)
  fact_card json
    constraint review_reports_fact_card_object check (fact_card is null or json_typeof(fact_card) = 'object'),
  approval_status text generated always as (report ->> 'approval_status') stored
    constraint review_reports_approval_status_check check (approval_status in ('draft', 'approved')),
  created_by uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint review_reports_case_version_key unique (case_id, report_version),
  constraint review_reports_id_case_key unique (id, case_id),
  constraint review_reports_judgment_fk
    foreign key (case_id, judgment_version) references public.judgment_runs (case_id, judgment_version),
  constraint review_reports_report_versions check (
    report -> 'versions' ->> 'case_id' is not distinct from case_id::text
    and (report -> 'versions' ->> 'report_version')::integer is not distinct from report_version
    and (report -> 'versions' ->> 'judgment_version')::integer is not distinct from judgment_version
    and (report -> 'versions' ->> 'fact_card_version')::integer is not distinct from fact_card_version
  ),
  constraint review_reports_approved_has_actor check (
    approval_status <> 'approved' or (report ->> 'approved_by' is not null and report ->> 'approved_at' is not null)
  )
);

comment on table public.review_reports is '검토 초안 보고서 버전. 쓰기는 BE만. report.versions는 이 행의 버전과 같아야 한다';
comment on column public.review_reports.report is 'ReviewReport. 확인 완료(approval_status=approved)는 담당자 [확인 완료] 기록';
comment on column public.review_reports.fact_card is '이 버전을 만들 때 쓴 FactCard. 카드 버전 테이블(A)이 생기면 외래키로 바꾼다';

create index review_reports_judgment_idx on public.review_reports (case_id, judgment_version);

create trigger review_reports_set_updated_at
before update on public.review_reports
for each row execute function private.set_updated_at();

-- 서류(견적서·자차 계약·증빙). 같은 document_id는 한 번만(재전송 무시). seq가 넣은 순서다
create table public.case_documents (
  case_id uuid not null references public.cases (id),
  document_id text not null
    constraint case_documents_document_id_check check (char_length(document_id) between 1 and 200),
  seq bigint generated always as identity,
  kind text not null
    constraint case_documents_kind_check check (kind in ('repair_estimate', 'own_damage_contract', 'evidence')),
  body json not null
    constraint case_documents_body_object check (json_typeof(body) = 'object'),
  added_by uuid not null,
  created_at timestamptz not null default now(),
  constraint case_documents_pkey primary key (case_id, document_id),
  constraint case_documents_body_matches check (
    body ->> 'document_id' = document_id and body ->> 'kind' = kind
  )
);

comment on table public.case_documents is '산정·청구 서류. 견적서는 repair_estimate, 자차 계약은 own_damage_contract, 증빙은 evidence';

create index case_documents_case_seq_idx on public.case_documents (case_id, seq);

-- 사건당 입력 1행: 면책 체크(case_facts: 무면허·음주·고의 → true/false/null)와 협의 과실
create table public.report_case_inputs (
  case_id uuid primary key references public.cases (id),
  case_facts jsonb not null default '{}'::jsonb
    constraint report_case_inputs_case_facts_object check (jsonb_typeof(case_facts) = 'object'),
  -- {"agreed_counterparty_fault": int | null, "fault_change_reason": text}. 빈 객체 = 협의 없음, null 값 = 협의 해제 기록
  fault_agreement jsonb not null default '{}'::jsonb
    constraint report_case_inputs_fault_agreement_object check (jsonb_typeof(fault_agreement) = 'object'),
  updated_by uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint report_case_inputs_facts_keys check (
    case_facts - array['무면허', '음주', '고의'] = '{}'::jsonb
  ),
  constraint report_case_inputs_agreed_fault_range check (
    fault_agreement ->> 'agreed_counterparty_fault' is null
    or (fault_agreement ->> 'agreed_counterparty_fault')::integer between 0 and 100
  ),
  constraint report_case_inputs_agreement_has_reason check (
    fault_agreement = '{}'::jsonb or coalesce(fault_agreement ->> 'fault_change_reason', '') <> ''
  )
);

comment on table public.report_case_inputs is '사건별 산정 입력: 면책 체크박스(case_facts)와 협의 과실. 쓰기는 BE만';

create trigger report_case_inputs_set_updated_at
before update on public.report_case_inputs
for each row execute function private.set_updated_at();

-- 구상 청구서(ClaimPackage). 확인 완료한 보고서 버전에서 만든다. 같은 입력(idempotency_key)은 한 번만
create table public.claim_packages (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null,
  report_id uuid not null,
  report_version integer not null,
  package_version integer not null
    constraint claim_packages_package_version_check check (package_version >= 1),
  idempotency_key text not null,
  package json not null
    constraint claim_packages_package_object check (json_typeof(package) = 'object'),
  created_by uuid not null,
  created_at timestamptz not null default now(),
  constraint claim_packages_case_version_key unique (case_id, package_version),
  constraint claim_packages_case_idempotency_key unique (case_id, idempotency_key),
  constraint claim_packages_report_fk
    foreign key (report_id, case_id) references public.review_reports (id, case_id),
  constraint claim_packages_body_matches check (
    package ->> 'case_id' = case_id::text
    and (package ->> 'report_version')::integer = report_version
    and (package ->> 'package_version')::integer = package_version
    and package ->> 'idempotency_key' = idempotency_key
  )
);

comment on table public.claim_packages is '구상 청구서. 담당자 확인 완료 때 만든다. 같은 입력으로 다시 확인하면 새 행을 만들지 않는다';

create index claim_packages_report_idx on public.claim_packages (case_id, report_version);

-- RLS: 브라우저 역할은 담당 사건의 행만 읽는다
alter table public.review_reports enable row level security;
alter table public.case_documents enable row level security;
alter table public.report_case_inputs enable row level security;
alter table public.claim_packages enable row level security;

create policy review_reports_select_assignee on public.review_reports
for select to authenticated using (private.can_access_case(case_id));

create policy case_documents_select_assignee on public.case_documents
for select to authenticated using (private.can_access_case(case_id));

create policy report_case_inputs_select_assignee on public.report_case_inputs
for select to authenticated using (private.can_access_case(case_id));

create policy claim_packages_select_assignee on public.claim_packages
for select to authenticated using (private.can_access_case(case_id));

revoke all on table
  public.review_reports, public.case_documents, public.report_case_inputs, public.claim_packages
from anon, authenticated;

grant select on table
  public.review_reports, public.case_documents, public.report_case_inputs, public.claim_packages
to authenticated;

grant all on table
  public.review_reports, public.case_documents, public.report_case_inputs, public.claim_packages
to service_role;
