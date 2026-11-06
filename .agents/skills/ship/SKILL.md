---
name: ship
description: ok-muncheol-be의 워킹 트리 변경을 검증(make lint·make check·make test)하고 이슈 생성 → <타입>/#<이슈> 브랜치 → main PR → CI 게이트 → squash 머지 → main CI 확인 → (Vercel 연결 시) 프로덕션 배포 확인까지 자동화한다 (하네스 중립 워크플로우 automation/ship.md의 Codex 어댑터). 사용자가 "ship", "$ship", "배포해줘", "PR 올려서 머지까지 해줘" 등 코드 수정이 끝난 변경을 main에 올리라고 하면 사용한다. 코드 수정·버그 분석 요청에는 쓰지 않는다. Codex에는 사용자 정의 슬래시 커맨드가 없어 "/ship"은 동작하지 않는다 — $ship 멘션이나 평문으로 부른다.
---

# ship (Codex 어댑터)

하네스 중립 배포 워크플로우의 **Codex 진입점**이다. 절차 원본은 리포의 `automation/ship.md`(+ `automation/pipeline.md`)에 있다. 그 파일들을 읽고 그대로 실행하되, 아래 Codex 전용 오버라이드를 적용한다.

## 실행 절차

1. `automation/ship.md`를 읽고 Preflight → Phase 1~4를 순서대로 수행한다. Phase 4에서 `automation/pipeline.md`를 읽어 배포한다.
2. 사용자 인자(타입+설명, 예: `$ship feat 사실 카드 확정 API 추가`)는 `automation/ship.md`의 Phase 1 규칙대로 처리한다.
3. 전 과정을 이 세션이 직접 순차 수행한다 — 서브에이전트(`spawn_agent`)에 폴링을 위임하지 않는다.
4. Windows 네이티브(PowerShell)에서 돌고 있으면 아래 「Windows 네이티브」를 함께 적용한다.

## Codex Co-Author 트레일러 · PR 서명 (`automation/pipeline.md` 하네스별 값)

- 커밋 메시지 끝: **`Co-Authored-By: Codex <noreply@openai.com>`** (다른 하네스의 트레일러를 쓰지 않는다)
- PR 본문 서명: **없음**

## Codex 폴링 모드 (`automation/pipeline.md` 「폴링 실행 규칙」의 Codex 모드)

- 배경 재호출 프리미티브가 없으므로 CI·PR 체크·Vercel 배포 폴링은 `automation/bin/`의 폴링 스크립트(`pr_gate.py`·`run_wait.py`·`deploy_wait.py`)를 가상환경 python으로 **포그라운드 블로킹 호출**로 **한 번의 셸 호출**로 완료까지 실행한다 — 스크립트가 종료 마커를 출력하고 반환할 때까지 그 호출이 블로킹된다.
- 스크립트 출력은 파일로 리다이렉트(`> "$LOG" 2>&1`)하고 마커를 `tail`로 읽는다. 스크립트 내부 타임아웃을 상한으로 삼고, **셸 명령 타임아웃은 그보다 넉넉히** 설정한다 (예: 게이트 10분 → 셸 15분+, 배포 대기는 health 재시도까지 최대 약 12분 → 셸 15분+). 없으면 스크립트가 셸 타임아웃에 먼저 끊겨 미완료로 오판될 수 있다.
- 폴링 스크립트는 `gh`를 반복 호출하므로 **샌드박스에 네트워크가 열려 있어야 한다** — `~/.codex/config.toml`(Windows는 `%USERPROFILE%\.codex\config.toml`)의 `[sandbox_workspace_write] network_access = true`. 닫혀 있으면 첫 호출이 `result=API_ERROR`로 끝난다: 이를 GitHub 장애로 추측하거나 `TIMEOUT`으로 바꾸지 말고, 설정을 안내하고 중단한다 (`approval_policy = "never"`에서는 승인 상승을 요청할 수 없다).
- 검증(Phase 2)의 `make` 명령은 폴링과 분리된 별도 셸 호출로 돌린다. 폴링 스크립트는 표준 라이브러리 Python에서 `gh`만 부른다.

## `.git` 쓰기 (workspace-write 샌드박스)

workspace-write 샌드박스는 `.git/` 쓰기를 기본 차단해 커밋·체크아웃·pull이 막힌다(원격·CI는 정상인데 로컬 `main`만 뒤처진다). 프로젝트 로컬 설정 `.codex/config.toml`의 `writable_roots`에 **이 체크아웃의 `.git` 절대경로**가 있어야 한다 — 파일이 없으면 `.codex/config.toml.example`을 복사해 경로를 맞춘다(머신마다 달라 `.gitignore` 대상). Windows 경로는 작은따옴표 TOML 리터럴로 적는다 — `writable_roots = ['C:\Users\<사용자>\…\ok-muncheol-be\.git']`(큰따옴표면 백슬래시를 `\\`로 이스케이프해야 한다). `.git` 쓰기 거부 에러가 보이면 이 설정을 안내하고 중단한다.

## 안전 규칙 (훅 + 직접 준수)

- `.codex/hooks.json`의 PreToolUse(`Bash`) 훅이 `automation/hooks/validate-git-sensitive.sh`로 `git add/commit/push`의 민감 파일을 자동 차단한다 — Claude Code와 같은 스크립트, 이중 방어다. 단 **프로젝트를 신뢰하고 `/hooks`에서 훅을 승인한 뒤에만** 동작한다(시작 시 "hooks need review" 경고가 보이면 미승인 상태). 훅 스크립트는 bash·jq로 돌아 Windows 네이티브(PowerShell)에서는 실행되지 않으므로 Windows에서는 승인하지 않는다(「Windows 네이티브」).
- 훅이 승인되지 않았거나 동작을 확신할 수 없으면 `automation/pipeline.md` 「민감 파일 커밋 금지」 규칙을 **직접** 준수한다 (Step 1의 `sensitive_gate.py` 기계 검사와 병행): `git add/commit/push` 전에 대상에 `.env`(및 `.env.*`, `.env.example` 제외), `.claude/settings.local.json`, `.mcp.json`, `.vercel/`, `certs/` 하위 키·인증서, `data/private/` 하위 비공개 데이터, `*.pem`·`*.p12`·`*.p8`·`*.key`, `firebase-adminsdk*.json`, `id_rsa*`가 없는지 확인하고, 있으면 커밋하지 말고 사용자에게 보고한다 (스테이징돼 있으면 `git reset HEAD <file>`).

## Windows 네이티브 (PowerShell)

Windows용 Codex는 명령을 PowerShell로 실행한다. WSL 없이 쓸 때는 `automation/ship.md` 「실행 환경」(가상환경 경로·PowerShell 문법)에 더해 아래를 따른다.

- 필요한 도구는 Windows용 git, gh(`gh auth login` 완료), Python 3.12 이상이다. bash·jq·make는 없어도 된다 — 없는 도구가 있으면 진행하지 말고 사용자에게 알린다.
- 폴링·게이트 스크립트는 같은 인자로 부른다: `.venv\Scripts\python.exe automation\bin\pr_gate.py <PR번호> 600 check gitleaks`. 로그 마커는 `Get-Content $LOG -Tail 5`로 읽는다.
- `make`가 없으면 Phase 2의 린트·초안 검사·테스트는 `.venv\Scripts\python.exe -m ruff check .`·`.venv\Scripts\python.exe scripts\check_scaffold.py`·`.venv\Scripts\python.exe -m pytest`로 돌린다 (`automation/ship.md` Phase 2).
- 훅(`.codex/hooks.json`)은 승인하지 않는다 — bash 스크립트라 PowerShell에서 실행되지 않는다. 민감 파일은 `sensitive_gate.py`와 「안전 규칙」의 직접 준수로 지킨다.
- `.codex/config.toml`의 `writable_roots`는 Windows 경로로 적는다 (「`.git` 쓰기」).
