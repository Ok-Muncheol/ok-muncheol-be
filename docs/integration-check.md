# 통합 검수·재현 경로

2026-11-18 기준. 기능 통합 뒤 운영(Vercel·공용 DB)에서 확인한 것을 적는다. 경로마다 다시 재현하는 방법과 그 경로를 지키는 테스트도 함께 적는다.
WBS 6.5(통합 검수 통과, 재현 경로 문서화)의 산출물이다. 경로를 더하거나 동작을 바꾸면 이 문서도 같이 고친다.

## 1. 운영에서 확인한 흐름

| 날짜 | 확인한 것 | 근거 |
| --- | --- | --- |
| 11/13 | 영상 분석: 분석 접수 → Modal → 콜백 → 사실 카드·미리보기 영상 | #164·#167·#170 |
| 11/13 | 시연 사건으로 카드 확정 → 구분 질문 → 검증 3겹 → 검토 보고서 → 확인 완료·구상 청구서 | FE #88·#92·#96·#100 |
| 11/16 | 교차로 밖 도표로 판단 범위를 넓히고, 5차·6차 검수 자료(도표 97/108)로 운영 색인을 바꿈 | #239·#247·#253·#261·#265 |
| 11/17 | 시연 사건 15건을 다시 판단해 모두 도표를 고른 상태로 맞춤 | - |
| 11/18 | 운영 DB의 판단 실행·검증 기록과 재실행 상한 제약 | 4절 |

## 2. 재현 경로

| 경로 | 재현 방법 | 지키는 테스트 |
| --- | --- | --- |
| 확인·재개 | 판단 화면에서 카드를 확정하고 판단한다. 구분 질문이 나오면 답하고, 같은 실행이 이어서 판정한다 | `tests/workflow/test_graph.py`, `tests/api/test_judgment_service.py` |
| 보류·추가 확인 필요 | 범위 밖 사고, 고를 도표 없음, 「모르겠어요」 답을 넣는다. 보류 사유와 고쳐서 고를 수 있는 도표·값이 나온다(#267·#281·#299) | `tests/workflow/test_graph.py`, `tests/workflow/test_graph_non_intersection.py` |
| 반박 재실행 상한 | 도표 오선택 반박이 서면 차순위 도표로 한 번만 다시 돈다. 두 번째 오선택은 세 번째 실행 없이 `needs_confirmation`이다. DB도 `rebuttal_retry_count`를 0~1로 막는다 | `tests/workflow/test_graph.py`(`test_wrong_chart_reruns_once_with_preferred_alternative`, `test_second_wrong_chart_needs_confirmation_without_third_run`), `tests/migrations/behavior.sql` |
| 수치·근거 일치 | 과실 산식(0~100 정수, A+B=100, 가감 부호)과 도표·수정요소·인용 ID 실재를 검증 3겹이 막는다 | `tests/validation/test_numerical.py`, `tests/validation/test_evidence.py`, `tests/validation/test_rebuttal.py` |
| 질의응답 불변 | 근거 질의응답·가정 실행은 공식 판단을 읽기만 한다. `app/agents/qa.py`와 `POST /cases/{id}/qa`에는 DB 쓰기가 없고, 가정 결과는 공식 판정과 따로 보인다 | `tests/agents/test_qa.py`, `tests/api/test_judgment_service.py` |
| 실패 | 영상 분석이 실패하면 접수 화면이 사유와 「영상 다시 올리기」를 보인다. 판단 단계가 실패하면 진행 표시에 실패한 단계가 나온다 | `tests/api/test_analysis.py`, `tests/test_modal_worker.py` |
| 버전 충돌 | 다른 사람이 카드를 먼저 고치면 `409 version_conflict`이고, 화면이 최신 카드를 다시 보라고 알린다. 질문 답도 `expected_card_version`으로 거절한다 | `tests/api/test_fact_cards.py`, `tests/api/test_judgment_service.py` |
| 재접속·새로고침 | `GET /cases/{id}`(단계·남은 질문 수)와 카드·판단 조회로 화면을 복원한다. Realtime 변경 알림은 쓰지 않는다 | `tests/api/test_cases.py` |
| 권한 | 다른 담당자의 사건은 읽지 못한다. 대물·대인 담당자는 같은 사건을 보고, 관리자는 사건 상세를 열지 못한다 | `tests/api/test_case_assignees_db.py`, `tests/migrations/behavior.sql` |
| 되돌리기 | 「영상 다시 올리기」·「초기화」는 `POST /cases/{id}/restart`로 접수·판단 단계 처음부터 다시 시작한다. 이력은 남고 서류·면책 체크·협의 과실은 지운다 | `tests/api/test_case_restart_db.py` |
| 영상 없이 진행 | `POST /cases/{id}/fact-card`로 빈 카드를 만들고, 비운 칸은 구분 질문으로 묻는다 | `tests/api/test_fact_cards.py` |

DB가 필요한 테스트는 CI가 Postgres를 띄워 돌린다. 로컬에 Postgres가 없으면 건너뛴다.

## 3. 시연 절차

1. 로그인 화면의 시연 계정 단추(대물·대인·관리자)로 들어간다. 계정 정보는 저장소에 두지 않는다. 단추는 FE 서버 환경 변수 `DEMO_LOGIN_*`가 있을 때만 보인다.
2. 관리자 계정의 사건 관제에서 새 접수가 들어와 배정 에이전트가 담당자를 고르는 과정(배정 방식·까닭)을 보인다. 시연 접수는 `scripts/demo_intake.py`로 넣는다. 10초 뒤 관제 화면에 나타나고 1분 뒤 지워진다.
3. 이관은 하지 않는다. 이미 사건을 맡고 있는 대인 시연 계정으로 넘어가 영상 → 사실 카드 → 판단 → 검토 보고서 → 확인 완료를 보인다.

시연 전에 챙길 것:
- 영상 분석을 보일 거면 Modal을 미리 켜 둔다. 꺼진 상태의 첫 분석은 52~77초, 켜진 상태는 약 14초다.
- 관제 화면의 가상 접수는 11/20 새벽까지 미리 만들어 두었다. 그 뒤에 시연하면 `scripts/seed_dispatch.py --apply --reset`으로 다시 채운다(약 40분).
- 시연 계정 사건은 진행하면 줄어든다. 화면의 「영상 다시 올리기」·「초기화」로 접수·판단 단계로 되돌려 다시 쓴다.

## 4. 운영 DB 확인(11/18, 읽기 전용)

| 확인 | 결과 |
| --- | --- |
| 재실행 상한 제약 `judgment_runs_retry_cap` | `CHECK (rebuttal_retry_count >= 0 AND rebuttal_retry_count <= 1)` 적용됨 |
| 판단 실행 111건 | 재실행 0건. `deferred` 47·`draft_ready` 37·`needs_confirmation` 17, 질문 대기 10 |
| 검증 기록 | 근거 38건·수치 37건 통과, 반박 38건 중 1건 실패. 실패 1건은 재실행 없이 `needs_confirmation`(11/17 시연 사건, 사실 누락 주장) |

반박 재실행은 운영에서 일어난 적이 없다. 상한은 DB 제약과 위 테스트로 확인했다. 다시 볼 때는 읽기 전용 트랜잭션에서 아래를 돌린다.

```sql
select rebuttal_retry_count, judgment_status, status, count(*)
from public.judgment_runs group by 1, 2, 3 order by 1, 2, 3;

select attempt, layer, passed, count(*)
from public.validation_checks group by 1, 2, 3 order by 2, 1, 3;
```

## 5. 알려진 한계와 범위 제외(11/18 결정)

- Realtime 변경 알림은 쓰지 않는다. 조회·새로고침 복원과 관제 화면의 3초 갱신으로 대신한다.
- 검색 색인은 공용 DB 표 대신 비공개 Storage의 파일 색인으로 운영한다(#183). 공용 DB 색인 표는 만들지 않는다.
- 자차·구상 산식과 서식의 실무 검수는 프로젝트 안에서 받을 수 없어 범위에서 뺐다. 금액·청구서는 '약관 검수 전 초안'으로 표시한다.
- 원문 PDF의 OCR은 하지 않는다. 텍스트가 없는 쪽은 검수 대상으로 남긴다.
- 도표 검수는 108개 중 97개(6차, #265)다. 검수하지 않은 도표로는 판정하지 않고 보류 사유로 알린다.
