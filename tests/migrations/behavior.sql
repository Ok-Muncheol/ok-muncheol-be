-- 마이그레이션 동작 검사. supabase_stub.sql → supabase/migrations/*.sql(파일명 순) 다음에 돈다.
-- 기대와 다르면 예외로 멈춘다. tests/migrations/test_migrations_apply.py가 임시 데이터베이스에서만 실행한다.
--
-- 사용자 A 00000000-0000-0000-0000-00000000000a  사건 1 10000000-0000-0000-0000-000000000001
-- 사용자 B 00000000-0000-0000-0000-00000000000b  사건 2 10000000-0000-0000-0000-000000000002
-- 카드 버전: 사건 1 v1~v3, 사건 2 v1(판단·응답·보고서가 가리킨다)
-- 실행 r1·r2·r3 2000…0001~0003(사건 1), 질문 q1·q2 3000…0001~0002

\set ON_ERROR_STOP on
set client_min_messages = warning;

create schema test_helpers;
grant usage on schema test_helpers to public;

create function test_helpers.claims(sub uuid) returns text
language sql immutable as $$
  select case when sub is null then '' else json_build_object('sub', sub, 'role', 'authenticated')::text end
$$;

-- 역할·JWT 주체를 바꿔 숫자 하나를 돌려주는 SQL을 실행한다. role_name이 null이면 관리자로 실행한다
create function test_helpers.scalar_as(role_name text, sub uuid, stmt text) returns bigint
language plpgsql as $$
declare
  answer bigint;
begin
  perform set_config('request.jwt.claims', test_helpers.claims(sub), true);
  if role_name is not null then
    execute format('set local role %I', role_name);
  end if;
  execute stmt into answer;
  execute 'reset role';
  return answer;
end;
$$;

create function test_helpers.expect_error(
  role_name text, sub uuid, stmt text, expected_state text, label text
) returns void
language plpgsql as $$
begin
  begin
    perform set_config('request.jwt.claims', test_helpers.claims(sub), true);
    if role_name is not null then
      execute format('set local role %I', role_name);
    end if;
    execute stmt;
  exception when others then
    if sqlstate <> expected_state then
      raise exception '[%] SQLSTATE %를 기대했지만 %: %', label, expected_state, sqlstate, sqlerrm;
    end if;
    return;
  end;
  raise exception '[%] SQLSTATE %로 실패해야 하는데 성공했다', label, expected_state;
end;
$$;

create function test_helpers.assert_equal(actual bigint, expected bigint, label text) returns void
language plpgsql as $$
begin
  if actual is distinct from expected then
    raise exception '[%] %를 기대했지만 %', label, expected, actual;
  end if;
end;
$$;

-- JudgmentResult 모양의 결과
create function test_helpers.result(
  case_id uuid, card integer, judgment integer, status text, chart text, fa integer, fb integer
) returns jsonb
language sql immutable as $$
  select jsonb_build_object(
    'versions', jsonb_build_object(
      'case_id', case_id, 'fact_card_version', card, 'judgment_version', judgment, 'report_version', null),
    'status', status, 'chart_id', chart, 'edition', case when chart is null then null else '2023' end,
    'fault_A', fa, 'fault_B', fb, 'calculation_log', '[]'::jsonb, 'evidence', '[]'::jsonb,
    'checks', '[]'::jsonb, 'questions', '[]'::jsonb, 'reasons', '[]'::jsonb)
$$;

-- FactCard 모양의 카드(fact_cards.card)
create function test_helpers.card(case_id uuid, version integer, status text, confirmed_by uuid) returns json
language sql immutable as $$
  select json_build_object('case_id', case_id, 'version', version, 'status', status, 'fields', '{}'::json,
    'confirmed_by', confirmed_by, 'confirmed_at', case when confirmed_by is null then null else '2026-11-11T15:00:00Z' end)
$$;

insert into auth.users (id) values
  ('00000000-0000-0000-0000-00000000000a'), ('00000000-0000-0000-0000-00000000000b');
insert into public.cases (id, assignee_id) values
  ('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-00000000000a'),
  ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-00000000000b');
-- 판단·응답·보고서는 실제 카드 버전만 가리킨다(10절). 카드 자체의 제약은 8절에서 본다
insert into public.fact_cards (case_id, version, card, origin) values
  ('10000000-0000-0000-0000-000000000001', 1,
   test_helpers.card('10000000-0000-0000-0000-000000000001', 1, 'draft', null), 'vision');
insert into public.fact_cards (case_id, version, card, origin, reason, created_by) values
  ('10000000-0000-0000-0000-000000000001', 2,
   test_helpers.card('10000000-0000-0000-0000-000000000001', 2, 'confirmed', '00000000-0000-0000-0000-00000000000a'),
   'edit', '영상 다시 봄', '00000000-0000-0000-0000-00000000000a'),
  ('10000000-0000-0000-0000-000000000001', 3,
   test_helpers.card('10000000-0000-0000-0000-000000000001', 3, 'confirmed', '00000000-0000-0000-0000-00000000000a'),
   'edit', '구분 질문 답 반영', '00000000-0000-0000-0000-00000000000a'),
  ('10000000-0000-0000-0000-000000000002', 1,
   test_helpers.card('10000000-0000-0000-0000-000000000002', 1, 'confirmed', '00000000-0000-0000-0000-00000000000b'),
   'manual', null, '00000000-0000-0000-0000-00000000000b');
insert into public.judgment_runs
  (id, case_id, judgment_version, fact_card_version, criteria_version, rule_version, requested_by)
values ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001',
        1, 1, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000a');

-- 1. 담당자만 자기 사건의 행을 본다
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000a',
  'select count(*) from public.cases'), 1, 'A는 자기 사건 1건만 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000a',
  'select count(*) from public.judgment_runs'), 1, 'A는 자기 사건의 판단을 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000b',
  'select count(*) from public.judgment_runs'), 0, 'B는 A 사건의 판단을 못 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', null,
  'select count(*) from public.cases'), 0, '주체 없는 토큰은 사건을 못 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000a',
  $sql$select count(*) from (select private.can_access_case('10000000-0000-0000-0000-000000000001') as ok) t where ok$sql$),
  1, '접근 함수: 자기 사건');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000a',
  $sql$select count(*) from (select private.can_access_case('10000000-0000-0000-0000-000000000002') as ok) t where ok$sql$),
  0, '접근 함수: 남의 사건');

-- 2. 브라우저 역할의 쓰기와 비공개 테이블은 막힌다. service_role은 쓴다
select test_helpers.expect_error('authenticated', '00000000-0000-0000-0000-00000000000a', $sql$
  insert into public.judgment_runs (case_id, judgment_version, fact_card_version, criteria_version, rule_version, requested_by)
  values ('10000000-0000-0000-0000-000000000001', 9, 9, 'c', 'r', '00000000-0000-0000-0000-00000000000a')
$sql$, '42501', '브라우저 역할은 판단을 쓸 수 없다');
select test_helpers.expect_error('authenticated', '00000000-0000-0000-0000-00000000000a',
  'update public.cases set updated_at = now()', '42501', '브라우저 역할은 사건을 고칠 수 없다');
select test_helpers.expect_error('authenticated', '00000000-0000-0000-0000-00000000000a',
  'delete from public.judgment_runs', '42501', '브라우저 역할은 판단을 지울 수 없다');
select test_helpers.expect_error('anon', null,
  'select count(*) from public.cases', '42501', 'anon은 사건을 읽을 수 없다');
select test_helpers.expect_error('authenticated', '00000000-0000-0000-0000-00000000000a',
  'select count(*) from public.api_idempotency_keys', '42501', '멱등성 키는 브라우저가 못 읽는다');
select test_helpers.expect_error('anon', null,
  $sql$select private.can_access_case('10000000-0000-0000-0000-000000000001')$sql$, '42501', 'anon은 접근 함수를 못 부른다');
select test_helpers.assert_equal(test_helpers.scalar_as('service_role', null, $sql$
  with inserted as (
    insert into public.api_idempotency_keys (actor_id, scope, key, request_hash, case_id)
    values ('00000000-0000-0000-0000-00000000000a', 'POST /cases/{case_id}/judgments', 'key-00000001',
            'sha256:abc', '10000000-0000-0000-0000-000000000001')
    returning 1)
  select count(*) from inserted
$sql$), 1, 'service_role은 멱등성 키를 쓴다');
select test_helpers.expect_error('service_role', null, $sql$
  insert into public.api_idempotency_keys (actor_id, scope, key, request_hash)
  values ('00000000-0000-0000-0000-00000000000a', 'POST /cases/{case_id}/judgments', 'key-00000001', 'sha256:def')
$sql$, '23505', '같은 멱등성 키는 한 번만 저장한다');
select test_helpers.expect_error('service_role', null, $sql$
  insert into public.api_idempotency_keys (actor_id, scope, key, request_hash)
  values ('00000000-0000-0000-0000-00000000000a', 'POST /cases/{case_id}/judgments', 'short', 'h')
$sql$, '23514', '멱등성 키는 8자 이상');

-- 3. 판정 결과 제약: 재실행 상한·결과/상태 짝·A+B=100·버전 일치·판정 상태 값
select test_helpers.expect_error(null, null, $sql$
  update public.judgment_runs set rebuttal_retry_count = 2 where id = '20000000-0000-0000-0000-000000000001'
$sql$, '23514', '반박 재실행은 1회까지');
select test_helpers.expect_error(null, null, $sql$
  update public.judgment_runs set status = 'completed', completed_at = now()
  where id = '20000000-0000-0000-0000-000000000001'
$sql$, '23514', '완료에는 판정 결과가 있어야 한다');
select test_helpers.expect_error(null, null, $sql$
  update public.judgment_runs set result = test_helpers.result('10000000-0000-0000-0000-000000000001', 1, 1, 'draft_ready', '차12-1', 70, 30)
  where id = '20000000-0000-0000-0000-000000000001'
$sql$, '23514', '진행 중인 실행에는 결과가 없다');
select test_helpers.expect_error(null, null, $sql$
  update public.judgment_runs set status = 'completed', completed_at = now(),
    result = test_helpers.result('10000000-0000-0000-0000-000000000001', 1, 1, 'draft_ready', '차12-1', 60, 30)
  where id = '20000000-0000-0000-0000-000000000001'
$sql$, '23514', 'A+B=100');
select test_helpers.expect_error(null, null, $sql$
  update public.judgment_runs set status = 'completed', completed_at = now(),
    result = test_helpers.result('10000000-0000-0000-0000-000000000001', 1, 1, 'draft_ready', '차12-1', null, null)
  where id = '20000000-0000-0000-0000-000000000001'
$sql$, '23514', '초안 판정에는 과실비율이 있어야 한다');
select test_helpers.expect_error(null, null, $sql$
  update public.judgment_runs set status = 'completed', completed_at = now(),
    result = test_helpers.result('10000000-0000-0000-0000-000000000001', 2, 1, 'draft_ready', '차12-1', 70, 30)
  where id = '20000000-0000-0000-0000-000000000001'
$sql$, '23514', '결과의 카드 버전은 실행의 입력 버전과 같다');
select test_helpers.expect_error(null, null, $sql$
  update public.judgment_runs set status = 'completed', completed_at = now(),
    result = test_helpers.result('10000000-0000-0000-0000-000000000001', 1, 1, 'approved', '차12-1', 70, 30)
  where id = '20000000-0000-0000-0000-000000000001'
$sql$, '23514', '판정 상태는 세 가지뿐');
select test_helpers.expect_error(null, null, $sql$
  update public.judgment_runs set status = 'failed', completed_at = now()
  where id = '20000000-0000-0000-0000-000000000001'
$sql$, '23514', '실패에는 오류가 있어야 한다');
select test_helpers.expect_error(null, null, $sql$
  update public.judgment_runs set error = '{"code": "x"}' where id = '20000000-0000-0000-0000-000000000001'
$sql$, '23514', '오류는 ApiError 모양');
update public.judgment_runs set status = 'completed', stage = 'build_judgment_result', completed_at = now(),
  result = test_helpers.result('10000000-0000-0000-0000-000000000001', 1, 1, 'draft_ready', '차12-1', 70, 30)
where id = '20000000-0000-0000-0000-000000000001';
select test_helpers.assert_equal((select count(*) from public.judgment_runs
  where id = '20000000-0000-0000-0000-000000000001' and judgment_status = 'draft_ready'
    and chart_id = '차12-1' and fault_a = 70 and fault_b = 30), 1, '정상 완료는 결과에서 판정 칼럼을 만든다');

-- 4. 중복 실행 방지: 같은 입력 재판단 금지·사건마다 진행 중 하나·판단 버전 유일. 실패한 실행은 막지 않는다
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_runs (case_id, judgment_version, fact_card_version, criteria_version, rule_version, requested_by)
  values ('10000000-0000-0000-0000-000000000001', 2, 1, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000a')
$sql$, '23505', '같은 입력으로 다시 판단하지 않는다');
insert into public.judgment_runs
  (id, case_id, judgment_version, fact_card_version, criteria_version, rule_version, requested_by)
values ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001',
        2, 2, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000a');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_runs (case_id, judgment_version, fact_card_version, criteria_version, rule_version, requested_by)
  values ('10000000-0000-0000-0000-000000000001', 3, 3, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000a')
$sql$, '23505', '사건마다 진행 중인 판단은 하나');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_runs (case_id, judgment_version, fact_card_version, criteria_version, rule_version,
    requested_by, status, error, completed_at)
  values ('10000000-0000-0000-0000-000000000001', 2, 9, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000a',
    'failed', '{"code": "x", "message": "m", "retryable": false}', now())
$sql$, '23505', '판단 버전은 사건마다 유일');
insert into public.judgment_runs
  (case_id, judgment_version, fact_card_version, criteria_version, rule_version, requested_by, status, error, completed_at)
values ('10000000-0000-0000-0000-000000000002', 1, 1, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000b',
        'failed', '{"code": "upstream_timeout", "message": "검색 시간 초과", "retryable": true}', now());
insert into public.judgment_runs
  (case_id, judgment_version, fact_card_version, criteria_version, rule_version, requested_by)
values ('10000000-0000-0000-0000-000000000002', 2, 1, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000b');

-- 5. 구분 질문·응답과 대체·재개
update public.judgment_runs set status = 'waiting_for_confirmation', stage = 'ask_or_resume'
where id = '20000000-0000-0000-0000-000000000002';
insert into public.judgment_questions (id, run_id, case_id, field_key, text, options)
values ('30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000002',
        '10000000-0000-0000-0000-000000000001', 'signal_A', 'A 차량의 신호는?', array['green', 'yellow', 'red']);
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_questions (run_id, case_id, field_key, text)
  values ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000002', 'signal_B', '?')
$sql$, '23503', '질문의 사건은 실행의 사건과 같다');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_questions (run_id, case_id, field_key, text)
  values ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'signal_A', '?')
$sql$, '23505', '실행마다 칸 하나에 질문 하나');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_answers (question_id, case_id, expected_card_version, value, unknown, answered_by)
  values ('30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 2, 'red', true,
          '00000000-0000-0000-0000-00000000000a')
$sql$, '23514', '모른다면 값을 비운다');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_answers (question_id, case_id, expected_card_version, value, unknown, answered_by)
  values ('30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 2, null, false,
          '00000000-0000-0000-0000-00000000000a')
$sql$, '23514', '모르는 게 아니면 값이 있다');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_answers (question_id, case_id, expected_card_version, value, resulting_card_version, answered_by)
  values ('30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 2, 'red', 2,
          '00000000-0000-0000-0000-00000000000a')
$sql$, '23514', '응답을 반영한 카드는 새 버전');
insert into public.judgment_answers (question_id, case_id, expected_card_version, value, resulting_card_version, answered_by)
values ('30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 2, 'red', 3,
        '00000000-0000-0000-0000-00000000000a');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_answers (question_id, case_id, expected_card_version, value, answered_by)
  values ('30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 2, 'green',
          '00000000-0000-0000-0000-00000000000a')
$sql$, '23505', '질문마다 응답은 한 번');
update public.judgment_runs set superseded_at = now() where id = '20000000-0000-0000-0000-000000000002';
insert into public.judgment_runs
  (id, case_id, judgment_version, fact_card_version, criteria_version, rule_version, requested_by, resumed_from_run_id)
values ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 3, 3,
        'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000a', '20000000-0000-0000-0000-000000000002');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_runs (case_id, judgment_version, fact_card_version, criteria_version, rule_version,
    requested_by, status, error, completed_at, resumed_from_run_id)
  values ('10000000-0000-0000-0000-000000000001', 4, 4, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000a',
    'failed', '{"code": "x", "message": "m", "retryable": false}', now(), '20000000-0000-0000-0000-000000000002')
$sql$, '23505', '한 실행은 한 번만 이어진다');
-- 사건 2의 실제 카드 버전(v1)을 써서 실행 잇기 외래키만 걸리게 한다
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_runs (case_id, judgment_version, fact_card_version, criteria_version, rule_version,
    requested_by, status, error, completed_at, resumed_from_run_id)
  values ('10000000-0000-0000-0000-000000000002', 3, 1, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000b',
    'failed', '{"code": "x", "message": "m", "retryable": false}', now(), '20000000-0000-0000-0000-000000000003')
$sql$, '23503', '다른 사건의 실행을 잇지 않는다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000a',
  'select count(*) from public.judgment_questions'), 1, 'A는 자기 사건의 질문을 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000a',
  'select count(*) from public.judgment_answers'), 1, 'A는 자기 사건의 응답을 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000b',
  'select count(*) from public.judgment_questions'), 0, 'B는 A 사건의 질문을 못 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000b',
  'select count(*) from public.judgment_answers'), 0, 'B는 A 사건의 응답을 못 본다');

-- 6. 검증 3겹 로그
insert into public.validation_checks (run_id, case_id, attempt, layer, passed)
values ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 0, 'evidence', true);
insert into public.validation_checks (run_id, case_id, attempt, layer, passed, reasons)
values ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 0, 'rebuttal', false,
        array['차순위 도표의 구분 조건이 카드에 없다']);
select test_helpers.expect_error(null, null, $sql$
  insert into public.validation_checks (run_id, case_id, attempt, layer, passed)
  values ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 0, 'numerical', false)
$sql$, '23514', '실패한 검증에는 이유가 있다');
select test_helpers.expect_error(null, null, $sql$
  insert into public.validation_checks (run_id, case_id, attempt, layer, passed)
  values ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 2, 'evidence', true)
$sql$, '23514', '재실행 상한을 넘는 시도는 없다');
select test_helpers.expect_error(null, null, $sql$
  insert into public.validation_checks (run_id, case_id, attempt, layer, passed)
  values ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 0, 'evidence', true)
$sql$, '23505', '시도·겹마다 검증 결과는 하나');
select test_helpers.expect_error(null, null, $sql$
  insert into public.validation_checks (run_id, case_id, attempt, layer, passed)
  values ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 1, 'style', true)
$sql$, '23514', '검증 겹은 세 가지뿐');
select test_helpers.expect_error(null, null, $sql$
  insert into public.validation_checks (run_id, case_id, attempt, layer, passed)
  values ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000002', 1, 'evidence', true)
$sql$, '23503', '검증 로그의 사건은 실행의 사건과 같다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000a',
  'select count(*) from public.validation_checks'), 2, 'A는 자기 사건의 검증 로그를 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000b',
  'select count(*) from public.validation_checks'), 0, 'B는 A 사건의 검증 로그를 못 본다');

-- 7. 플랫폼 설정: RLS 전부·브라우저 쓰기 권한 없음·Realtime·비공개 스키마·updated_at
select test_helpers.assert_equal((select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity), 0, 'public 테이블은 모두 RLS');
select test_helpers.assert_equal((
  select count(*) from (values ('public.cases'), ('public.judgment_runs'), ('public.judgment_questions'),
                               ('public.judgment_answers'), ('public.validation_checks')) t(name),
                       (values ('insert'), ('update'), ('delete'), ('truncate')) p(priv)
  where has_table_privilege('authenticated', t.name, p.priv)), 0, 'authenticated에는 쓰기 권한이 없다');
create table public.zz_default_privilege_probe (id integer);
select test_helpers.assert_equal((select count(*) from (select 1) t
  where has_table_privilege('authenticated', 'public.zz_default_privilege_probe', 'insert')), 1,
  '대조군: 스텁의 기본 권한이 살아 있어 위 검사가 의미 있다');
drop table public.zz_default_privilege_probe;
select test_helpers.assert_equal((select count(*) from pg_publication_tables
  where pubname = 'supabase_realtime' and schemaname = 'public'
    and tablename in ('judgment_runs', 'judgment_questions')), 2, 'Realtime은 판단 상태와 질문을 알린다');
select test_helpers.assert_equal((select count(*) from (values ('anon'), ('authenticated')) r(name)
  where has_schema_privilege(r.name, 'langgraph', 'usage')), 0, 'langgraph 스키마는 브라우저 역할이 못 쓴다');
select test_helpers.assert_equal((select count(*) from (values ('anon'), ('authenticated')) r(name)
  where has_schema_privilege(r.name, 'langgraph', 'create')), 0, 'langgraph 스키마에 브라우저 역할이 못 만든다');
update public.judgment_runs set stage = 'check_chart_scope' where id = '20000000-0000-0000-0000-000000000003';
select test_helpers.assert_equal((select count(*) from public.judgment_runs
  where id = '20000000-0000-0000-0000-000000000003' and updated_at > created_at), 1, 'updated_at 트리거');

-- 8. 사실 카드 버전(A): 담당자만 읽기·브라우저 쓰기 금지·버전 유일·카드와 행 일치·확정 기록·만든 사람·Realtime
-- 카드 행은 판단보다 먼저 맨 앞에서 넣었다(사건 1 v1~v3, 사건 2 v1)
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000a',
  'select count(*) from public.fact_cards'), 3, 'A는 자기 사건의 카드 버전을 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000b',
  'select count(*) from public.fact_cards'), 1, 'B는 자기 사건의 카드만 보고 A 사건의 카드는 못 본다');
select test_helpers.assert_equal((select count(*) from public.fact_cards where status = 'confirmed'), 3,
  'status는 card.status에서 만든다');
select test_helpers.expect_error('authenticated', '00000000-0000-0000-0000-00000000000a', $sql$
  update public.fact_cards set reason = 'x'
$sql$, '42501', '브라우저 역할은 카드를 고칠 수 없다');
select test_helpers.expect_error('service_role', null, $sql$
  insert into public.fact_cards (case_id, version, card, origin) values ('10000000-0000-0000-0000-000000000001', 1,
    test_helpers.card('10000000-0000-0000-0000-000000000001', 1, 'draft', null), 'vision')
$sql$, '23505', '같은 사건의 같은 카드 버전은 하나');
select test_helpers.expect_error('service_role', null, $sql$
  insert into public.fact_cards (case_id, version, card, origin) values ('10000000-0000-0000-0000-000000000001', 4,
    test_helpers.card('10000000-0000-0000-0000-000000000001', 5, 'draft', null), 'vision')
$sql$, '23514', 'card.version은 행의 버전과 같다');
select test_helpers.expect_error('service_role', null, $sql$
  insert into public.fact_cards (case_id, version, card, origin, reason, created_by) values ('10000000-0000-0000-0000-000000000001', 4,
    test_helpers.card('10000000-0000-0000-0000-000000000001', 4, 'confirmed', null), 'edit', 'r',
    '00000000-0000-0000-0000-00000000000a')
$sql$, '23514', '확정 카드는 확정한 사람·시각이 있다');
select test_helpers.expect_error('service_role', null, $sql$
  insert into public.fact_cards (case_id, version, card, origin) values ('10000000-0000-0000-0000-000000000001', 4,
    test_helpers.card('10000000-0000-0000-0000-000000000001', 4, 'draft', null), 'edit')
$sql$, '23514', '담당자 수정은 만든 사람과 사유가 있다');
select test_helpers.assert_equal((select count(*) from (values ('insert'), ('update'), ('delete'), ('truncate')) p(priv)
  where has_table_privilege('authenticated', 'public.fact_cards', p.priv)), 0, 'authenticated는 카드를 쓸 수 없다');
select test_helpers.assert_equal((select count(*) from pg_publication_tables
  where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'fact_cards'), 1,
  'Realtime은 카드 생성·확정을 알린다');

-- 9. 영상 분석 작업(A): 사건 폴더 영상만·진행 중 하나·같은 키 한 번·완료는 카드 버전·실패는 오류·카드 외래키
insert into public.analysis_tasks (id, case_id, storage_path, idempotency_key, callback_token_hash, requested_by)
values ('40000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001',
        'case-files/10000000-0000-0000-0000-000000000001/videos/a.mp4', 'analysis-0001', repeat('a', 64),
        '00000000-0000-0000-0000-00000000000a');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000a',
  'select count(*) from public.analysis_tasks'), 1, 'A는 자기 사건의 분석 작업을 본다');
select test_helpers.assert_equal(test_helpers.scalar_as('authenticated', '00000000-0000-0000-0000-00000000000b',
  'select count(*) from public.analysis_tasks'), 0, 'B는 A 사건의 분석 작업을 못 본다');
select test_helpers.expect_error('service_role', null, $sql$
  insert into public.analysis_tasks (case_id, storage_path, idempotency_key, callback_token_hash, requested_by)
  values ('10000000-0000-0000-0000-000000000001', 'case-files/10000000-0000-0000-0000-000000000001/videos/b.mp4',
          'analysis-0002', repeat('b', 64), '00000000-0000-0000-0000-00000000000a')
$sql$, '23505', '사건마다 진행 중인 분석은 하나');
select test_helpers.expect_error('service_role', null, $sql$
  insert into public.analysis_tasks (case_id, storage_path, idempotency_key, callback_token_hash, requested_by, status, error, completed_at)
  values ('10000000-0000-0000-0000-000000000001', 'case-files/10000000-0000-0000-0000-000000000002/videos/b.mp4',
          'analysis-0003', repeat('b', 64), '00000000-0000-0000-0000-00000000000a', 'failed',
          '{"code": "x", "message": "m", "retryable": false}', now())
$sql$, '23514', '다른 사건 폴더의 영상은 분석하지 않는다');
select test_helpers.expect_error('service_role', null, $sql$
  update public.analysis_tasks set status = 'completed', completed_at = now()
  where id = '40000000-0000-0000-0000-000000000001'
$sql$, '23514', '완료한 작업은 카드 버전이 있다');
select test_helpers.expect_error('service_role', null, $sql$
  update public.analysis_tasks set status = 'completed', completed_at = now(), fact_card_version = 9
  where id = '40000000-0000-0000-0000-000000000001'
$sql$, '23503', '완료 카드 버전은 실제 카드 버전이다');
select test_helpers.expect_error('service_role', null, $sql$
  update public.analysis_tasks set status = 'failed', completed_at = now()
  where id = '40000000-0000-0000-0000-000000000001'
$sql$, '23514', '실패한 작업은 오류가 있다');
update public.analysis_tasks set status = 'completed', completed_at = now(), fact_card_version = 1
where id = '40000000-0000-0000-0000-000000000001';
select test_helpers.expect_error('service_role', null, $sql$
  insert into public.analysis_tasks (case_id, storage_path, idempotency_key, callback_token_hash, requested_by)
  values ('10000000-0000-0000-0000-000000000001', 'case-files/10000000-0000-0000-0000-000000000001/videos/c.mp4',
          'analysis-0001', repeat('c', 64), '00000000-0000-0000-0000-00000000000a')
$sql$, '23505', '같은 사건의 같은 Idempotency-Key는 한 번');
select test_helpers.assert_equal((select count(*) from (values ('insert'), ('update'), ('delete'), ('truncate')) p(priv)
  where has_table_privilege('authenticated', 'public.analysis_tasks', p.priv)), 0, 'authenticated는 분석 작업을 쓸 수 없다');
select test_helpers.assert_equal((select count(*) from pg_publication_tables
  where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'analysis_tasks'), 1,
  'Realtime은 분석 작업 상태를 알린다');

-- 10. 카드 버전 외래키(B): 판단·응답·보고서는 그 사건의 실제 카드 버전만 가리키고, 가리킨 카드 버전은 지울 수 없다
-- 판단은 실패한 실행으로 넣는다. 사건마다 진행 중 하나·같은 입력 재판단 제약을 피해서 카드 외래키만 걸리게 한다
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_runs (case_id, judgment_version, fact_card_version, criteria_version, rule_version,
    requested_by, status, error, completed_at)
  values ('10000000-0000-0000-0000-000000000001', 9, 9, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000a',
    'failed', '{"code": "x", "message": "m", "retryable": false}', now())
$sql$, '23503', '판단의 카드 버전은 실제 카드 버전이다');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_runs (case_id, judgment_version, fact_card_version, criteria_version, rule_version,
    requested_by, status, error, completed_at)
  values ('10000000-0000-0000-0000-000000000002', 9, 2, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000b',
    'failed', '{"code": "x", "message": "m", "retryable": false}', now())
$sql$, '23503', '다른 사건의 카드 버전은 가리킬 수 없다');
insert into public.judgment_runs
  (case_id, judgment_version, fact_card_version, criteria_version, rule_version, requested_by, status, error, completed_at)
values ('10000000-0000-0000-0000-000000000002', 9, 1, 'criteria-2023', 'rules-v1', '00000000-0000-0000-0000-00000000000b',
        'failed', '{"code": "x", "message": "m", "retryable": false}', now());
select test_helpers.assert_equal((select count(*) from public.judgment_runs
  where case_id = '10000000-0000-0000-0000-000000000002' and judgment_version = 9), 1,
  '대조군: 같은 행이 자기 사건의 카드 버전을 가리키면 들어간다');
insert into public.judgment_questions (id, run_id, case_id, field_key, text, options)
values ('30000000-0000-0000-0000-000000000002', '20000000-0000-0000-0000-000000000003',
        '10000000-0000-0000-0000-000000000001', 'signal_B', 'B 차량의 신호는?', array['green', 'yellow', 'red']);
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_answers (question_id, case_id, expected_card_version, value, answered_by)
  values ('30000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 9, 'red',
          '00000000-0000-0000-0000-00000000000a')
$sql$, '23503', '응답이 본 카드 버전은 실제 카드 버전이다');
select test_helpers.expect_error(null, null, $sql$
  insert into public.judgment_answers (question_id, case_id, expected_card_version, value, resulting_card_version, answered_by)
  values ('30000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 3, 'red', 9,
          '00000000-0000-0000-0000-00000000000a')
$sql$, '23503', '응답을 반영한 카드 버전은 실제 카드 버전이다');
-- ReviewReport 모양의 보고서. 제약이 읽는 versions·approval_status만 채운다
create function test_helpers.report(case_id uuid, report integer, judgment integer, card integer) returns json
language sql immutable as $$
  select json_build_object('versions', json_build_object('case_id', case_id, 'report_version', report,
    'judgment_version', judgment, 'fact_card_version', card), 'approval_status', 'draft')
$$;
select test_helpers.expect_error(null, null, $sql$
  insert into public.review_reports (case_id, report_version, judgment_version, fact_card_version, report, created_by)
  values ('10000000-0000-0000-0000-000000000001', 1, 1, 9,
    test_helpers.report('10000000-0000-0000-0000-000000000001', 1, 1, 9), '00000000-0000-0000-0000-00000000000a')
$sql$, '23503', '보고서의 카드 버전은 실제 카드 버전이다');
insert into public.review_reports (case_id, report_version, judgment_version, fact_card_version, report, created_by)
values ('10000000-0000-0000-0000-000000000001', 1, 1, 1,
  test_helpers.report('10000000-0000-0000-0000-000000000001', 1, 1, 1), '00000000-0000-0000-0000-00000000000a');
select test_helpers.assert_equal((select count(*) from public.review_reports
  where case_id = '10000000-0000-0000-0000-000000000001'), 1, '대조군: 실제 카드 버전을 가리키는 보고서는 들어간다');
select test_helpers.expect_error(null, null, $sql$
  delete from public.fact_cards where case_id = '10000000-0000-0000-0000-000000000001' and version = 2
$sql$, '23503', '판단·응답이 가리킨 카드 버전은 지울 수 없다');

select 'MIGRATION_BEHAVIOR_OK' as result;
