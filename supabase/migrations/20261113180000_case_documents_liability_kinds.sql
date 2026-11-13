-- C(김지은): 산정 서류(case_documents)에 대물·대인 담당자 입력 두 종류를 더한다.
--
-- injured_person: 다친 사람 1명의 상해 급수·치료관계비(누적)·운전자 여부. 진단서·영수증을 보고 담당자가 적는다.
-- property_damage_input: 대물 한도(우리 계약)·상대 차 차량가액·출고일(모두 선택).
-- 칸 계약은 docs/contracts/liability-input.md, 앱 쪽 허용값은 app/api/reports.py DOCUMENT_KINDS다.
-- 진단서·영수증 파일은 기존 evidence 행(doc_type medical_certificate·medical_bill)이라 여기서 바꾸지 않는다.
-- 머지된 20261111090000_review_reports.sql은 고치지 않고 이 파일에서 제약을 바꿔 건다.

alter table public.case_documents
  drop constraint case_documents_kind_check,
  add constraint case_documents_kind_check check (kind in (
    'repair_estimate', 'own_damage_contract', 'evidence',
    'injured_person', 'property_damage_input'));

comment on table public.case_documents is '산정·청구 서류. 견적서는 repair_estimate, 자차 계약은 own_damage_contract, 증빙은 evidence, 대인 입력은 injured_person, 대물 입력은 property_damage_input';
