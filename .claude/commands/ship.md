# /ship — 코드 변경 배포 자동화 (Claude Code 어댑터)

이 명령은 하네스 중립 배포 워크플로우의 **Claude Code 진입점**이다. 절차 원본은 `automation/ship.md`(+ `automation/pipeline.md`)에 있다.

## 실행

`automation/ship.md`를 읽고 Preflight → Phase 1~4를 그대로 실행한다. Phase 4에서 `automation/pipeline.md`를 읽어 배포한다.
사용자 인자(예: `/ship feat 사실 카드 확정 API 추가`)는 `automation/ship.md`의 Phase 1 규칙대로 처리한다.

## Claude Code 하네스 설정 (원본 명세에 주입)

- **폴링 모드**: CI·PR 체크·Vercel 배포 대기는 `automation/bin/`의 폴링 스크립트(`pr_gate.py`·`run_wait.py`·`deploy_wait.py`)를 가상환경 python으로 **`run_in_background: true` Bash**에서 실행한다. 포그라운드 `sleep` 차단·Monitor 도구 금지. 메인 세션이 직접 수행하고 서브에이전트에 위임하지 않는다. 스크립트 완료 시 세션이 자동 재호출된다. (`automation/pipeline.md` 「폴링 실행 규칙」의 Claude Code 모드)
- **Co-Author 트레일러**: `automation/pipeline.md` Step 1-1 커밋 메시지 끝에 현재 세션의 시스템 규칙이 지정하는 `Co-Authored-By:` 트레일러를 붙인다.
- **PR 본문 서명**: Step 1-4 PR 본문 끝에 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`를 붙인다.
- **안전 규칙**: 「민감 파일 커밋 금지」는 커밋 전 `automation/bin/sensitive_gate.py` 실행으로 기계 검사하고, PreToolUse 훅(`.claude/settings.json` → `automation/hooks/validate-git-sensitive.sh`)이 `git add/commit/push`를 추가로 자동 차단한다 — 이중 방어로 작동한다.
