# 구현 단계 검증 계획

`make check`는 문법·import·JSON 검사다.
`make test`(pytest)는 `tests/` 전체를 돌린다. API 인증·오류 응답·경로 배선은 `tests/api/`, 검색·코퍼스는 `tests/test_*.py`,
비전 평가는 `tests/evals/`, ship 게이트·폴링 스크립트는 `tests/automation/`에 있다.
마이그레이션 파일 규칙·계약 동기화는 `tests/migrations/`에 있다. DB 동작 검사(마이그레이션, 판단 저장 `tests/api/test_judgment_service.py`)는
`MIGRATION_TEST_DATABASE_URL`이 있을 때 돌고, CI는 Postgres 서비스로 늘 돌린다(`supabase/migrations/README.md` 「로컬 검사」).
판단 그래프·검증 3겹은 `tests/workflow/`·`tests/validation/`에 있다. 근거 질의응답 에이전트(도구 루프·근거 표시·가정 결과 분리)는
`tests/agents/`, 판정·카드를 읽고 가정 실행이 저장하지 않는지는 `tests/api/test_judgment_service.py`가 본다.
다음 계약이 구현될 때 의미 있는 동작 테스트를 이 폴더에 추가하면 CI `check`가 함께 돌린다.

D(옥석현) — 흐름: 판단 → 바로 산정 → 서류 추가 시 자동 재산정 → 담당자 확인 완료 1회 → 구상 청구서, 지급 내역 입력 없음:
- `tests/test_d_reports.py` — 손해액·자차 보험금·보고서 1~9·구상 청구서(사건 1건 = 1행)·버전
- `tests/test_d_edge_cases.py` — 가입금액 초과·자기부담금 ≥ 손해액·견적 혼재·과실 0:100/100:0·원 미만 버림·협의 과실·면책 담당자 입력
- `tests/test_d_flow.py` — 확인 완료 1회·최신 버전만·확인 후 서류 추가 시 새 버전·재확인
- `tests/test_d_assumptions.py` — 계약·자기부담금·면책 기본 가정, 청구서 없음 사유, `case_facts` 검사
- `tests/test_d_estimate_pdf.py` — 서식 견적서 PDF 자동 추출(pdfplumber 없으면 일부 건너뜀)

| 담당 | 우선 검증 |
| --- | --- |
| 정지원 | 수동/영상 카드, 원값 보존, 불명/충돌·당사자 매핑, 확정 전 차단, 버전 충돌·작업 복원·권한 |
| 구준휘 | 확인/재개, 없는 근거·유효판 오류 차단, 반박 재실행 1회, 부호/중복/배타·A+B=100, QA 공식 상태 불변 |
| 김지은 | PDF 표/페이지·구조 청킹·적용판, 임베딩 메타데이터, 코퍼스 혼입·분할 누수, 미라벨 평가 거절 |
| 옥석현 | 검색 필터 보존, 문서/질의 임베딩 호환, 미확정 금액·확인 완료 조건, 수치/근거/버전 일치, 구상 금액 구분 |

외부 모델·GPU·DB 통합 검증은 결정적 로직 테스트와 분리하고 비용·데이터·환경을 기록한다.
11/12 통합 검수, 11/13 결함 수정·최종 재현을 목표로 한다.
