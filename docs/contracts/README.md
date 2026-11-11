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
| TaskView·VersionRef·ApiError·UploadTicket | 구준휘 통합, 전원 검토 | 전원 |

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
| POST | `/cases/{id}/uploads` | A | `{kind, content_type}` → `UploadTicket` `{storage_path, upload_url, expires_in}`. kind가 `estimate`·`video`가 아니면 422 `invalid_request`, 형식이 kind와 안 맞으면 422 `invalid_upload_type`. 아래 「파일 업로드」 |
| POST | `/cases/{id}/analysis` | A | `{storage_path}`, `Idempotency-Key` 필수, 202 |
| GET | `/cases/{id}` | A | 상태 복원 |
| GET·PATCH | `/cases/{id}/fact-card` | A | PATCH 본문은 `FactPatch` |
| POST | `/cases/{id}/fact-card/confirm` | A | `{expected_version}` |
| POST | `/cases/{id}/judgments` | B | `{card_version}`, `Idempotency-Key` 필수, 202. 버전 불일치 409, 미확정 422 |
| GET | `/cases/{id}/judgment` | B | 최신 판단 실행 `JudgmentRunView`, 판단 전이면 `null`. 새로고침·재접속 복원 |
| POST | `/cases/{id}/questions/{question_id}/answer` | B | 본문은 `QuestionAnswer`. 본문 `question_id`가 경로와 다르면 422 `question_mismatch` |
| POST | `/cases/{id}/qa` | B | `{text}`(1~2000자) → `QaAnswer`. 판정이 난 판단이 없으면 409 `judgment_required`. 1차 확장 |
| GET | `/cases/{id}/reports/{version}` | D | |
| POST | `/cases/{id}/reports/{version}/confirm` | D | 본문 `{counterparty?}`, 응답 `ConfirmResult`. 최신 아님 409, 추가 확인·보류 422 `confirm_blocked` |
| GET | `/cases/{id}/reports/{version}/claim` | D | 응답 `ClaimResult`. 청구서가 없으면 404 |
| POST | `/cases/{id}/documents` | D | `{documents, case_facts?}` → 재산정한 `ReviewReport` 또는 null. 1차 확장 |
| POST | `/cases/{id}/estimates` | D | `{storage_path, party_id}` → 재산정한 `ReviewReport` 또는 null. `storage_path`는 `/uploads`(kind=estimate)가 준 버킷 포함 경로 `case-files/{id}/estimates/<파일>`(버킷은 `app/storage.py` `STORAGE_BUCKET`). 권한 → 경로 확인 → 서버 키로 Storage에서 내려받기(10MB 이하 PDF). 1차 확장 |
| PATCH | `/cases/{id}/agreed-fault` | D | `{agreed_counterparty_fault?, fault_change_reason, clear?}` → 새 `ReviewReport`. 1차 확장 |

| 상태 | `code` | 뜻 |
| --- | --- | --- |
| 401 | `unauthorized` | 토큰 없음·만료·서명/발급자/audience 불일치 |
| 403 | `forbidden` | 담당 사건이 아님(서비스 함수가 던짐) |
| 404 | `not_found` | 사건·질문이 없음 |
| 404 | `estimate_file_not_found` | 견적서 경로에 Storage 파일이 없음 |
| 409 | `version_conflict` | 기대 버전과 저장 버전이 다름 |
| 409 | `judgment_in_progress`·`judgment_conflict`·`request_in_progress`·`already_answered` | 같은 사건 판단 실행 중·요청 겹침·같은 키 처리 중·이미 답한 질문. 재시도 여부는 `retryable` |
| 422 | `invalid_request`·업무 코드 | 요청 모양 오류, 또는 `card_not_confirmed`·`question_mismatch`·`question_closed`·`invalid_answer`·`idempotency_key_reused`·`confirm_blocked`·`invalid_case_facts`·`invalid_documents`·`invalid_agreed_fault` 같은 처리 불가 상태 |
| 422 | `invalid_upload_type` | 업로드 형식이 kind의 허용 목록에 없음(견적서 `application/pdf`, 영상 mp4·mov·avi·webm·mkv) |
| 422 | `invalid_storage_path`·`estimate_too_large`·`estimate_not_pdf` | 견적서 경로가 그 사건의 `estimates/` 폴더가 아님(다른 사건·`..`·비ASCII 이름 등)·10MB 초과·PDF가 아니거나 읽을 수 없음 |
| 501 | `not_implemented` | 서비스 본문 미구현 |
| 502 | `qa_failed` | 질의응답 모델 호출·응답 오류. 다시 질문할 수 있다 |
| 503 | `auth_not_configured`·`auth_unavailable`·`db_not_configured`·`db_unavailable`·`reports_db_not_ready`·`storage_not_configured`·`storage_unavailable` | 서버 인증 설정 없음·JWKS 조회 실패·서버 DB 설정 없음·DB 연결 실패·보고서 테이블 미적용·서버 Storage 키(`SUPABASE_URL`·`SUPABASE_SECRET_KEY`) 없음·Storage 연결 실패나 오류 |
| 504 | `qa_timeout` | 질의응답 LLM 호출 시간 상한 초과(질문당 합계 120초, 또는 호출 1회가 재시도 뒤에도 45초 초과). 다시 질문할 수 있다 |
| 500 | `internal_error` | 내부 오류. 메시지에 세부 내용을 싣지 않는다 |

담당 서비스는 `app/api/errors.py`의 `forbidden()`·`not_found()`·`version_conflict()`·`unprocessable()`을 던진다.

### 파일 업로드

11/11 정지원·옥석현·구준휘가 합의했다. 견적서·영상 파일은 API 본문으로 받지 않는다(Vercel 요청 본문 한도 4.5MB).

1. `POST /cases/{id}/uploads` `{kind, content_type}` → `UploadTicket`
2. FE가 `upload_url`에 키 없이 파일 본문과 `Content-Type`만 넣어 `PUT` → 200 `{"Key": "<storage_path>"}`
3. 견적서는 `POST /cases/{id}/estimates` `{storage_path, party_id}`, 영상은 `POST /cases/{id}/analysis` `{storage_path}`

- 버킷 `case-files`는 비공개이고 `application/pdf`·`video/*`만 받는다.
- 무료 플랜이라 파일 하나 50MB가 고정 한도다. 영상은 사고 구간만 잘라 올리는 것을 권한다(정지원: 지금 영상 중앙값 6.3MB, 최대 24MB).
- 경로는 `case-files/{case_id}/estimates/{uuid}.pdf`·`case-files/{case_id}/videos/{uuid}.{확장자}`이고, 이름은 BE가 만든다. Supabase는 한글 객체 이름을 거절한다(400 `InvalidKey`).
- 서명 업로드 URL은 7200초 유효하고 한 번만 쓸 수 있다. Storage는 아래 두 경우 모두 HTTP 400과 본문 `statusCode`로 응답한다.
  - 같은 URL로 다시 올리면 `statusCode` 409(`Duplicate`)
  - 허용하지 않은 형식이면 `statusCode` 415(`invalid_mime_type`)
- 서버 키(`SUPABASE_SECRET_KEY`)는 RLS를 건너뛴다. 그래서 발급과 내려받기 모두 사건 권한 확인과 경로 확인을 먼저 한다. Storage 호출(서명 발급·내려받기)은 공용 `app/storage.py`에 둔다.
- 영상 분석(Modal)에는 서버 키를 두지 않는다.
  - 분석 접수 때 BE가 짧게 유효한 서명 내려받기 URL을 만들어 Modal에 넘긴다.
  - Modal 쪽(구준휘, 인프라)이 그 URL로 영상을 받아 로컬 경로로 `workers/vision/pipeline.py` `analyze_video`를 부른다. 그래서 `analyze_video`는 Storage를 직접 읽지 않고 로컬 영상 경로를 받는다(정지원).
  - 호출 모양은 구준휘가 정리한다.

## 구분 질문과 재개

판단 그래프(`app/workflow/graph.py`)는 도표를 가르는 칸이 `human_confirmed`가 아니면 `ConfirmationQuestion`을 내고 멈춘다.
`question_id`는 실행 ID와 칸 이름으로 정해지는 UUID라 같은 실행에서 다시 물어도 같다(`judgment_questions.id`).

질문의 `field_key`·`options`는 카드 형식을 따른다. 값 응답이 그 칸 이름·값 그대로 카드 수정으로 가기 때문이다.
- 카드 v1(한국어 값, 정지원 11/11 안)이면 v1 칸 이름·값으로 묻는다. 예: `accident_object` `["차대차", "차대이륜차", "기타"]`.
  `불명`은 선택지에 넣지 않고 '모른다' 응답으로 받는다.
- 이전 형식(영어 값) 카드면 `chart_scope.FIELD_OPTIONS` 그대로다.
- `question_id`는 형식과 상관없이 도표 선택 칸 이름(`accident_type`)으로 만든다.

카드에서 담당자가 `불명`으로 확인한 칸은 다시 묻지 않고 처음부터 '모른다' 응답처럼 다룬다. 사고 유형·장소가 `불명`이면 검색 전에 보류한다.
카드 값 → 도표 선택 값 대응은 `app/workflow/card_fields.py`, 규칙은 [mvp-chart-scope.md](mvp-chart-scope.md) 「카드 v1 값 읽기」다.

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
`decision` 적용·미적용·불명과 `reason`·`evidence_fields`, 가감치가 있는 요소만), `alternatives`(고르지 않은 도표와 이유. 카드와 맞지 않는 도표는 검색 점수 순 앞 5개, 검수 전 도표는 앞 3개까지)다.
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

## 근거 질의응답(what-if)

`POST /cases/{id}/qa`는 최신 판단 실행이 판정을 냈을 때만 받는다(확인 대기·실패·판단 전이면 409 `judgment_required`).
질문마다 모델이 도구를 최대 5번 고른다(`app/agents/qa.py`, 아키텍처 v7 「질의응답 에이전트」).

| 도구 | 하는 일 |
| --- | --- |
| `get_judgment_log` | 공식 판정(`judgment_runs.result`)과 그 판단에 쓴 카드(체크포인트)의 확인한 사실 |
| `search_dispute_cases` | ② 분쟁심의 결정례 검색(김지은 검색툴, 사건의 사고 일자) |
| `search_policy_terms` | ③ 약관·보상 지침 검색 |
| `simulate` | 카드 칸 1~4개를 가정 값(`chart_scope.FIELD_OPTIONS`)으로 바꿔 같은 판단 그래프를 메모리에서 다시 실행. 질문당 2번까지 |

- 응답 `QaAnswer`는 `answer`·`citations`(답에 쓴 `[E1]` 표시마다 청크·코퍼스·적용판·페이지·발췌·`source`)·`hypotheticals`(`QaHypothesis` — 가정한 칸,
  `status` `completed`·`waiting_for_confirmation`(`open_fields`)·`failed`(`error`), 가정 판정·도표·과실)·`tool_calls`·`fact_change_hint`다. 견본은 `qa_answer`다.
- 인용 `source`는 그 근거를 돌려준 도구다 — `official`(공식 판단 로그)·`search`(결정례·약관 검색)·`hypothesis`(가정 실행이 고른 도표의 근거).
  공식 판단 로그가 돌려준 근거이기도 하면 `official`이다. `hypothesis` 근거는 공식 판정의 근거가 아니다.
- 가정 실행은 실행 행·체크포인트·보고서를 만들지 않고 공식 판정을 바꾸지 않는다. 가정 실행을 했으면 `fact_change_hint`가
  공식 반영 경로(사실 카드 수정·확정 → 판단 다시 요청)를 알린다.
- 수치는 판단 로그(공식)와 가정 실행 결과에서만 인용한다. 도구가 돌려준 적 없는 `[E…]` 표시는 답에서 지운다.
- 검색 색인이 없으면 그 도구만 실패로 돌려주고 답은 낸다. 모델 키가 없으면 503 `dependency_not_ready`, 모델 호출 오류는 502 `qa_failed`(다시 질문 가능)다.
- 시간 제한은 LLM 호출에만 둔다(`app/llm.py`, lumi·cure-agent-be 방식). 호출 1회는 45초에서 끊고 SDK 재시도는 끈다. 타임아웃·네트워크·5xx만 1회 다시 시도하고
  4xx는 다시 보내지 않는다(반박 검토도 같은 정책). 질문 하나의 LLM 호출 시간 합계는 120초이고 검색·가정 실행 같은 도구 시간은 세지 않는다.
  넘으면 504 `qa_timeout`(다시 질문 가능)이다.
- 질문·답은 저장하지 않는다(대화 기록 없음, 질문마다 새로 답한다).
