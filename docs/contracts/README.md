# 공통 계약 초안

원천: `app/schemas/`의 TypedDict. 아직 런타임 검증·Pydantic·OpenAPI 계약은 아니다.
`make contracts`로 `examples.json`을 생성한다. 합성 견본은 필드 모양을 보여 주는 양식이며 시연/평가 정답이 아니다.
FE 전달본의 `src/lib/api/types.ts`는 이 타입의 수동 대응 초안이다.

| 계약 | 생산/소유 | 소비 |
| --- | --- | --- |
| FactCard·FactPatch | A | B 판단·D 보고서·A FE |
| SearchChunk·EmbeddedChunk·SearchRequest·EvidenceHit | C, D 공동 검토 | B 판단/QA·D 산정/보고서·C FE |
| JudgmentState·JudgmentResult·QuestionAnswer | B | A 카드 재확인·D 보고서·B FE |
| CalculationResult·RecoveryAmounts·ReviewReport | D | B 가정 실행·D FE |
| TaskView·VersionRef·ApiError | D 통합, 전원 검토 | 전원 |

`model_confidence=null`은 수동 입력에서 해당 없음을 뜻한다. 미확정 비율/금액도 `null`로 표시하고 0으로 채우지 않는다.
카드·판단·보고서 버전, 도표/약관 적용판과 규칙 버전은 별도로 추적한다.
약관은 계약 적용판·담보를 별도 필터로 받는다. 사고 일자만으로 약관을 고르지 않는다.
숫자·상태·필수 필드·권한·멱등성은 향후 런타임 검증과 DB 제약으로 강제해야 한다.

계약 변경은 BE 타입 → 견본 재생성 → FE 타입/견본/소비 화면 → 양쪽 PR 연결 순으로 반영한다.
OpenAPI 확정 시 타입 생성 도구·재생성 명령을 추가한다.
