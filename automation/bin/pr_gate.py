"""PR 게이트 폴링 — PR의 체크가 전부 성공하고 머지 가능해질 때까지 대기한다 (30초 간격).

게이트 = mergeable MERGEABLE
       + 앵커 체크 존재 (체크 등록 전 공집합 통과(vacuous pass) 방지)
       + 비차단 예외를 제외한 모든 체크가 완료·성공 (실패 0, 대기 0, 체크 1개 이상)
체크를 이름 허용목록으로 고르지 않는다 — 잡이 추가·개명돼도 게이트가 자동으로 따라간다.
앵커를 하나도 주지 않으면 「CI가 없는 레포」 모드다 — 체크 존재를 요구하지 않고 mergeable만
본다 (워크플로우가 없는 레포용. 이 레포는 CI가 있으므로 반드시 앵커 check·gitleaks를 넘긴다).
판정 로직·비차단 예외 목록의 원천은 judge()·EXEMPT다 (tests/automation/test_pr_gate.py가
fixture로 검증한다).

표준 라이브러리와 gh만 쓴다 — bash·jq가 없는 Windows(PowerShell)에서도 같은 명령으로 돈다.

사용법: <가상환경 python> automation/bin/pr_gate.py <PR번호> <타임아웃초> [앵커 체크명...]
  CI 있음: pr_gate.py <번호> 600 check
  CI 없음: pr_gate.py <번호> 180
종료 마커: PR_GATE_DONE result=PASS|FAIL...|TIMEOUT...|API_ERROR|ERROR
  ERROR = gh 부재·판정 입력 이상·예상 못 한 예외 (fail-closed — TIMEOUT까지 기다리지 않고
  즉시 종료)
"""

import json
import subprocess
import sys
import time
import traceback

INTERVAL = 30
MAX_API_ERRORS = 3

# 비차단 예외 체크 — 게이트에서 제외할 이름. 현재 없음: 생기면 여기에만 추가한다.
EXEMPT: frozenset[str] = frozenset()


def check_name(check: dict) -> str:
    # CheckRun은 name, commit status(StatusContext)는 context에 이름이 있다
    return check.get("name") or check.get("context") or ""


def check_state(check: dict) -> str:
    """CheckRun(.conclusion)과 commit status(.state)를 PASS/PENDING/FAIL로 정규화한다.

    NEUTRAL·SKIPPED는 통과다.
    """
    if check.get("__typename") == "StatusContext":
        state = check.get("state")
        if state == "SUCCESS":
            return "PASS"
        return "PENDING" if state in ("PENDING", "EXPECTED") else "FAIL"
    if check.get("status") != "COMPLETED":
        return "PENDING"
    return "PASS" if check.get("conclusion") in ("SUCCESS", "NEUTRAL", "SKIPPED") else "FAIL"


def judge(view: dict, anchors: list[str]) -> str:
    """`gh pr view --json mergeable,statusCheckRollup` 출력을 판정해 한 줄로 돌려준다.

    결과는 "PASS" | "FAIL …" | "PENDING …"이다. 앵커가 하나라도 있으면 앵커 존재 + 체크
    1개 이상 + 전수 성공이 필수다. 앵커가 없으면(CI 없는 레포) 체크 존재를 요구하지 않고
    mergeable + (있다면) 전수 성공만 본다.
    """
    mergeable = view["mergeable"]
    # 게이트 대상 = 비차단 예외를 뺀 모든 체크. 이름을 고르는 게 아니라 실패 0을 요구한다.
    checks = [c for c in view["statusCheckRollup"] or [] if check_name(c) not in EXEMPT]
    failed = [check_name(c) for c in checks if check_state(c) == "FAIL"]
    pending = [check_name(c) for c in checks if check_state(c) == "PENDING"]
    # 앵커 체크는 존재 자체가 필수 — 체크 등록 전 공집합 통과(vacuous pass)를 막는다.
    names = {check_name(c) for c in checks}
    absent = [a for a in anchors if a not in names]
    if failed or mergeable == "CONFLICTING":
        return f"FAIL failed={','.join(failed)} mergeable={mergeable}"
    if mergeable == "MERGEABLE" and not absent and not pending and (checks or not anchors):
        return "PASS"
    return (
        f"PENDING anchors_absent={','.join(absent)} pending={','.join(pending)} "
        f"mergeable={mergeable}"
    )


def gh(*args: str) -> str:
    # stderr는 가로채지 않는다 — 로그에 남아야 API_ERROR를 보고할 때 실제 원인을 요약할 수 있다.
    return subprocess.run(
        ["gh", *args], check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8"
    ).stdout


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].isdecimal():
        print("usage: pr_gate.py <PR번호> <타임아웃초> [앵커 체크명...]", file=sys.stderr)
        return 2
    pr, limit, anchors = argv[0], int(argv[1]), argv[2:]
    deadline = time.monotonic() + limit
    api_errors = 0
    while True:
        # GitHub 조회 실패를 pending으로 삼키지 않는다 — 3회 연속 실패 시 API_ERROR로 종료.
        try:
            out = gh("pr", "view", pr, "--json", "mergeable,statusCheckRollup")
        except FileNotFoundError:
            print("gh를 찾을 수 없다 — PR 상태 조회에 필요하다")
            print("PR_GATE_DONE result=ERROR")
            return 1
        except subprocess.CalledProcessError:
            api_errors += 1
            print(f"GitHub API query failed ({api_errors}/{MAX_API_ERRORS})")
            if api_errors >= MAX_API_ERRORS:
                print("PR_GATE_DONE result=API_ERROR")
                return 1
            time.sleep(INTERVAL)
            continue
        api_errors = 0
        try:
            result = judge(json.loads(out), anchors)
        except (ValueError, KeyError, TypeError, AttributeError):
            # 판정 입력이 비었거나 형식이 다르면 조용히 TIMEOUT까지 돌지 않고 즉시 끝낸다.
            print(f"판정 입력이 비었거나 형식이 다르다: {out[:200]!r}")
            print("PR_GATE_DONE result=ERROR")
            return 1
        if result == "PASS":
            print("PR_GATE_DONE result=PASS")
            return 0
        if result.startswith("FAIL"):
            print(f"PR_GATE_DONE result={result}")
            return 1
        if time.monotonic() >= deadline:
            print(f"PR_GATE_DONE result=TIMEOUT {result}")
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
        print("PR_GATE_DONE result=ERROR")
        sys.exit(1)
