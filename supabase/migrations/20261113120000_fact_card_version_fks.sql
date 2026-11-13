-- B(구준휘): 판단·구분 질문 응답·검토 보고서가 가리키는 사실 카드 버전을 실제 카드 버전 행(fact_cards)에 묶는다.
--
-- 지금까지 이 테이블들은 카드 버전 숫자만 들고 있었다. 카드 버전 테이블(20261111150000_fact_cards.sql)이 생겨
-- (case_id, 버전) 복합 외래키를 건다. case_id가 키에 들어 있어 다른 사건의 카드 버전은 가리킬 수 없다.
-- 카드 버전 행은 지우지 않는다(수정은 새 행). 그래서 삭제 동작은 기본(no action)이고, 판단·보고서가 쓴 카드 버전은 지울 수 없다.
--
-- 앱은 이미 이 순서를 지킨다.
--   판단 접수(POST /cases/{id}/judgments)는 fact_cards의 최신 카드를 다시 읽어 그 버전으로 실행을 만든다(app/api/routes.py).
--   응답 버전(expected_card_version)은 질문한 실행의 카드 버전과 같을 때만 저장한다(app/api/judgments.py).
--   응답을 반영한 카드(resulting_card_version)는 새 카드 버전으로 판단을 다시 접수할 때 적는다(app/workflow/runs.py open_run).
--   보고서는 그 판단에 쓴 카드로 만든다(app/api/reports.py submit_judgment).
-- review_reports.fact_card(카드 사본 json)는 재산정이 읽으므로 그대로 둔다. 없애려면 D 코드를 먼저 바꾸고 따로 지운다.

alter table public.judgment_runs
  add constraint judgment_runs_card_fk
  foreign key (case_id, fact_card_version) references public.fact_cards (case_id, version);

alter table public.judgment_answers
  add constraint judgment_answers_expected_card_fk
  foreign key (case_id, expected_card_version) references public.fact_cards (case_id, version),
  -- 반영 전(null)이면 검사하지 않는다
  add constraint judgment_answers_resulting_card_fk
  foreign key (case_id, resulting_card_version) references public.fact_cards (case_id, version);

alter table public.review_reports
  add constraint review_reports_card_fk
  foreign key (case_id, fact_card_version) references public.fact_cards (case_id, version);
