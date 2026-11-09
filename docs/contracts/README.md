# 공통 계약 초안

원천: `app/schemas/`의 TypedDict. 아직 런타임 검증·Pydantic·OpenAPI 계약은 아니다.
`make contracts`로 `examples.json`을 생성한다. 합성 견본은 필드 모양을 보여 주는 양식이며 시연/평가 정답이 아니다.
FE 전달본의 `src/lib/api/types.ts`는 이 타입의 수동 대응 초안이다.

| 계약 | 생산/소유 | 소비 |
| --- | --- | --- |
| FactCard·FactPatch | 정지원 | 구준휘 판단·옥석현 보고서·정지원 FE |
| SearchChunk·EmbeddedChunk·SearchRequest·EvidenceHit | 김지은, 옥석현 공동 검토 | 구준휘 판단/QA·옥석현 산정/보고서·김지은 FE |
| JudgmentState·JudgmentResult·QuestionAnswer | 구준휘 | 정지원 카드 재확인·옥석현 보고서·구준휘 FE |
| CalculationResult·RecoveryAmounts·ReviewReport | 옥석현 | 구준휘 가정 실행·옥석현 FE |
| TaskView·VersionRef·ApiError | 구준휘 통합, 전원 검토 | 전원 |

`model_confidence=null`은 수동 입력에서 해당 없음을 뜻한다. 미확정 비율/금액도 `null`로 표시하고 0으로 채우지 않는다.
카드·판단·보고서 버전, 도표/약관 적용판과 규칙 버전은 별도로 추적한다.
약관은 계약 적용판·담보를 별도 필터로 받는다. 사고 일자만으로 약관을 고르지 않는다.
숫자·상태·필수 필드·권한·멱등성은 향후 런타임 검증과 DB 제약으로 강제해야 한다.

계약 변경은 BE 타입 → 견본 재생성 → FE 타입/견본/소비 화면 → 양쪽 PR 연결 순으로 반영한다.
OpenAPI 확정 시 타입 생성 도구·재생성 명령을 추가한다.

## HTTP 경로

`app/api/routes.py`가 FE `src/lib/api/client.ts`의 `ApiClient` 메서드와 1:1로 대응한다.
모든 경로는 `Authorization: Bearer <Supabase access token>`을 요구하고, 오류는 `{"error": ApiError}`로 돌려준다.

| 메서드 | 경로 | 담당 | 비고 |
| --- | --- | --- | --- |
| POST | `/cases` | A | 201 |
| POST | `/cases/{id}/uploads` | A | 서명 URL |
| POST | `/cases/{id}/analysis` | A | `{storage_path}`, `Idempotency-Key` 필수, 202 |
| GET | `/cases/{id}` | A | 상태 복원 |
| GET·PATCH | `/cases/{id}/facts` | A | PATCH 본문은 `FactPatch` |
| POST | `/cases/{id}/facts/confirm` | A | `{expected_version}` |
| POST | `/cases/{id}/judgments` | B | `{card_version}`, `Idempotency-Key` 필수, 202. 버전 불일치 409, 미확정 422 |
| POST | `/cases/{id}/answers` | B | 본문은 `QuestionAnswer` |
| POST | `/cases/{id}/qa` | B | `{text}`, 1차 확장 |
| GET | `/cases/{id}/reports/{version}` | D | |
| POST | `/cases/{id}/reports/{version}/approve` | D | |
| POST | `/cases/{id}/reports/{version}/calculation` | D | 1차 확장 |
| POST | `/cases/{id}/reports/{version}/claim-export` | D | 1차 확장 |

| 상태 | `code` | 뜻 |
| --- | --- | --- |
| 401 | `unauthorized` | 토큰 없음·만료·서명/발급자/audience 불일치 |
| 403 | `forbidden` | 담당 사건이 아님(서비스 함수가 던짐) |
| 409 | `version_conflict` | 기대 버전과 저장 버전이 다름 |
| 422 | `invalid_request`·업무 코드 | 요청 모양 오류, 또는 `card_not_confirmed` 같은 처리 불가 상태 |
| 501 | `not_implemented` | 서비스 본문 미구현 |
| 503 | `auth_not_configured`·`auth_unavailable` | 서버 인증 설정 없음·JWKS 조회 실패 |
| 500 | `internal_error` | 내부 오류. 메시지에 세부 내용을 싣지 않는다 |

담당 서비스는 `app/api/errors.py`의 `forbidden()`·`not_found()`·`version_conflict()`·`unprocessable()`을 던진다.

MVP 도표 범위(사고 유형·카드 칸·수정요소·보류 사유)는 [mvp-chart-scope.md](mvp-chart-scope.md)를 따른다.
