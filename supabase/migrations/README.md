# DB·RLS 마이그레이션

구준휘가 통합하고 정지원·김지은·옥석현이 자기 테이블을 새 마이그레이션 파일로 더한다.
파일명은 Supabase CLI 규칙 `<YYYYMMDDHHMMSS>_<이름>.sql`이고 파일명 순으로 적용한다.
**머지된 파일은 고치지 않는다.** 바꿀 것이 생기면 새 파일을 추가한다.

| 파일 | 담당 | 내용 |
| --- | --- | --- |
| `20261109163900_case_access_base.sql` | 구준휘(통합) | `cases` 최소 골격, `private.can_access_case()`, `private.set_updated_at()` |
| `20261109163901_judgment_runs.sql` | 구준휘 | 판단 실행·구분 질문/응답·검증 로그·요청 멱등성, `langgraph` 스키마 |
| `20261111090000_review_reports.sql` | 옥석현 | 검토 보고서 버전·서류·면책/협의 입력·구상 청구서(`review_reports`·`case_documents`·`report_case_inputs`·`claim_packages`), 시퀀스 권한 회수 |

## 공통 원칙

- **쓰기는 BE만 한다.** 브라우저(authenticated)는 담당 사건 행의 SELECT만 받는다. Realtime 변경 알림과 새로고침 뒤 조회 복원에 쓴다.
- **권한은 테이블마다 명시한다.** Supabase 기본 권한은 새 public 테이블에 anon·authenticated의 쓰기까지 준다.
  테이블마다 `revoke all ... from anon, authenticated` 뒤 필요한 것만 `grant`하고, service_role에는 전체를 준다.
- **public 테이블은 모두 RLS를 켠다.** `tests/migrations/test_migration_files.py`가 검사한다.
- **사건 단위 테이블은 `case_id`와 `private.can_access_case(case_id)`로 정책을 건다.** 자식 테이블도 `case_id` 사본을 두고
  부모와 `(id, case_id)` 복합 외래키로 묶는다. 행마다 조인 없이 RLS를 판정하고 사본이 어긋나지 않는다.
- **RLS 도우미는 `private` 스키마에 둔다.** Data API에 노출되지 않는다(security definer 함수 노출 금지).
- 타 담당 사건/파일 차단, 서버 전용 비밀키, 카드/판단/보고서 버전 외래키, 중복 실행 방지를 검수한다.

## 담당별로 더할 것

| 담당 | 테이블/규칙 | 이 기반과 맞출 점 |
| --- | --- | --- |
| 정지원 | 사건 범위·상태 칼럼, 파일·작업·사실 카드 버전·수정/확정 이력, Storage RLS | `cases`는 새 파일의 ADD COLUMN으로 넓힌다. 카드 버전 테이블이 생기면 판단 테이블의 `(case_id, fact_card_version)`·`expected_card_version`·`resulting_card_version` 외래키를 새 파일로 추가한다 |
| 김지은 | 도표·수정요소·관계·참고 판례·결정례·약관·문서/청크·임베딩 메타데이터 | 사건 단위가 아니므로 원문 이용조건에 맞춰 공개 범위를 정한다 |
| 옥석현 | 공통 검색 함수·인덱스·코퍼스별 통계, 산정·보고서·승인·구상 버전 | 보고서는 `judgment_runs (case_id, judgment_version)`을 참조한다 |

pgvector 차원·pg_trgm·Kiwi 토큰 통계는 김지은·옥석현 계약 후 SQL로 확정한다. lumi의 논문·웰니스 테이블을 복제하지 않는다.

## 판단 테이블

| 테이블 | 핵심 제약 |
| --- | --- |
| `judgment_runs` | 작업 상태(`status`)와 판정 상태(`judgment_status`) 분리, 반박 재실행 1회, A+B=100, 결과의 `versions` = 행의 버전, 사건마다 진행 중인 판단 하나, 같은 입력(카드·인정기준·규칙 버전) 재판단 금지 |
| `judgment_questions` | 실행마다 카드 칸 하나에 질문 하나 |
| `judgment_answers` | 질문마다 응답 하나, 모른다면 값 없음, 반영한 카드는 새 버전 |
| `validation_checks` | 시도(0·1)·겹마다 결과 하나, 실패하면 이유 필수 |
| `api_idempotency_keys` | 키 8~128자, 같은 키는 한 번, 브라우저 접근 없음 |

- 판정 칼럼(`judgment_status`·`chart_id`·`edition`·`fault_a`·`fault_b`)은 `result`(JudgmentResult)에서 생성한다. BE는 `result`만 쓴다.
- 같은 카드 버전의 확인/재개는 같은 실행을 잇는다. 응답으로 카드 버전이 바뀌면 새 실행을 `resumed_from_run_id`로 잇고
  이전 실행에 `superseded_at`을 찍는다. 대체된 실행은 진행 중으로 세지 않는다.
- LangGraph 체크포인트는 `langgraph-checkpoint-postgres`의 `setup()`이 `langgraph` 스키마에 만든다. `thread_id`는 `judgment_runs.id`다.
- Realtime 발행 대상은 `judgment_runs`·`judgment_questions`다.

## 로컬 검사

Docker로 버려도 되는 Postgres를 띄우고 돌린다. 테스트가 임시 데이터베이스를 만들고 지운다. **Supabase 주소를 넣지 않는다.**
같은 변수로 판단 저장 검사(`tests/api/test_judgment_service.py`)도 돈다. CI는 Postgres 서비스로 늘 돌린다.

```bash
docker run --rm -d --name okm-migration-pg -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:54329:5432 postgres:17-alpine
MIGRATION_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54329/postgres make test ARGS="tests/migrations -q"
docker stop okm-migration-pg
```

`psql`이 필요하다(macOS는 `brew install libpq`). `tests/migrations/supabase_stub.sql`이 Supabase의 역할·`auth.uid()`·기본 권한·
Realtime publication을 흉내 내고, `tests/migrations/behavior.sql`이 RLS·권한·제약을 검사한다.
변수가 없으면 DB 검사는 건너뛰고 파일 규칙·계약 동기화 검사만 돈다(CI).

## 적용

`main`에 머지된 파일만 공용 Supabase DB에 파일명 순으로 적용한다. 대시보드 SQL Editor 또는
`psql "$DATABASE_URL" --single-transaction -f <파일>`. 적용은 사람이 하고 ship은 안내만 한다(`automation/pipeline.md` 「DB 마이그레이션 원칙」).
파괴적 변경은 2단계로 나눈다.

LangGraph 체크포인트 테이블은 마이그레이션 뒤 한 번 `.venv/bin/python scripts/setup_checkpointer.py`로 `langgraph` 스키마에
만든다(`DATABASE_URL`, 여러 번 실행해도 된다). `langgraph-checkpoint-postgres`를 올리면 다시 실행한다.
공용 DB 연결은 Session pooler 주소(5432)를 쓴다. 직접 연결 주소는 IPv6 전용이고, Transaction pooler(6543)는 세션 설정
(`search_path`)이 유지되지 않는다.
