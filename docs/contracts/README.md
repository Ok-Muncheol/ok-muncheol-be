# 공통 계약 초안

원천: `app/schemas/`의 TypedDict. 아직 런타임 검증·Pydantic·OpenAPI 계약은 아니다.
`make contracts`로 `examples.json`을 생성한다. 합성 견본은 필드 모양을 보여 주는 양식이며 시연/평가 정답이 아니다.
FE 전달본의 `src/lib/api/types.ts`는 이 타입의 수동 대응 초안이다.

| 계약 | 생산/소유 | 소비 |
| --- | --- | --- |
| FactCard·FactPatch | 정지원 | 구준휘 판단·옥석현 보고서·정지원 FE |
| SearchChunk·EmbeddedChunk·SearchRequest·EvidenceHit | 김지은, 옥석현 공동 검토 | 구준휘 판단/QA·옥석현 산정/보고서·김지은 FE |
| JudgmentState·JudgmentResult·QuestionAnswer·JudgmentRunView | 구준휘 | 정지원 카드 재확인·옥석현 보고서·구준휘 FE |
| CalculationResult·RecoveryAmounts·ReviewReport | 옥석현 | 구준휘 가정 실행·옥석현 FE |
| TaskView·VersionRef·ApiError | 구준휘 통합, 전원 검토 | 전원 |

`model_confidence=null`은 수동 입력에서 해당 없음을 뜻한다. 미확정 비율/금액도 `null`로 표시하고 0으로 채우지 않는다.
카드·판단·보고서 버전, 도표/약관 적용판과 규칙 버전은 별도로 추적한다.
약관은 계약 적용판·담보를 별도 필터로 받는다. 사고 일자만으로 약관을 고르지 않는다.
숫자·상태·필수 필드·권한·멱등성은 런타임 검증과 DB 제약으로 강제한다. 판단 테이블의 제약(작업/판정 상태 값·A+B=100·
반박 재실행 상한·결과 버전 일치·멱등성 키)은 `supabase/migrations/`에 있고, 상태 값은 `tests/migrations/`가 이 타입과 대조한다.

계약 변경은 BE 타입 → 견본 재생성 → FE 타입/견본/소비 화면 → 양쪽 PR 연결 순으로 반영한다.
OpenAPI 확정 시 타입 생성 도구·재생성 명령을 추가한다.

## HTTP 경로

`app/api/routes.py`가 FE `src/lib/api/client.ts`의 `ApiClient` 메서드와 1:1로 대응한다.
경로는 README 「API 계약 제안」 표가 기준이고, `tests/api/test_app.py`가 그 표와 아래 표를 서버 OpenAPI와 대조한다.
모든 경로는 `Authorization: Bearer <Supabase access token>`을 요구하고, 오류는 `{"error": ApiError}`로 돌려준다.

| 메서드 | 경로 | 담당 | 비고 |
| --- | --- | --- | --- |
| POST | `/cases` | A | 201 |
| POST | `/cases/{id}/uploads` | A | 서명 URL |
| POST | `/cases/{id}/analysis` | A | `{storage_path}`, `Idempotency-Key` 필수, 202 |
| GET | `/cases/{id}` | A | 상태 복원 |
| GET·PATCH | `/cases/{id}/fact-card` | A | PATCH 본문은 `FactPatch` |
| POST | `/cases/{id}/fact-card/confirm` | A | `{expected_version}` |
| POST | `/cases/{id}/judgments` | B | `{card_version}`, `Idempotency-Key` 필수, 202. 버전 불일치 409, 미확정 422 |
| GET | `/cases/{id}/judgment` | B | 최신 판단 실행 `JudgmentRunView`, 판단 전이면 `null`. 새로고침·재접속 복원 |
| POST | `/cases/{id}/questions/{question_id}/answer` | B | 본문은 `QuestionAnswer`. 본문 `question_id`가 경로와 다르면 422 `question_mismatch` |
| POST | `/cases/{id}/qa` | B | `{text}`, 1차 확장 |
| GET | `/cases/{id}/reports/{version}` | D | |
| POST | `/cases/{id}/reports/{version}/confirm` | D | 본문 `{counterparty?}`, 응답 `ConfirmResult`. 최신 아님 409, 추가 확인·보류 422 `confirm_blocked` |
| GET | `/cases/{id}/reports/{version}/claim` | D | 응답 `ClaimResult`. 청구서가 없으면 404 |
| POST | `/cases/{id}/documents` | D | `{documents, case_facts?}` → 재산정한 `ReviewReport` 또는 null. 1차 확장 |
| POST | `/cases/{id}/estimates` | D | `{storage_path, party_id}`. 경로 본문의 Storage 내려받기도 D(11/10 B에서 이관), 연결 전에는 501. 1차 확장 |
| PATCH | `/cases/{id}/agreed-fault` | D | `{agreed_counterparty_fault?, fault_change_reason, clear?}` → 새 `ReviewReport`. 1차 확장 |

| 상태 | `code` | 뜻 |
| --- | --- | --- |
| 401 | `unauthorized` | 토큰 없음·만료·서명/발급자/audience 불일치 |
| 403 | `forbidden` | 담당 사건이 아님(서비스 함수가 던짐) |
| 404 | `not_found` | 사건·질문이 없음 |
| 409 | `version_conflict` | 기대 버전과 저장 버전이 다름 |
| 409 | `judgment_in_progress`·`judgment_conflict`·`request_in_progress`·`already_answered` | 같은 사건 판단 실행 중·요청 겹침·같은 키 처리 중·이미 답한 질문. 재시도 여부는 `retryable` |
| 422 | `invalid_request`·업무 코드 | 요청 모양 오류, 또는 `card_not_confirmed`·`question_mismatch`·`question_closed`·`invalid_answer`·`idempotency_key_reused`·`confirm_blocked`·`invalid_case_facts`·`invalid_documents`·`invalid_agreed_fault` 같은 처리 불가 상태 |
| 501 | `not_implemented` | 서비스 본문 미구현 |
| 503 | `auth_not_configured`·`auth_unavailable`·`db_not_configured`·`db_unavailable`·`reports_db_not_ready` | 서버 인증 설정 없음·JWKS 조회 실패·서버 DB 설정 없음·DB 연결 실패·보고서 테이블 미적용 |
| 500 | `internal_error` | 내부 오류. 메시지에 세부 내용을 싣지 않는다 |

담당 서비스는 `app/api/errors.py`의 `forbidden()`·`not_found()`·`version_conflict()`·`unprocessable()`을 던진다.

## 구분 질문과 재개

판단 그래프(`app/workflow/graph.py`)는 도표를 가르는 칸이 `human_confirmed`가 아니면 `ConfirmationQuestion`을 내고 멈춘다.
`question_id`는 실행 ID와 칸 이름으로 정해지는 UUID라 같은 실행에서 다시 물어도 같다(`judgment_questions.id`).

| 응답 | 처리 |
| --- | --- |
| 값 응답(`unknown=false`) | 정지원의 카드 수정/확정 경로로 새 카드 버전을 만들고 새 판단 실행을 시작한다. 같은 실행을 잇지 않는다 |
| 모른다(`unknown=true`, `value=null`) | 같은 실행을 재개한다. 그 칸으로 갈리는 도표는 후보에서 빼고 그 칸이 `*`(무관)인 도표로 진행한다. 남는 도표가 없으면 `deferred`(확인 불가)다 |

반박 검토는 `wrong_chart`일 때만 차순위 도표로 1회 재실행하고, `missing_fact`·`contradiction`은 바로 `needs_confirmation`이다.
반박 검토 LLM 응답은 JSON 스키마(`holds`·`kind`·`preferred_chart_id`·`reasons`)로 고정하며 모양이 어긋나면 실행 실패로 올린다.

## 판단 접수·질문 응답

판단은 요청 안에서 끝까지 실행하고(`app/api/judgments.py`) 실행·질문·응답·검증 로그를 `judgment_runs` 계열 테이블에,
그래프 체크포인트를 `langgraph` 스키마에 저장한다. FE는 판단 조회(`GET /cases/{id}/judgment`)로 최신 실행·판정·질문·답을 읽어
새로고침·재접속을 복원하고, Realtime은 변경 알림으로만 쓴다. 판정 결과(`JudgmentResult`)는 `judgment_runs.result`에 있다.
보고서 3·4·5번용 필드도 판단 그래프가 채운다. `base_fault_A`(선택한 기본 과실 A), `modifiers`(선택한 도표의 수정요소마다
`decision` 적용·미적용·불명과 `reason`·`evidence_fields`, 가감치가 있는 요소만), `alternatives`(고르지 않은 도표와 이유)다.
MVP 그래프는 수정요소를 자동 적용하지 않으므로 `modifiers`는 모두 불명(담당자 판단)이고 과실은 기본 과실이다.
도표를 고르지 못한 보류면 `null`·빈 목록이다.

| 요청 | 응답 본문 |
| --- | --- |
| 판단 접수(202) | `TaskView` — `task_id`·`case_id`·`stage`·`status`(`completed`·`waiting_for_confirmation`·`failed`)·`input_version`(카드 버전) + `report_version`·`report_error` |
| 질문 응답(200) | 판단 접수 응답 + `pending_question_ids`·`next_action`·`field_values` |
| 판단 조회(200) | `JudgmentRunView` 또는 판단 전이면 `null` — 판단 버전이 가장 큰 실행 하나 |

`JudgmentRunView`는 `judgment_runs` 행(`task_id`·`case_id`·`judgment_version`·`fact_card_version`·`criteria_version`·`rule_version`·
`status`·`stage`·`rebuttal_retry_count`·`result`·`error`·`updated_at`)에 `report_version`(판정이 났으면 그 판단으로 만든 보고서의
최신 버전, 못 만들었으면 `null`)과 `questions`를 더한다. `result`는 `completed`일 때만, `error`는 `failed`일 때만 온다.
확인 대기(`waiting_for_confirmation`) 실행은 `result`가 비어 있으니 질문은 `questions`에서 읽는다. `questions`는 실행의 구분 질문
(`ConfirmationQuestion`)마다 `answer`(`value`·`unknown`·`expected_card_version`·`resulting_card_version`·`answered_at`, 안 답했으면 `null`)를
붙인다. 새 카드 버전의 판단이 확인 대기 실행을 대체하면 그 새 실행이 최신이라 대체된 실행은 오지 않는다. 견본은 `judgment_run_waiting`이다.

`next_action`은 `answer_remaining`(남은 질문에 답한다), `update_fact_card`(값 답변을 카드에 반영·재확정한 뒤 새 카드 버전으로
판단을 다시 요청한다. `field_values`가 칸별 답), `null`(모두 '모름'이라 같은 실행을 재개해 끝냈다) 중 하나다.

- 같은 `Idempotency-Key`·같은 요청이면 저장한 응답을 돌려준다. 같은 입력(카드·인정기준·규칙 버전)은 다시 판단하지 않고 기존 실행을 돌려준다.
- 판정이 나면(세 상태 모두) 옥석현 `submit_judgment`로 보고서를 만든다. `report_version`은 이 판단으로 만든 보고서의 최신 버전이고,
  질문 대기·실행 실패면 `null`이다. 같은 판단 버전의 보고서가 있으면 다시 만들지 않는다.
- 보고서를 못 만들어도 판단은 `completed`로 남고 `report_error`(보고서 쪽 오류 코드 또는 `report_failed`)로 알린다.
  같은 입력으로 판단 접수를 다시 보내면(새 키) 판단은 다시 하지 않고 보고서 만들기만 다시 시도한다.
- 확인 대기 중인 실행은 새 카드 버전의 판단 접수가 대체하고(`superseded_at`), 새 실행이 `resumed_from_run_id`로 잇는다.
- 실행 실패는 `status=failed`와 `judgment_runs.error`로 남는다. `dependency_not_ready`(검색 색인·검토 모델 미연결),
  `judgment_failed`(재시도 가능), `run_interrupted`(10분 넘게 멈춘 실행을 정리)다.

MVP 도표 범위(사고 유형·카드 칸·수정요소·보류 사유)는 [mvp-chart-scope.md](mvp-chart-scope.md)를 따른다.
