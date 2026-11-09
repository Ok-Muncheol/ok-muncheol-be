# pipeline — 배포 파이프라인 (하네스 중립 명세)

이슈 브랜치의 변경을 커밋 → `main` PR → CI 체크 → squash 머지 → main push CI 확인 → (Vercel 연결 시) 프로덕션 배포 확인까지 자동화한다. `ship`의 Phase 4에서 사용한다.
브랜치명에 `#`이 포함되므로 모든 git/gh 명령에서 브랜치명은 따옴표로 감싼다.

> **이 파일은 하네스 중립 「진실의 원천」이다.** 진입 어댑터(`ship`)를 통해 실행되며, 어댑터가 「폴링 실행 규칙」의 모드와 아래 하네스별 값을 지정한다:
>
> | 하네스 | 커밋 Co-Author 트레일러 (Step 1-1 커밋 메시지 끝 한 줄) | PR 본문 서명 (Step 1-4 본문 끝) |
> |--------|---------------------------------------------------------|---------------------------------|
> | Claude Code | 세션 시스템 규칙이 지정하는 `Co-Authored-By:` 트레일러 | `🤖 Generated with [Claude Code](https://claude.com/claude-code)` |
> | Codex | `Co-Authored-By: Codex <noreply@openai.com>` | 없음 |

**실행 주체**: 전 과정(Step 1~4)을 하나의 실행 흐름이 직접 수행한다. 파이프라인은 "액션 → 체크 폴링 → 다음 액션"의 반복이라, 폴링을 서브에이전트에 위임하면 완료 후 재개가 안 돼 대기 지점에서 멈춘다.
- **Claude Code**: 메인 세션이 직접 수행하고 서브에이전트에 위임하지 않는다. 메인 세션의 `run_in_background` Bash는 완료 시 세션을 확실히 재호출한다.
- **Codex**: 단일 호출 흐름에서 순차 수행한다. 서브에이전트(`spawn_agent`)에 폴링을 맡기지 않는다.

## 폴링 실행 규칙

CI·PR 체크·Vercel 배포 대기는 `automation/bin/`의 **폴링 스크립트**(`pr_gate.py`, `run_wait.py`, `deploy_wait.py`)로 실행한다 — 루프를 즉석에서 작성하지 않는다. 스크립트는 표준 라이브러리만 쓰는 Python이라 가상환경 인터프리터로 부른다(`.venv/bin/python`, Windows는 `.venv\Scripts\python.exe` — `automation/ship.md` 「실행 환경」). 스크립트의 *실행 모드*는 진입 어댑터가 지정한 **하네스별 폴링 모드**를 따른다:

| 하네스 | 폴링 모드 |
|--------|-----------|
| **Claude Code** | 스크립트를 **`run_in_background: true` Bash**로 실행. 포그라운드 `sleep` 차단·Monitor 도구 금지(단일 완료 대기에 부적합). 스크립트 완료 시 세션이 자동 재호출된다. |
| **Codex** | 스크립트를 포그라운드 **블로킹** 호출로 완료까지 대기 — 배경 재호출 프리미티브가 없으므로 스크립트가 종료 마커를 출력하고 반환할 때까지 그 호출이 블로킹된다. 출력은 파일로 리다이렉트(`> "$LOG" 2>&1`)하고, **셸 명령 타임아웃을 스크립트 내부 타임아웃보다 넉넉히** 둔다(게이트 10분 → 셸 15분+. 배포 대기는 health 재시도까지 최대 약 12분 → 셸 15분+). 샌드박스에 네트워크가 열려 있어야 한다(어댑터 참조). |

- 스크립트는 성공·실패·타임아웃 **모든 종료 상태에서 exit**하고, 종료 직전 결과 마커 1줄(`... result=...`)을 출력한다. 출력의 마커·종료코드로 분기한다. 게이트 정의·간격·타임아웃·마커 규약은 **각 스크립트 상단 주석이 원천**이다.
- GitHub 조회 실패는 pending으로 삼키지 않는다 — 3회 연속 실패 시 `result=API_ERROR` 마커로 종료된다(스크립트에 구현됨). `API_ERROR`는 체크 실패나 `TIMEOUT`과 구분해 실제 stderr를 요약하고 중단한다. GitHub 장애로 단정하지 말고, 하네스의 네트워크 권한·DNS·인증 상태를 함께 확인한다.
- `result=ERROR`는 gh 부재·판정 입력 이상·예상 못 한 예외 등 **실행 환경 문제**다(fail-closed) — 통과로 취급하지 않고 환경을 확인해 보고 후 중단한다.
- **알림이 애매하거나 오래 소식이 없으면 추측·무한 대기 말고 즉시 `gh pr view/checks`·`gh run list`로 실제 상태를 확인**한 뒤 분기한다.

## 제목 규범 — main 한 줄 = 한 변경

| 대상 | 형식 | 예 |
|------|------|----|
| 커밋 제목 (Step 1-1) | `[TYPE] 설명` | `[FEAT] 사실 카드 확정 API를 추가한다` |
| PR 제목 (Step 1-4) | `[<타입>/#<이슈번호>] 설명` | `[feat/#5] 사실 카드 확정 API를 추가한다` |
| squash 머지 제목 (Step 1-5) | `[TYPE] 설명 (#<PR번호>)` | `[FEAT] 사실 카드 확정 API를 추가한다 (#6)` |

- **squash 제목은 `--subject`로 고정한다.** GitHub의 squash 기본 제목은 PR 커밋이 하나면 그 커밋 제목, 둘 이상이면 PR 제목이라 재시도 커밋이 쌓이면 main 히스토리 형식이 흔들린다. `(#<PR번호>)` 접미사는 커밋↔PR 자동 링크이므로 기본값 그대로 붙인다.
- **머지는 `--squash`다. `--merge`·`--rebase`로 바꾸지 않는다.** GitHub Actions는 `push` 이벤트 런의 제목으로 **head 커밋 메시지 첫 줄**을 쓴다(`pull_request` 이벤트만 PR 제목을 쓴다). `ci.yml`은 `push: [main]`과 `pull_request` 양쪽에 걸려 있으므로, squash여야 main push CI 런이 `[FEAT] … (#6)`로 어느 변경의 CI인지 런 목록에서 식별된다. merge commit이면 런 제목이 `Merge pull request #N from Ok-Muncheol/<브랜치>`가 되고 main 히스토리에 브랜치 커밋이 그대로 섞여 「한 줄 = 한 변경」이 깨진다. `run-name`으로는 이 접두사를 걷어낼 수 없어 워크플로우 쪽에 우회책이 없다.
- 저장소는 squash/merge/rebase 세 방식을 모두 허용한다 — 이 규범을 지키는 장치는 이 문서뿐이므로 명령을 그대로 따른다.

## Step 1: 커밋 & PR & 머지 (main)

1. `git status --short`를 확인해 커밋 안 된 변경이 있으면 `git add -A` → **민감 파일 게이트 통과 후** 커밋한다. 제목은 「제목 규범」의 커밋 제목, 본문은 변경 요점을 `-` 불릿으로, 끝에 **현재 하네스의 Co-Author 트레일러** 한 줄. 이미 커밋돼 있고 스테이징 후에도 변경이 없으면 커밋을 스킵한다. 게이트는 반드시 스테이징 **후**에 돌린다 — add 이전 검사로는 untracked였던 민감 파일이 잡히지 않는다:
   - 게이트: `.venv/bin/python automation/bin/sensitive_gate.py` 실행. `SENSITIVE_GATE result=PASS` → 커밋 진행. `result=BLOCKED`(종료코드 1) → 출력된 파일을 `git reset HEAD <파일>`로 해제하고 보고 후 중단. `result=ERROR` → git 조회 실패(fail-closed) — 통과로 취급하지 않고 보고 후 중단.
2. `git pull origin main --rebase` — 충돌 시 `git rebase --abort` 후 보고하고 중단. (ship Preflight에서 이미 rebase했으므로 보통 no-op이다 — Phase 2 검증이 도는 동안 main이 움직인 경우의 안전망.)
3. `git push --force-with-lease -u origin "<브랜치명>"`
4. PR 확보 — **기존 PR 확인부터** (재시작 시 같은 브랜치에 열린 PR이 이미 있다):
   - `gh pr list --head "<브랜치명>" --base main --state open --json number --jq '.[0].number // empty'` — 번호가 나오면 **생성을 스킵**하고 그 번호를 `<PR번호>`로 재사용한다 (3의 push만으로 같은 PR이 업데이트되고 체크가 재실행된다).
   - 없으면 `.github/PULL_REQUEST_TEMPLATE.md`를 채워 `gh pr create --base main --head "<브랜치명>" --title "[<타입>/#<이슈번호>] 설명"`:
     - **변경 사항**: 변경 요점을 불릿으로. **변경 타입**: 타입 표의 해당 항목 하나만 `[x]`. **테스트 방법**: Phase 2에서 실제 수행한 명령과 결과(`make lint`, `make check`, `make test` 통과 개수·건너뜀 등). **참고 사항**: 계약 변경이면 FE 대응(타입·견본·화면)과 대응 FE PR, 마이그레이션 적용 필요, 환경변수 추가 등 머지 후 사람이 할 일 — 없으면 "없음". **연결된 이슈**: `close #<이슈번호>`. **체크리스트**: 실제 확인한 항목만 `[x]`, 환경변수 변경이 있으면 키 이름만 적는다.
     - 본문 끝에 현재 하네스의 **PR 본문 서명**(있으면)을 붙인다. **라벨·담당자를 붙이지 않는다.**
     - 출력 URL 끝 숫자가 `<PR번호>`.
5. 머지 게이트 폴링: **`.venv/bin/python automation/bin/pr_gate.py <PR번호> 600 check gitleaks`** (30초 간격, 최대 10분) → `PR_GATE_DONE` 마커. **게이트 = `mergeable` MERGEABLE + 앵커 체크(`check`·`gitleaks`) 존재 + 비차단 예외를 제외한 모든 체크가 완료·성공 (실패 0, 대기 0, 체크 1개 이상)** — 정확한 판정 로직·비차단 예외 목록은 `automation/bin/pr_gate.py`의 `judge()`·`EXEMPT`가 원천이다(`tests/automation/test_pr_gate.py`가 fixture로 검증). 체크를 이름 허용목록으로 고르지 않으므로 다른 잡도 자동으로 게이트에 포함되고, 잡이 추가·개명돼도 따라간다. Vercel을 연결하면 PR 브랜치의 Preview 배포가 `Vercel` 커밋 상태로 붙을 수 있고, 붙으면 같은 규칙으로 게이트에 들어간다 — 프로덕션 배포는 Step 3이 따로 확인한다. 이 클라이언트 게이트는 관측·진행 판단용이고, **강제는 `main` 브랜치 보호(required checks `check`·`gitleaks`)가 서버에서 한다** — CI 잡 이름을 바꾸면 브랜치 보호 설정도 함께 갱신한다. 의심스러우면 `gh api repos/Ok-Muncheol/ok-muncheol-be/branches/main/protection`으로 확인하고, 확인한 값과 다르면 이 문장을 고친다.
   - `result=PASS` → `gh pr merge <PR번호> --squash --subject "[TYPE] 설명 (#<PR번호>)"`. **`--auto`를 쓰지 않는 이유**: 머지를 서버에 위임하면 게이트 실패 시 아무 일도 일어나지 않은 채 대기만 남아 "실패 → 즉시 로그 요약·보고"라는 이 파이프라인의 실행 흐름이 끊긴다. 서버가 머지를 거부하면 `--admin`으로 우회하지 않고 게이트 실패로 보고한다.
   - `result=FAIL*` → 마커의 `failed=` 목록에 있는 체크의 로그를 요약해(`gh run view <run-id> --log-failed` 등) 보고하고 중단. `mergeable=CONFLICTING`·`TIMEOUT`·`API_ERROR`·`ERROR`도 각각 요약해 중단 (「폴링 실행 규칙」의 구분 참조).
6. `MERGE_SHA=$(gh pr view <PR번호> --json mergeCommit --jq '.mergeCommit.oid')`
7. `git checkout main && git pull origin main`
   - `main`이 기본 브랜치이므로 PR 본문의 `close #<이슈번호>`가 머지 시 이슈를 **자동으로 닫는다**. 따로 닫지 않는다.

## Step 2: main push CI 확인

`main` push(squash 머지)가 CI(`Backend CI`, `ci.yml`)를 다시 트리거한다. PR에서 이미 검증한 트리지만, main 쪽 런이 남아야 배포 대상 커밋의 검증 기록이 된다(`ci.yml`의 `concurrency` 주석).

CI 폴링: **`.venv/bin/python automation/bin/run_wait.py ci.yml main "<MERGE_SHA>" 600`** (30초 간격, 최대 10분) → `RUN_RESULT` 마커.
- `result=success` → Step 3으로.
- `result=failure` 등 실패 conclusion → `main`이 깨진 상태다. 마커의 `run_id=`로 `gh run view <run-id> --log-failed`를 요약하고 **즉시 보고**한다 (「실패 시 재시작 규칙」).
- `result=NOT_FOUND` → run이 아예 생성되지 않았다 — 워크플로우 미트리거(트리거 조건·파일명 변경 등) 신호이므로 `TIMEOUT`(진행 중일 수 있음)과 구분해 보고하고 중단. `TIMEOUT`·`API_ERROR`·`ERROR`는 「폴링 실행 규칙」대로 처리.

## Step 3: Vercel 프로덕션 배포 확인

Vercel 프로젝트 `ok-muncheol-be`가 이 저장소에 연결되어 있다(README 「기술 구성」, 2026-11-06). `main` push를 Vercel이 감지해 프로덕션 배포를 시작하고, 결과를 머지 커밋의 **커밋 상태**(context `Vercel` — pending "Vercel is deploying your app" → success "Deployment has completed")로 남긴다. GitHub Deployment는 보지 않는다 — Vercel 프로젝트 설정에 따라 생기지 않을 수 있다(lumi BE·FE 0건, 2026-10-27 실측). FE는 별도 Vercel 프로젝트(`ok-muncheol-fe`)라 이 단계는 BE 배포만 본다.

배포 폴링: **`.venv/bin/python automation/bin/deploy_wait.py "<MERGE_SHA>"`** (30초 간격, 최대 10분) → `DEPLOY_RESULT` 마커. 연결 여부도 이 스크립트가 판정한다 — 머지 커밋에 `Vercel` 상태가 2분 안에 생기지 않으면 직전 `main` 커밋(첫 부모)의 상태로 가른다. 상태를 봤으면 마커에 `url=`(Vercel 배포 페이지)이 함께 찍힌다. 두 번째 인자로 health URL을 넘겨(`deploy_wait.py "<MERGE_SHA>" https://ok-muncheol-be.vercel.app/health`) 배포 뒤 200인지까지 본다(최대 3회, `health=`가 마커에 찍힌다). 운영 도메인이 바뀌면 이 URL을 고친다. 판정 로직은 스크립트 상단 주석이 원천이다(`tests/automation/test_deploy_wait.py`가 대역으로 검증).
- `result=NOT_CONNECTED`(종료코드 0) → **Vercel 미연결** — 머지 커밋과 직전 `main` 커밋 어디에도 `Vercel` 상태가 없다. 지금은 연결돼 있으므로 이 결과가 나오면 Git 연동이 끊긴 것이다 — 대시보드의 Git 연결을 확인하도록 안내한다. Step 4로 가고, 최종 보고에 "프로덕션 배포 없음(Vercel 미연결) — `main` 반영까지"를 명시한다.
- `result=success` → 배포 완료(health URL을 넘겼으면 200까지). Step 4로.
- `result=failure`·`error` → `url=`의 배포 페이지에서 로그를 확인하도록 안내하고 보고한다. Vercel은 이전 배포로 **즉시 롤백**이 가능하다.
- `result=UNHEALTHY` → 배포는 끝났는데 health URL이 3번 모두 200이 아니다. 마커 앞 줄(`health <코드> (n/3): <응답 앞부분>`)을 요약해 보고한다 — 환경변수·마이그레이션 같은 배포 환경 쪽을 먼저 의심한다. `health=unreachable`이면 도메인·Vercel 상태를 확인한다.
- `result=NOT_FOUND` → 직전 `main` 커밋은 배포됐는데 이 커밋의 배포가 생기지 않았다 — Vercel 프로젝트의 Production Branch가 `main`인지, Git 연동이 살아 있는지 확인을 안내한다. 팀원이 작성한 커밋이면 Hobby 플랜 제한(프로젝트 소유자가 작성한 커밋만 배포)일 수 있으니 대시보드의 Redeploy도 안내한다.
- `result=TIMEOUT` → 아직 진행 중일 수 있으니 단정하지 말고 `url=`로 확인을 안내한다. `API_ERROR` → 스크립트 stderr를 요약한다. `ERROR`(대문자) → 배포 상태 `error`와 다른 실행 환경 문제다 — 「폴링 실행 규칙」대로 처리한다.

> PR 브랜치의 Preview 배포는 켜져 있다(2026-11-06 PR #10·#14 실측 — `Vercel`·`Vercel Preview Comments` 체크가 PR에 붙는다). Hobby 플랜은 배포를 하루(24시간) 100번까지만 만들어 PR push가 잦아지면 `main` 배포까지 막힐 수 있다 — 그때는 lumi처럼 `vercel.json`의 `git.deploymentEnabled`로 `main`만 배포하도록 바꾼다(2026-10-29). 그러면 변경이 Vercel에서 처음 빌드되는 곳이 프로덕션 배포이므로, 머지 전 확인은 CI `check`(ruff·`make check`·pytest)가 맡는다. 어느 쪽이든 브랜치 보호의 required check에는 `Vercel`을 넣지 않는다.

## Step 4: 정리 & 보고

각 명령이 실패해도 **중단하지 않고 계속 진행**한다(실패 내역만 기록):
1. `git branch -D "<브랜치명>"`
2. `git push origin --delete "<브랜치명>"` — 레포 설정(`delete_branch_on_merge`)이 머지 시 원격 브랜치를 자동 삭제하므로 보통 "remote ref does not exist"다. 정리 실패로 세지 않고 무시한다.
3. 최종 보고:
   - main 머지 커밋(`MERGE_SHA`)·main CI 결과, 닫힌 이슈 번호
   - **FE 대응 안내**(계약 변경 시): `src/lib/api/types.ts`·`src/fixtures/contracts.json`·소비 화면을 같은 계약으로 맞추는 FE 작업이 남았다 (`docs/contracts/README.md`).
   - **마이그레이션 적용 안내**(해당 시): `supabase/migrations/<파일>`을 Supabase에 적용해야 한다 — 대시보드 SQL Editor 또는 `psql "$DATABASE_URL" -f <파일>`. 적용 전까지 관련 기능은 동작하지 않는다.
   - 정리 실패 내역(있으면). 머지 성공 + 정리 실패는 파이프라인 실패로 취급하지 않는다.
   - 프로덕션 배포 결과(`DEPLOY_RESULT`의 `result`·`health`) — 또는 **"프로덕션 배포 없음(Vercel 미연결)"**. 배포됐다고 오해하지 않도록 명시한다.

## 실패 시 재시작 규칙

이슈 브랜치와 PR은 재사용한다 — 수정 커밋을 **같은 브랜치에 push**하면 CI 체크가 재실행되고 같은 PR이 업데이트된다. **코드 수정이 수반되면 반드시 Step 1부터 다시 거친다 — CI 검증 없는 머지 금지.**

| 실패 시점 | 원인 | 재시작 |
|-----------|------|--------|
| Step 1-5 | CI 체크 실패 | 코드 수정 → **Step 1** (같은 브랜치에 새 커밋 push, 게이트 폴링 재개) |
| Step 1-5 | PR 충돌(`CONFLICTING`) | `git pull origin main --rebase`로 해결(자동 해결 금지 — 충돌 내용을 보고하고 사용자가 정한다) → **Step 1** |
| Step 2 | main push CI 실패 | `main`이 깨졌다. 즉시 보고하고 사용자 판단으로 수정 커밋(새 이슈·브랜치) → **ship 처음부터** |
| Step 3 | Vercel 프로덕션 배포 실패(`failure`·`error`) | 코드 수정 → **ship 처음부터** (새 머지 → 새 배포). 필요 시 Vercel 대시보드에서 이전 배포로 롤백 |
| Step 3 | `NOT_FOUND`·`UNHEALTHY` | 대개 코드가 아니라 Vercel·DB 쪽이다 — Redeploy·환경변수(바꾸면 Redeploy해야 반영)·마이그레이션을 처리한 뒤 `deploy_wait.py`를 같은 인자로 다시 돌린다. 코드 문제면 **ship 처음부터** |

- 재시작에 새 브랜치가 필요하면 같은 이슈 번호로 `fix/#<이슈번호>`를 쓴다 (기존 로컬 브랜치는 삭제 후 생성).

## DB 마이그레이션 원칙 (Supabase 공용 DB)

- Supabase DB는 팀이 함께 쓰므로 **`main`에 머지된 마이그레이션만 적용**하고, PR 단계의 스키마 변경은 로컬 Postgres(pgvector·pg_trgm 확장)에서 먼저 확인한다. 적용은 사람이 한다(SQL Editor 또는 `psql`) — ship은 Step 4에서 안내만 한다.
- 파일명은 Supabase CLI 규칙(`supabase/migrations/<YYYYMMDDHHMMSS>_<이름>.sql`)을 따른다. 담당별 테이블·RLS 계획은 `supabase/migrations/README.md`에 있다.
- **파괴적 변경은 2단계로 나눈다.** 파괴적 마이그레이션은 앱 코드를 되돌려도 이미 적용된 스키마가 되돌아가지 않아, 구버전 코드가 사라진 컬럼·테이블을 참조하며 런타임 오류를 낸다.
  - **2단계 필수**: `DROP TABLE/COLUMN`, 타입 축소, `RENAME COLUMN/TABLE`, 기존 데이터가 있는 컬럼의 `ADD NOT NULL`
  - **1단계로 충분**: `CREATE TABLE`, `ADD COLUMN`(nullable/default), 타입 확장, 인덱스·확장(extension) 추가
  - 분리 방법 — **1차**: 앱 코드에서 기존 스키마 참조 제거 + additive 마이그레이션(있으면). **2차**: 파괴적 마이그레이션(1차가 적용돼 안정된 뒤 별도 ship).

## 환경변수 추가 체크리스트

**새 환경변수** 도입 시 머지 전 아래를 모두 확인한다 (누락 시 앱 시작 실패 또는 기능 무동작). LLM·VLM·Modal·DB·Supabase 서버 비밀키는 BE에서만 관리한다(README 「기술 구성」).

1. `.env.example` — 키·주석 추가 (실값 금지. `.env.example`만 커밋 허용이다. 파일이 없으면 이 변경에서 만든다)
2. `.env` — 로컬 값 추가 (커밋 금지)
3. 설정을 읽는 코드 — 비어 있을 때의 동작을 정한다 (없어도 뜨는지, 필수인지)
4. `README.md` — 관련 절에 설명
5. 배포 환경 변수 — Vercel이 연결돼 있으면 프로젝트 Settings → Environment Variables에 등록하고, 다시 배포해야 반영된다. 미연결이어도 PR 본문 「참고 사항」에 키 이름만 적는다.

## 민감 파일 커밋 금지

`git add/commit/push` 대상에 아래 민감 파일이 포함되지 않도록 커밋 전 확인한다 (기본 이름 및 `<이름>.*` 확장 변형 포함). 기계적 검사는 Step 1의 **`automation/bin/sensitive_gate.py`**(스테이징 후 `git diff --cached` 검사)가 수행한다 — **스크립트의 정규식이 패턴의 단일 원천**이고, 아래 목록과 훅(`automation/hooks/validate-git-sensitive.sh`)의 배열은 사람용 요약/이중 방어다(불일치하면 스크립트가 우선). 패턴 변경은 스크립트에서 하고 훅을 같이 갱신한다:

- `.env` (및 `.env.local`, `.env.prod` 등 `.env.*` — `.env.example` 제외)
- `.claude/settings.local.json` (Claude Code 개인 권한 설정)
- `.mcp.json` (로컬 IDE MCP 연결 — 개인 환경 전용)
- `.vercel/` (Vercel 프로젝트/조직 ID — `vercel link` 산출물)
- `certs/` 하위 키·인증서, `*.pem`, `*.p12`, `*.p8`, `*.key`
- `data/private/` 하위 전부 (AI Hub 원본 영상·실제 사건·이용조건 미확인 원문·가중치를 두는 비공개 폴더 — TEAM.md 「검증·PR 규칙」 5)
- `firebase-adminsdk*.json`, GCP 서비스 계정 키 등 자격증명 JSON
- `id_rsa*`

발견 시 커밋하지 말고 사용자에게 보고한다 — 스테이징돼 있으면 `git reset HEAD <file>`로 해제하고, 이미 커밋에 포함됐으면 해당 커밋을 정리한 뒤 재시도한다. 파일 이름으로 가릴 수 없는 것(다른 폴더에 둔 원본 영상·실제 사건 자료·가중치)은 게이트가 잡지 못한다 — Phase 1의 변경 분석에서 보이면 보고하고 중단한다.

> **하네스별 적용**: 모든 하네스가 Step 1에서 `sensitive_gate.py`를 실행한다. 훅을 지원하는 하네스(Claude Code `.claude/settings.json`, Codex `.codex/hooks.json`)는 같은 스크립트 `automation/hooks/validate-git-sensitive.sh`가 PreToolUse에서 `git add/commit/push`를 추가로 자동 차단한다(이중 방어). Codex 훅은 프로젝트 신뢰 + `/hooks` 승인 뒤에만 동작하므로, 승인 전에는 규칙을 직접 준수한다. 훅 스크립트는 bash·jq로 돌아 Windows 네이티브 Codex(PowerShell)에서는 실행되지 않는다 — 그 환경에서는 훅을 승인하지 않고 규칙을 직접 준수한다. 어느 하네스든 `.gitignore` + CI 단 gitleaks와 병행되는 것을 전제로 한다.

## 규칙

- 모든 `gh`/`git` 실패 시 에러 내용을 사용자에게 보고한다.
- PR 생성 시 `.github/PULL_REQUEST_TEMPLATE.md` 형식을 준수하고, **라벨·담당자는 부여하지 않는다**(라벨은 이슈 몫이다).
- **머지는 항상 `--squash` + `--subject`다** — `--merge`·`--rebase`로 바꾸지 않는다. 이유는 「제목 규범」 참조.
- **CI 체크 통과 없이 머지하지 않는다.** 브랜치 보호가 이를 서버에서도 강제한다 — 머지가 거부되면 `--admin`으로 우회하지 말고 게이트 실패로 보고한다.
- **`main`에 직접 커밋·push하지 않는다.** 변경은 반드시 이슈 브랜치 → PR을 거친다.
- **실패 시 재시작 규칙을 반드시 따른다.**
- **「민감 파일 커밋 금지」 규칙을 준수한다.**
