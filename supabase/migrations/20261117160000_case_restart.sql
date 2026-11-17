-- B(통합): 사건을 접수 단계부터 다시 한 시각(cases.restarted_at).
--
-- 담당자가 접수 화면의 「영상 다시 올리기」로 사건을 접수 단계로 되돌리면
-- (POST /cases/{id}/restart, stage intake — app/api/cases.py restart_case) BE가 이 시각을 적는다.
-- 산출물은 지우지 않는다. 그 전에 만든 사실 카드(fact_cards)·영상 분석 작업(analysis_tasks)은 이력으로
-- 남고, 조회는 이 시각 뒤에 만든 것만 지금 것으로 본다(app/workflow/runs.py RESTARTED_AT_SQL).
-- 판단 실행은 같은 때 superseded_at을 찍어 지금 것에서 뺀다(새 칼럼 없음).
-- 판단 단계로 되돌리기(stage judgment)는 이 칼럼을 쓰지 않는다 — 원본 카드를 새 버전으로 다시 만든다.
-- 되돌린 적이 없는 사건은 null이라 기존 행의 동작은 그대로다.
-- BE 조회는 이 칼럼이 없어도 깨지지 않게 읽으므로(행을 jsonb로 바꿔 읽는다) 코드보다 늦게 적용해도
-- 된다. 적용 전에는 접수 단계로 되돌리기만 503 restart_db_not_ready다.

alter table public.cases
  add column restarted_at timestamptz;

comment on column public.cases.restarted_at is
  '접수 단계로 되돌린 시각. 그 전의 사실 카드·영상 분석은 이력이고 지금 단계 계산에서 빠진다. 되돌린 적 없으면 null';
