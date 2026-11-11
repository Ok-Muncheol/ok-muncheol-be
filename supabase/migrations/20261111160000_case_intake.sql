-- A(정지원): 사건 접수 칼럼. POST /cases(app/api/cases.py create_case)가 쓰고 GET /cases·/cases/{id}가 읽는다.
--
-- 범위 검사(차대차 2대만)는 접수 API가 한다. 범위 밖 사건은 행을 만들지 않으므로 사고 유형·당사자 수 칼럼은 두지 않는다.
-- 기존 행이 있어도 적용되도록 새 칼럼은 null 또는 기본값을 둔다.

alter table public.cases
  add column accident_date timestamptz,
  add column input_path text not null default 'video'
    constraint cases_input_path_check check (input_path in ('video', 'manual'));

comment on column public.cases.accident_date is '사고 일시(담당자 입력). 모르면 null';
comment on column public.cases.input_path is 'video: 블랙박스 영상 분석으로 카드 생성 / manual: 담당자가 카드를 직접 작성';
