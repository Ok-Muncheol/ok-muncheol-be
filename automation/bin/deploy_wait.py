"""Vercel 프로덕션 배포 대기 — main 머지 커밋의 커밋 상태(context `Vercel`)를 폴링하고,
배포가 끝나면 (넘겼으면) 프로덕션 health URL까지 확인한다 (30초 간격, 최대 10분).

Vercel Git 연동은 배포 결과를 커밋 상태로 남긴다 — pending "Vercel is deploying your app"
→ success "Deployment has completed". GitHub Deployment는 보지 않는다: Vercel 프로젝트 설정에
따라 생기지 않을 수 있다(lumi BE·FE 0건, 2026-10-27 실측). 상태는 컨텍스트 `Vercel`의
최신 1건으로 판정한다 — 같은 컨텍스트에 새 상태가 오면 그것이 현재 값이다.
판정은 FE의 `automation/bin/deploy-wait.mjs`와 같다.

연결 여부도 여기서 판정한다. 머지 커밋에 `Vercel` 상태가 GRACE 안에 생기지 않으면
직전 main 커밋(첫 부모)으로 가른다 — 거기엔 있으면 NOT_FOUND(연동은 살아 있는데 이 커밋이
배포되지 않았다), 거기도 없으면 NOT_CONNECTED(Vercel 미연결 — 확인할 배포가 없으므로
종료코드 0). Vercel은 push 직후 pending을 남기므로, main CI를 기다린 뒤(pipeline.md
Step 2) 도는 이 스크립트에서 2분이면 넉넉하다. 배포를 연결하기 전(README 「기술 구성」)에는
NOT_CONNECTED로 끝난다.

"Deployment has completed"는 빌드·배포까지만 말해 준다. health URL을 넘기면 배포가
success일 때 그 주소를 GET해 200인지 본다. 콜드 스타트를 고려해 HEALTH_ATTEMPTS번까지
30초 간격으로 다시 묻는다. 서버와 운영 URL이 생기면 pipeline.md Step 3에 health URL을 적는다.

표준 라이브러리와 gh만 쓴다 — bash·jq가 없는 Windows(PowerShell)에서도 같은 명령으로 돈다.

사용법: <가상환경 python> automation/bin/deploy_wait.py <머지커밋SHA> [<health URL>]
  예: deploy_wait.py "$MERGE_SHA"
      deploy_wait.py "$MERGE_SHA" https://<운영 주소>/health
종료 마커: DEPLOY_RESULT result=<결과>
  결과 = success|failure|error|UNHEALTHY|NOT_CONNECTED|NOT_FOUND|TIMEOUT|API_ERROR|ERROR
  상태를 봤으면 url=<Vercel 배포 페이지>, health를 물었으면 health=<HTTP 코드|unreachable>가
  함께 찍힌다 — failure·TIMEOUT이면 url에서 배포 로그를, UNHEALTHY면 마커 앞 줄의 응답을 본다.
  소문자 error는 Vercel 배포 상태다.
  대문자 ERROR = gh 부재·응답 형식 이상·예상 못 한 예외 (fail-closed).
"""

import json
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

CONTEXT = "Vercel"
INTERVAL = 30
LIMIT = 600
GRACE = 120
MAX_API_ERRORS = 3
HEALTH_ATTEMPTS = 3
HEALTH_TIMEOUT = 20
# 종료 상태별 종료코드. 그 밖의 상태는 pending(진행 중)뿐이다
DONE = {"success": 0, "failure": 1, "error": 1}


def gh(*args: str) -> str:
    # stderr는 가로채지 않는다 — 로그에 남아야 API_ERROR를 보고할 때 실제 원인을 요약할 수 있다.
    return subprocess.run(
        ["gh", *args], check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8"
    ).stdout


def statuses(ref: str) -> str:
    # {owner}/{repo}는 gh가 현재 체크아웃의 레포로 채운다.
    return gh("api", f"repos/{{owner}}/{{repo}}/commits/{ref}/statuses?per_page=100")


def vercel_status(out: str) -> tuple[str, str] | None:
    """커밋 상태 목록(최신순)에서 `Vercel`의 최신 상태를 (state, target_url)로. 없으면 None."""
    items = json.loads(out)
    if not isinstance(items, list):
        raise TypeError("statuses 응답이 배열이 아니다")
    for item in items:
        if item["context"] == CONTEXT:
            if item["state"] not in DONE and item["state"] != "pending":
                raise ValueError(f"알 수 없는 커밋 상태 {item['state']}")
            return item["state"], item["target_url"]
    return None


def first_parent(out: str) -> str | None:
    """`gh api …/git/commits/<sha>` 응답의 첫 부모 SHA. 부모가 없으면(루트 커밋) None."""
    parents = json.loads(out)["parents"]
    if not isinstance(parents, list):
        raise TypeError("parents가 배열이 아니다")
    return parents[0]["sha"] if parents else None


def probe(url: str) -> tuple[int | None, str]:
    """GET url → (HTTP 코드, 응답 본문 앞부분). 연결하지 못하면 (None, 사유)."""
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as response:
            return response.status, response.read(300).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(300).decode("utf-8", "replace")
    except OSError as exc:  # URLError(DNS·연결 거부)와 타임아웃
        return None, str(exc)


def finish(result: str, url: str | None, code: int, health: int | str | None = None) -> int:
    marker = f"DEPLOY_RESULT result={result}"
    if url:
        marker += f" url={url}"
    if health is not None:
        marker += f" health={health}"
    print(marker)
    return code


def check_health(health_url: str, url: str) -> int:
    """배포가 끝난 프로덕션의 health URL이 200인지 본다. 콜드 스타트를 고려해 몇 번 다시 묻는다."""
    for attempt in range(1, HEALTH_ATTEMPTS + 1):
        code, body = probe(health_url)
        if code == 200:
            return finish("success", url, 0, health=code)
        print(f"health {code or 'unreachable'} ({attempt}/{HEALTH_ATTEMPTS}): {body}")
        if attempt < HEALTH_ATTEMPTS:
            time.sleep(INTERVAL)
    return finish("UNHEALTHY", url, 1, health=code or "unreachable")


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print("usage: deploy_wait.py <머지커밋SHA> [<health URL>]", file=sys.stderr)
        return 2
    sha = argv[0]
    health_url = argv[1] if len(argv) == 2 else None
    start = time.monotonic()
    deadline, grace_end = start + LIMIT, start + GRACE
    api_errors = 0
    status = None
    # 직전 main 커밋에 `Vercel` 상태가 있었나 — 머지 커밋에 상태가 없을 때 한 번만 본다
    parent_deployed = None
    while True:
        queried = False
        try:
            status = vercel_status(statuses(sha))
            if status is None and parent_deployed is None:
                parent = first_parent(gh("api", f"repos/{{owner}}/{{repo}}/git/commits/{sha}"))
                parent_deployed = parent is not None and vercel_status(statuses(parent)) is not None
            api_errors = 0
            queried = True
        except FileNotFoundError:
            print("gh를 찾을 수 없다 — 커밋 상태 조회에 필요하다")
            print("DEPLOY_RESULT result=ERROR")
            return 1
        except subprocess.CalledProcessError:
            # GitHub 조회 실패를 pending으로 삼키지 않는다 — 3회 연속 실패 시 API_ERROR로 종료.
            api_errors += 1
            print(f"GitHub API query failed ({api_errors}/{MAX_API_ERRORS})")
            if api_errors >= MAX_API_ERRORS:
                print("DEPLOY_RESULT result=API_ERROR")
                return 1
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            # 형식이 다르면 조용히 TIMEOUT까지 돌지 않고 즉시 끝낸다.
            print(f"gh 응답 형식이 다르다: {exc!r}")
            print("DEPLOY_RESULT result=ERROR")
            return 1
        if queried and status is not None and status[0] in DONE:
            state, url = status
            if state == "success" and health_url is not None:
                return check_health(health_url, url)
            return finish(state, url, DONE[state])
        if queried and status is None and time.monotonic() >= grace_end:
            if parent_deployed:
                return finish("NOT_FOUND", None, 1)
            return finish("NOT_CONNECTED", None, 0)
        if time.monotonic() >= deadline:
            return finish("TIMEOUT", status[1] if status else None, 1)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    # 로그 파일로 리다이렉트돼도 줄마다 내보내고, 콘솔 코드 페이지에 없는 문자에서 죽지 않는다.
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # 예상 못 한 예외도 마커 없이 끝나지 않는다 (fail-closed).
        traceback.print_exc()
        print("DEPLOY_RESULT result=ERROR")
        sys.exit(1)
