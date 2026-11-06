"""Actions 워크플로우 run 대기 — 특정 커밋이 트리거한 run의 완료를 폴링한다 (30초 간격).

main 머지 커밋의 CI(ci.yml) 대기에 쓴다. 배포 워크플로우가 생기면 같은 스크립트로 기다린다.
run이 한 번도 발견되지 않은 채 타임아웃되면 NOT_FOUND로 구분한다 — 워크플로우 미트리거
(트리거 조건·파일명 변경 등) 신호이므로, 진행 중일 수 있는 TIMEOUT과 다르게 처리한다.

표준 라이브러리와 gh만 쓴다 — bash·jq가 없는 Windows(PowerShell)에서도 같은 명령으로 돈다.

사용법: <가상환경 python> automation/bin/run_wait.py <워크플로우> <브랜치> <커밋SHA> <타임아웃초>
  예: run_wait.py ci.yml main "$MERGE_SHA" 600
종료 마커: RUN_RESULT result=success|<conclusion>|NOT_FOUND|TIMEOUT|API_ERROR|ERROR
  완료 시 run_id=<id>가 함께 찍힌다 — 실패면 gh run view <id> --log-failed로 로그를 요약한다.
  ERROR = gh 부재·run 목록 형식 이상·예상 못 한 예외 (fail-closed)
"""

import json
import subprocess
import sys
import time
import traceback

INTERVAL = 30
MAX_API_ERRORS = 3


def gh(*args: str) -> str:
    # stderr는 가로채지 않는다 — 로그에 남아야 API_ERROR를 보고할 때 실제 원인을 요약할 수 있다.
    return subprocess.run(
        ["gh", *args], check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8"
    ).stdout


def latest_run(out: str) -> tuple[str, str, int] | None:
    """`gh run list --json …` 출력의 첫 run을 (status, conclusion, id)로 꺼낸다. 없으면 None."""
    runs = json.loads(out)
    if not runs:
        return None
    run = runs[0]
    return run["status"], run["conclusion"] or "unknown", run["databaseId"]


def main(argv: list[str]) -> int:
    if len(argv) != 4 or not argv[3].isdecimal():
        print("usage: run_wait.py <워크플로우> <브랜치> <커밋SHA> <타임아웃초>", file=sys.stderr)
        return 2
    workflow, branch, sha, limit = argv[0], argv[1], argv[2], int(argv[3])
    deadline = time.monotonic() + limit
    api_errors = 0
    found = False
    while True:
        # GitHub 조회 실패를 pending으로 삼키지 않는다 — 3회 연속 실패 시 API_ERROR로 종료.
        try:
            out = gh(
                "run", "list", "--branch", branch, "--workflow", workflow, "--commit", sha,
                "--limit", "1", "--json", "status,conclusion,databaseId",
            )
        except FileNotFoundError:
            print("gh를 찾을 수 없다 — run 상태 조회에 필요하다")
            print("RUN_RESULT result=ERROR")
            return 1
        except subprocess.CalledProcessError:
            api_errors += 1
            print(f"GitHub API query failed ({api_errors}/{MAX_API_ERRORS})")
            if api_errors >= MAX_API_ERRORS:
                print("RUN_RESULT result=API_ERROR")
                return 1
            time.sleep(INTERVAL)
            continue
        api_errors = 0
        try:
            run = latest_run(out)
        except (ValueError, KeyError, TypeError):
            # 형식이 다르면 조용히 TIMEOUT까지 돌지 않고 즉시 끝낸다.
            print(f"run 목록 형식이 다르다: {out[:200]!r}")
            print("RUN_RESULT result=ERROR")
            return 1
        if run is not None:
            found = True
            status, conclusion, run_id = run
            if status == "completed":
                print(f"RUN_RESULT result={conclusion} run_id={run_id}")
                return 0 if conclusion == "success" else 1
        if time.monotonic() >= deadline:
            print(f"RUN_RESULT result={'TIMEOUT' if found else 'NOT_FOUND'}")
            return 1
        time.sleep(INTERVAL)


if __name__ == "__main__":
    # 로그 파일로 리다이렉트돼도 줄마다 내보내고, 콘솔 코드 페이지에 없는 문자에서 죽지 않는다.
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # 예상 못 한 예외도 마커 없이 끝나지 않는다 (fail-closed).
        traceback.print_exc()
        print("RUN_RESULT result=ERROR")
        sys.exit(1)
