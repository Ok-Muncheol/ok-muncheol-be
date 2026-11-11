-- A(정지원): 사실 카드 버전. 카드 한 버전 = 한 행이고, 쌓인 행이 곧 수정 이력이다.
--
-- 계약 원천: app/schemas/facts.py(FactCard), docs/contracts/fact-card-v1.md, 서비스 app/api/facts.py
--   origin = 'vision'  비전 파이프라인이 만든 카드(created_by 없음)
--            'manual'  담당자가 직접 만든 카드
--            'edit'    담당자 수정(PATCH) 또는 구분 질문 답 반영. 새 버전은 늘 draft(기존 확정을 잇지 않는다)
-- 확정(confirm)은 그 버전 행의 card만 바꾼다(status·confirmed_by·confirmed_at). 확정된 행은 다시 바꾸지 않는다.
-- 원본 card는 json(키 순서 유지, review_reports와 같은 이유). 쓰기는 BE만, 브라우저는 담당 사건 행을 읽기만 한다.
-- 판단(judgment_runs)·보고서(review_reports)의 (case_id, fact_card_version) 외래키는 통합 뒤 새 파일로 더한다.

create table public.fact_cards (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases (id),
  version integer not null
    constraint fact_cards_version_check check (version >= 1),
  card json not null
    constraint fact_cards_card_object check (json_typeof(card) = 'object'),
  status text generated always as (card ->> 'status') stored
    constraint fact_cards_status_check check (status in ('draft', 'confirmed')),
  origin text not null
    constraint fact_cards_origin_check check (origin in ('vision', 'manual', 'edit')),
  reason text,
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint fact_cards_case_version_key unique (case_id, version),
  constraint fact_cards_id_case_key unique (id, case_id),
  constraint fact_cards_card_matches check (
    card ->> 'case_id' = case_id::text and (card ->> 'version')::integer = version
  ),
  constraint fact_cards_confirmed_has_actor check (
    status <> 'confirmed' or (card ->> 'confirmed_by' is not null and card ->> 'confirmed_at' is not null)
  ),
  -- 비전 카드만 만든 사람이 없다. 담당자가 만든 버전은 누가 왜 바꿨는지 남긴다
  constraint fact_cards_actor_by_origin check (
    (origin = 'vision' and created_by is null)
    or (origin <> 'vision' and created_by is not null)
  ),
  constraint fact_cards_edit_has_reason check (origin <> 'edit' or coalesce(reason, '') <> '')
);

comment on table public.fact_cards is '사실 카드 버전(FactCard). 수정은 새 행, 확정은 그 행의 card만 갱신. 쓰기는 BE만';
comment on column public.fact_cards.card is 'FactCard v1(영어 12칸). card.version = version, card.case_id = case_id';
comment on column public.fact_cards.origin is 'vision: 비전 파이프라인 / manual: 담당자 직접 작성 / edit: 담당자 수정·질문 답 반영';

create trigger fact_cards_set_updated_at
before update on public.fact_cards
for each row execute function private.set_updated_at();

-- RLS: 브라우저 역할은 담당 사건의 카드만 읽는다
alter table public.fact_cards enable row level security;

create policy fact_cards_select_assignee on public.fact_cards
for select to authenticated using (private.can_access_case(case_id));

revoke all on table public.fact_cards from anon, authenticated;
grant select on table public.fact_cards to authenticated;
grant all on table public.fact_cards to service_role;

-- Realtime 변경 알림: 비전 분석이 끝나 카드가 생기거나 확정되면 화면이 다시 읽는다
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime' and not puballtables) then
    alter publication supabase_realtime add table public.fact_cards;
  end if;
end;
$$;
