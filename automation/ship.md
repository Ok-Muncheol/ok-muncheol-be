# ship — 코드 변경 배포 자동화 (하네스 중립 명세)

워킹 트리의 코드 변경사항을 검증하고 이슈 → 브랜치 → PR → CI → `main` 머지 → (Vercel 연결 시) 프로덕션 배포 확인까지 자동화한다.
코드 수정이 이미 완료된 상태에서 사용한다 — 문제 분석·코드 변경은 수행하지 않는다.
브랜치명에 `#`이 포함되므로(`feat/#12`), 모든 git/gh 명령에서 브랜치명은 반드시 따옴표로 감싼다.

> **이 파일은 하네스 중립 「진실의 원천」이다.** 직접 실행 대상이 아니라, 사용하는 LLM 하네스의 진입 어댑터를 통해 실행된다:
> - Claude Code → `.claude/commands/ship.md` (`/ship`)
> - Codex → `.agents/skills/ship/SKILL.md` (`$ship`)
>
> 각 어댑터는 **폴링 모드**, **Co-Author 트레일러**, **안전 규칙 적용 방식**(훅 유무)을 지정한다. 배포(Phase 4)의 CI·PR 체크·Vercel 배포 폴링은 `automation/pipeline.md`의 「폴링 실행 규칙」에 정의된 **하네스별 모드**를 따른다.

## 실행 환경

macOS·Linux·WSL과 Windows(PowerShell)에서 같은 절차를 쓴다. 게이트·폴링 스크립트(`automation/bin/*.py`)는 표준 라이브러리만 쓰는 Python이라 bash·jq 없이 돈다.

- **가상환경 인터프리터**: 명세는 `.venv/bin/python`·`.venv/bin/pip`로 적는다. Windows 가상환경은 `.venv\Scripts\python.exe`·`.venv\Scripts\pip.exe`이므로 그 경로로 바꿔 실행한다.
- **셸 문법**: 명세의 명령은 bash 표기다. 셸이 PowerShell이면 `VAR=$(…)`·`&&` 같은 문법을 PowerShell로 옮긴다(Windows PowerShell 5.1에는 `&&`가 없다). 브랜치명을 감싸는 따옴표는 PowerShell에서도 필수다 — 따옴표 없는 `#`는 주석 시작이라 `feat/#12`가 `feat/`로 잘린다.

## 워크플로우 전제 (ok-muncheol-be)

README 「브랜치·PR 규칙」이 원천이다 (FE 레포와 같은 규칙). 요약:

- **`dev` 없이 feature → `main`.** 배포 환경이 `main` 프로덕션(Vercel) 하나뿐이라 `dev`를 두어도 나뉘는 환경이 없다. `main`이 곧 통합 브랜치다.
- **이슈 퍼스트.** 이슈 템플릿(`.github/ISSUE_TEMPLATE/*.yml`)으로 이슈를 만들고 `<타입>/#<이슈번호>` 브랜치를 딴다. 타입 라벨(`✨ FEAT` 등 6개)이 레포에 없으면 Phase 3에서 만든다.
- **PR 제목 `[<타입>/#<이슈번호>] 설명`, 본문에 `close #<이슈번호>`.** `main`은 PR 필수(승인 0명), 필수 체크 `check`(ruff·`make check`·pytest)·`gitleaks`, 강제 푸시·삭제 금지로 보호된다. 머지는 **squash**다 (`automation/pipeline.md` 「제목 규범」).
- **계약은 FE와 짝으로 바꾼다.** `app/schemas/` → `make contracts` → FE 타입·견본·화면 순서로 반영하고(`docs/contracts/README.md`), PR 본문에 대응 FE PR을 적는다(TEAM.md 「검증·PR 규칙」).
- **배포는 Vercel 프로젝트 `ok-muncheol-be`에 연결되어 있다** (README 「기술 구성」, 2026-11-06 — FE·API를 한 프로젝트로 묶는 방식은 아직 미정). ship의 종착점은 **`main` 머지 + main push CI 성공 + 머지 커밋의 프로덕션 배포·`/health` 200**이다. `automation/pipeline.md` Step 3이 머지 커밋의 `Vercel` 커밋 상태로 판정한다.
- **DB 마이그레이션은 사람이 적용한다.** Supabase DB는 팀이 함께 쓰므로 `main`에 머지된 마이그레이션만 적용하고, 적용 자체는 ship이 하지 않는다 — 머지 후 최종 보고에서 적용 대상 파일을 안내한다. (적용 순서·방법은 `supabase/migrations/README.md` 「적용」)

## Preflight

1. `gh auth status` — 인증 실패 시 **중단**.
2. `git fetch origin main` — 이후 모든 main 대비 비교(변경 유무 판정, 변경 분석)는 **`origin/main` 기준**이다. 로컬 `main`은 뒤처져 있을 수 있으므로 비교 기준으로 쓰지 않는다.
3. 현재 브랜치 판별:
   - **이슈 브랜치** (`<타입>/#<번호>` 형식):
     - 브랜치명에서 타입과 이슈번호를 파싱한다. `gh issue view <번호> --json state,title`로 이슈가 열려 있는지 확인한다 — 없거나 닫혀 있으면 보고하고 **중단**.
     - `git log origin/main..HEAD --oneline`과 `git status --porcelain`으로 변경사항 확인 — 커밋도 변경도 없으면 **중단** ("배포할 변경사항이 없습니다"). 변경 유무 판정에 `git diff`(무인자)를 쓰지 않는다 — staged 변경이 보이지 않는다.
     - 최신 동기화: `git pull --rebase --autostash origin main` — **충돌 시 자동 해결하지 않는다**: rebase 충돌은 `git rebase --abort`로 원상복구 후 보고하고 중단, autostash 재적용 충돌은 변경이 stash에 보존된 상태이므로(`git stash list`로 확인) 그대로 두고 보고하고 중단한다. 이 rebase로 Phase 1의 분석 diff에 main 쪽 무관한 변경이 섞이지 않고, Phase 2가 실제 push될 트리를 검증하며, `automation/pipeline.md` Step 1-2의 rebase는 사실상 no-op이 된다. 브랜치가 이미 push돼 있었어도 Step 1-3의 `--force-with-lease` push가 재작성된 커밋을 안전하게 반영한다.
     - **Phase 3(이슈·브랜치 생성)을 스킵**한다 — 타입은 브랜치 prefix, 이슈는 브랜치 번호로 확정.
   - **`main`**:
     - `git log origin/main..HEAD --oneline` — **push 안 된 로컬 커밋이 있으면 중단**하고 커밋 목록과 함께 보고한다. `main` 직접 커밋은 ship이 배포하지 않는다 — 워킹트리만 보는 아래 판정이 이 커밋들을 놓치므로, 사용자가 브랜치로 옮기는 등 직접 처리해야 한다 (`main`은 push 보호 대상이기도 하다).
     - `git status --porcelain` — 변경사항이 없으면 **중단** ("배포할 변경사항이 없습니다").
     - 최신 동기화: `git pull --rebase --autostash origin main` — 충돌 처리는 위 이슈 브랜치 경우와 동일하다(**자동 해결 금지** — 원상복구·보고·중단).
     - Phase 1로 진행한다. 이슈·브랜치는 Phase 3에서 만든다.

## Phase 1: 변경사항 분석

> 사용자가 인자로 타입과 설명을 직접 제공한 경우 (예: `/ship feat 사실 카드 확정 API 추가`), 해당 값을 사용하고 사용자 확인 없이 Phase 2로 직행한다.

1. `git diff origin/main`과 `git status --porcelain`(untracked 신규 파일 확인)으로 변경된 파일과 내용을 분석한다. 무인자 `git diff`를 쓰지 않는다 — staged·커밋된 변경이 보이지 않아, 변경이 모두 커밋된 이슈 브랜치에서는 분석 대상이 비어 보인다. Preflight의 rebase가 선행되므로 이 diff에는 main 쪽 무관한 변경이 섞이지 않는다.
2. 변경 성격에 맞는 타입을 결정한다. **타입 토큰은 이슈 템플릿 제목 접두사의 소문자**이고, 브랜치·PR·커밋·라벨이 모두 여기서 파생된다:

   | 타입 | 이슈 템플릿 | 이슈 제목·커밋 제목 | 라벨 (색) | 브랜치 | PR 제목 | PR 「변경 타입」 |
   |------|-------------|---------------------|-----------|--------|---------|-----------------|
   | `feat` | `기능-구현.yml` | `[FEAT] 설명` | `✨ FEAT` (`a2eeef`) | `feat/#N` | `[feat/#N] 설명` | 기능 구현 |
   | `fix` | `기능-수정.yml` | `[FIX] 설명` | `🔧 FIX` (`fbca04`) | `fix/#N` | `[fix/#N] 설명` | 기능 수정 |
   | `bug` | `오류-수정.yml` | `[BUG] 설명` | `🕷️ BUG` (`d73a4a`) | `bug/#N` | `[bug/#N] 설명` | 오류 수정 |
   | `refact` | `리팩토링.yml` | `[REFACT] 설명` | `♻️ REFACT` (`c2e0c6`) | `refact/#N` | `[refact/#N] 설명` | 리팩토링 |
   | `doc` | `문서-작업.yml` | `[DOC] 설명` | `📜 DOC` (`0075ca`) | `doc/#N` | `[doc/#N] 설명` | 문서 작업 |
   | `chore` | `기타-수정.yml` | `[CHORE] 설명` | `🔩 CHORE` (`cfd3d7`) | `chore/#N` | `[chore/#N] 설명` | 기타 수정 |

   `fix`와 `bug`를 가른다 — `bug`는 **동작이 틀린 것**(예상과 다른 응답·예외), `fix`는 **의도 변경**(동작은 맞지만 기준을 바꾸는 수정). 템플릿 설명(「리팩토링 목적이 아닌 기존 기능을 수정」 vs 「버그 및 예상과 다르게 동작하는 문제」)이 기준이다.
3. 변경 내용을 한 줄로 요약한다 — 커밋·이슈·PR 제목에 그대로 쓰이므로 한국어 서술형(`…한다`)으로 쓴다 (예: `[FEAT] 사실 카드 확정 API를 추가한다`).
4. **사용자 확인 대기**:
   ```
   변경사항 요약:
   - 타입: feat
   - 설명: ...
   - 변경 파일: N개
   - 계약: 없음 | app/schemas/… (대응 FE 작업 필요)
   - 마이그레이션: 없음 | supabase/migrations/… (머지 후 수동 적용 필요)

   이대로 진행할까요? (타입이나 설명을 변경하려면 알려주세요)
   ```

## Phase 2: 검증

CI(`.github/workflows/ci.yml`)의 **`check` 잡과 같은 명령을 같은 순서**로 로컬 선검증한다. `gitleaks` 잡(커밋 이력 시크릿 스캔)은 로컬 선검증 대상이 아니다 — Phase 4의 머지 게이트가 잡는다. 게이트·폴링 스크립트의 테스트(`tests/automation/`)는 `make test`에 들어 있다.

1. **의존성**: `.venv`가 없으면 README 「개발 시작과 검증」대로 만들고, `.venv`가 없었거나 `pyproject.toml`이 변경에 포함됐으면 `.venv/bin/pip install -e ".[dev]"`.
2. **린트**: `make lint` (ruff). `make`가 없으면(Windows 기본) 같은 명령인 `.venv/bin/python -m ruff check .`
3. **초안 검사**: `make check` (문법·import·타입 참조·JSON·계약 견본 동기화 — `scripts/check_scaffold.py`). `make`가 없으면 `.venv/bin/python scripts/check_scaffold.py`. `Stale contract fixture`로 실패하면 계약 변경에 `make contracts`가 빠진 것이다 — 8의 규칙대로 보고한다.
4. **테스트**: `make test` (pytest). `make`가 없으면 `.venv/bin/python -m pytest`
5. **계약 체크**: `app/schemas/`·`docs/contracts/`가 변경에 포함됐으면 FE 대응(`src/lib/api/types.ts`·`src/fixtures/contracts.json`·소비 화면)이 필요한 변경으로 기록한다(Phase 4에 전달 — PR 「참고 사항」에 적는다).
6. **환경변수 체크**: 새 환경변수가 도입됐으면 `automation/pipeline.md`의 「환경변수 추가 체크리스트」를 확인한다.
7. **마이그레이션 체크**: `supabase/migrations/*.sql`이 변경에 포함됐으면 `automation/pipeline.md`의 「DB 마이그레이션 원칙」에 따라 파괴적 변경 여부를 판정한다. **파괴적이면** 2단계 분리가 필요하다고 보고하고 사용자가 명시적으로 진행을 승인한 경우에만 계속한다. 비파괴면 머지 후 적용 안내 대상으로 기록한다(Phase 4에 전달).
8. 린트·초안 검사·테스트 실패 시 → 사용자에게 실패 내용을 보고하고 **중단**한다 (자동 수정하지 않는다). 원인 확인은 파일 읽기 도구로 최소한만 하고, 심층 진단하지 않는다.

## Phase 3: 이슈 & 브랜치

> Preflight에서 이슈 브랜치로 진입한 경우 이 Phase를 스킵한다.

1. **라벨 확인**: `gh label list --json name --jq '.[].name'`에 타입 표의 라벨이 없으면 `gh label create "<라벨>" --description "<PR 변경 타입 이름>" --color <색>`으로 만든다 (FE 레포와 같은 6개). 한 번 만들면 다음부터는 스킵된다.
2. **이슈 생성**: `gh issue create --title "[TYPE] 설명" --label "<라벨>" --body "<본문>"`. 본문: 해당 타입의 이슈 템플릿 `.github/ISSUE_TEMPLATE/<파일>.yml`은 GitHub 이슈 폼(form)이므로, 그 `body:` 각 항목의 `label`을 `### <label>` 섹션으로 매핑하고 `required: true` 항목을 빠짐없이 변경 내용으로 채워 전달한다 (폼 파일 자체를 본문으로 붙여넣지 않는다). 체크박스 항목은 `- [x] …`로 적는다. 선택 항목은 쓸 내용이 없으면 생략한다. 출력 URL 끝 숫자가 `<이슈번호>`.
3. **브랜치 생성**: `git checkout -b "<타입>/#<이슈번호>"` (변경사항은 워킹 트리에 그대로 유지된다).

## Phase 4: 배포

`automation/pipeline.md`를 읽고 Step 1부터 실행한다.
CI·PR 체크·Vercel 배포 대기 폴링은 `automation/pipeline.md`의 「폴링 실행 규칙」에 정의된 **현재 하네스의 폴링 모드**를 따른다 (진입 어댑터가 지정).

전달할 컨텍스트:
- 브랜치명, 타입, 이슈번호
- 변경사항 요약 (Phase 1에서 생성한 요약 또는 사용자 인자)
- Phase 2 검증 결과 (PR 본문 「테스트 방법」에 실제 수행한 것만 적는다)
- 계약 변경 여부 (있으면 — PR 「참고 사항」의 FE 대응 안내 대상)
- 마이그레이션 파일 목록 (있으면 — 최종 보고의 적용 안내 대상)

## 규칙

- Phase 1~3에서는 코드를 수정하지 않는다 — 이미 완료된 변경사항을 검증하는 것이 목적이다.
- 검증 실패 시 (Phase 2) 자동 수정하지 않고 사용자에게 보고한다.
- Phase 4 (배포)의 파이프라인 실패 처리는 `automation/pipeline.md`의 규칙을 따른다.
- 모든 `gh`/`git` 명령 실패 시 에러 내용을 사용자에게 보고한다.
- 이슈/PR 생성 시 GitHub 템플릿 형식을 준수한다. 라벨은 **이슈에만** 붙인다(타입 표) — PR에는 라벨·담당자를 붙이지 않는다.
- **민감 파일 커밋 금지**: 배포 과정의 `git add/commit/push`는 `automation/pipeline.md`의 「민감 파일 커밋 금지」 규칙을 따른다 (훅이 있는 하네스는 자동 차단이 추가되는 이중 방어).
