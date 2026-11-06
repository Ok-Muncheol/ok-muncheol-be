"""민감 파일 커밋 게이트 — 스테이징된 파일에 비밀·키·로컬 설정·비공개 데이터가 없는지 검사한다.

반드시 git add(스테이징) 후에 실행한다 — add 이전 검사로는 untracked였던 민감 파일이 잡히지 않는다.
이 정규식이 민감 파일 패턴의 단일 원천이다. automation/pipeline.md 「민감 파일 커밋 금지」의
목록과 automation/hooks/validate-git-sensitive.sh(Claude Code·Codex 훅 이중 방어)의 배열은
이 정규식을 따라간다 — 패턴 변경은 여기서 하고 둘을 같이 갱신한다.
게이트는 fail-closed다 — git 조회가 실패하면 통과가 아니라 ERROR로 종료한다.

표준 라이브러리와 git만 쓴다 — bash가 없는 Windows(PowerShell)에서도 같은 명령으로 돈다.

사용법: <가상환경 python> automation/bin/sensitive_gate.py
종료 마커: SENSITIVE_GATE result=PASS|BLOCKED|ERROR
  BLOCKED(종료코드 1)면 출력된 파일을 git reset HEAD <파일>로 해제하고 사용자에게 보고한다.
"""

import re
import subprocess
import sys
import traceback

SENSITIVE = re.compile(
    r"(^|/)\.env(\..+)?$"
    r"|(^|/)\.claude/settings\.local\.json$"
    r"|(^|/)\.mcp\.json$"
    # vercel link 산출물(프로젝트·조직 ID)
    r"|(^|/)\.vercel/"
    r"|(^|/)certs/"
    # 비공개 데이터 폴더(.gitignore) — AI Hub 원본 영상·실제 사건·이용조건 미확인 원문·가중치를 둔다
    r"|(^|/)data/private/"
    r"|firebase-adminsdk[^/]*\.json$"
    r"|\.(pem|p12|p8|key)$"
    r"|(^|/)id_rsa[^/]*$"
)


def blocked(paths: list[str]) -> list[str]:
    # *.example은 플레이스홀더 템플릿이라 커밋 허용 (.gitignore의 !.env.example과 정합)
    return [p for p in paths if SENSITIVE.search(p) and not p.endswith(".example")]


def main() -> int:
    # -z로 받는다 — 기본 출력은 한글 경로를 "\353\271…"처럼 따옴표로 감싸 정규식을 빠져나간다.
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        print("SENSITIVE_GATE result=ERROR")
        return 1
    matches = blocked([p for p in out.split("\0") if p])
    if matches:
        print("차단된 민감 파일:")
        for path in matches:
            print(f"  {path}")
        print("SENSITIVE_GATE result=BLOCKED")
        return 1
    print("SENSITIVE_GATE result=PASS")
    return 0


if __name__ == "__main__":
    # 콘솔 코드 페이지에 없는 문자(경로)에서 죽지 않는다.
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    try:
        sys.exit(main())
    except Exception:
        # 예상 못 한 예외도 마커 없이 끝나지 않는다 (fail-closed).
        traceback.print_exc()
        print("SENSITIVE_GATE result=ERROR")
        sys.exit(1)
