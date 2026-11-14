-- B(통합): 접수 건의 배정 참고 정보(case_intakes.details).
--
-- 배정 에이전트와 규칙 배정이 담당자를 고를 때 읽는다. 모양은 app/schemas/admin.py IntakeDetails다.
--   estimated_damage   예상 손해액(원)
--   vehicle_type       우리 계약 차량의 차종(passenger·imported·truck·motorcycle)
--   vehicle_count      사고에 관련된 차량 수
--   injury_severity    가장 크게 다친 사람의 정도(minor·serious·fatal). 다친 사람이 없으면 없다
--   policy             가입 담보(대인배상Ⅱ·대물 한도·자기차량손해·자기신체사고/자동차상해·무보험차상해)
--   policyholder       계약자 이력(최근 3년 사고 횟수·민원 횟수·장기 고객 여부)
-- 사람을 특정하는 정보(이름·주민등록번호·연락처·주소·차량 번호)는 넣지 않는다 — 배정에 쓰지 않고,
-- 이 값은 외부 모델(LLM)에 보내기 때문이다.
-- 값의 검증은 BE가 한다(접수 등록 요청). DB는 객체인지만 본다. 이 정보가 없던 접수 건은 빈 객체다.
-- 담당자 쪽 속성(경력·전문 분야·고액 사건 권한·부재)은 Supabase Auth의 app_metadata에 있다
-- (career_years·specialties·high_value·away) — 표를 따로 두지 않는다.

alter table public.case_intakes
  add column details jsonb not null default '{}'::jsonb
    constraint case_intakes_details_is_object check (jsonb_typeof(details) = 'object');

comment on column public.case_intakes.details is
  '배정 참고 정보(사고 규모·가입 담보·계약자 이력). 사람을 특정하는 정보는 넣지 않는다. 없으면 빈 객체';
