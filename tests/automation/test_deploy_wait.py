"""Vercel 프로덕션 배포 대기(automation/bin/deploy_wait.py)를 gh · health 대역으로 확인한다."""

import io
import json
import subprocess
import urllib.error

import pytest

from automation.bin import deploy_wait

SHA = "abc123"
URL = "https://vercel.com/team/be/abc"
HEALTH = "https://be.example/health"
API_ERROR = subprocess.CalledProcessError(1, ["gh"])
NONE = "[]"
COMMIT = json.dumps({"parents": [{"sha": "parent1"}]})


def vercel(*states: str) -> str:
    """`Vercel` 커밋 상태 목록. GitHub는 최신순으로 준다."""
    return json.dumps([{"context": "Vercel", "state": s, "target_url": URL} for s in states])


def statuses_call(sha: str) -> tuple[str, str]:
    return ("api", f"repos/{{owner}}/{{repo}}/commits/{sha}/statuses?per_page=100")


def last_line(capsys) -> str:
    return capsys.readouterr().out.splitlines()[-1]


@pytest.fixture
def fake_probe(monkeypatch):
    """health 조회를 대역으로 바꾼다. (코드, 본문)을 차례로 돌려주고, 받은 URL 목록을 돌려준다."""

    def install(*responses):
        queue = list(responses)
        calls = []

        def probe(url):
            calls.append(url)
            return queue.pop(0)

        monkeypatch.setattr(deploy_wait, "probe", probe)
        return calls

    return install


def test_main_succeeds_when_latest_vercel_status_is_success(fake_gh, capsys):
    calls = fake_gh(deploy_wait, vercel("success", "pending"))

    assert deploy_wait.main([SHA]) == 0
    assert last_line(capsys) == f"DEPLOY_RESULT result=success url={URL}"
    assert calls == [statuses_call(SHA)]


def test_main_ignores_other_contexts(fake_gh, capsys):
    other = {"context": "ci", "state": "failure", "target_url": ""}
    fake_gh(deploy_wait, json.dumps([other, json.loads(vercel("success"))[0]]))

    assert deploy_wait.main([SHA]) == 0
    assert last_line(capsys) == f"DEPLOY_RESULT result=success url={URL}"


def test_main_waits_while_pending(fake_gh, capsys):
    fake_gh(deploy_wait, vercel("pending"), vercel("success", "pending"))

    assert deploy_wait.main([SHA]) == 0
    assert last_line(capsys) == f"DEPLOY_RESULT result=success url={URL}"


@pytest.mark.parametrize("state", ["failure", "error"])
def test_main_reports_failed_deployment_without_health(fake_gh, fake_probe, capsys, state):
    fake_gh(deploy_wait, vercel(state, "pending"))
    probes = fake_probe()

    assert deploy_wait.main([SHA, HEALTH]) == 1
    assert last_line(capsys) == f"DEPLOY_RESULT result={state} url={URL}"
    assert probes == []


def test_main_checks_parent_once_and_waits_for_status(fake_gh, capsys):
    calls = fake_gh(deploy_wait, NONE, COMMIT, vercel("success"), NONE, vercel("success"))

    assert deploy_wait.main([SHA]) == 0
    assert last_line(capsys) == f"DEPLOY_RESULT result=success url={URL}"
    assert calls == [
        statuses_call(SHA),
        ("api", "repos/{owner}/{repo}/git/commits/abc123"),
        statuses_call("parent1"),
        statuses_call(SHA),
        statuses_call(SHA),
    ]


# 머지 커밋에 상태가 끝내 안 생기면 직전 main 커밋으로 연동 여부를 가른다
@pytest.mark.parametrize(
    ("responses", "want", "code"),
    [
        ([NONE, COMMIT, vercel("success")], "NOT_FOUND", 1),  # 직전 커밋은 배포됨
        ([NONE, COMMIT, NONE], "NOT_CONNECTED", 0),  # 직전 커밋도 상태 없음
        ([NONE, json.dumps({"parents": []})], "NOT_CONNECTED", 0),  # 부모 없는 커밋
    ],
)
def test_main_tells_not_found_from_not_connected(
    fake_gh, fake_probe, monkeypatch, capsys, responses, want, code
):
    monkeypatch.setattr(deploy_wait, "GRACE", 0)
    fake_gh(deploy_wait, *responses)
    probes = fake_probe()

    assert deploy_wait.main([SHA, HEALTH]) == code
    assert last_line(capsys) == f"DEPLOY_RESULT result={want}"
    assert probes == []


def test_main_times_out_while_pending(fake_gh, monkeypatch, capsys):
    monkeypatch.setattr(deploy_wait, "LIMIT", 0)
    fake_gh(deploy_wait, vercel("pending"))

    assert deploy_wait.main([SHA]) == 1
    assert last_line(capsys) == f"DEPLOY_RESULT result=TIMEOUT url={URL}"


def test_main_continues_after_fewer_than_three_api_errors(fake_gh, capsys):
    fake_gh(deploy_wait, API_ERROR, API_ERROR, vercel("success"))

    assert deploy_wait.main([SHA]) == 0
    assert last_line(capsys) == f"DEPLOY_RESULT result=success url={URL}"


def test_main_reports_api_error_after_three_failures(fake_gh, capsys):
    fake_gh(deploy_wait, API_ERROR, API_ERROR, API_ERROR)

    assert deploy_wait.main([SHA]) == 1
    assert last_line(capsys) == "DEPLOY_RESULT result=API_ERROR"


@pytest.mark.parametrize(
    "responses",
    [
        ["not json"],
        [json.dumps({"message": "Not Found"})],
        [json.dumps([{"context": "Vercel", "state": "queued", "target_url": URL}])],
        [NONE, "{}"],
    ],
)
def test_main_fails_closed_on_malformed_output(fake_gh, capsys, responses):
    fake_gh(deploy_wait, *responses)

    assert deploy_wait.main([SHA]) == 1
    assert last_line(capsys) == "DEPLOY_RESULT result=ERROR"


def test_main_fails_closed_without_gh(fake_gh, capsys):
    fake_gh(deploy_wait, FileNotFoundError("gh"))

    assert deploy_wait.main([SHA]) == 1
    assert last_line(capsys) == "DEPLOY_RESULT result=ERROR"


@pytest.mark.parametrize("argv", [[], [SHA, HEALTH, "extra"]])
def test_main_rejects_bad_arguments(capsys, argv):
    assert deploy_wait.main(argv) == 2
    assert "usage" in capsys.readouterr().err


def test_main_checks_health_after_success(fake_gh, fake_probe, capsys):
    fake_gh(deploy_wait, vercel("success"))
    probes = fake_probe((200, '{"status":"ok"}'))

    assert deploy_wait.main([SHA, HEALTH]) == 0
    assert last_line(capsys) == f"DEPLOY_RESULT result=success url={URL} health=200"
    assert probes == [HEALTH]


def test_main_asks_health_again_after_cold_start(fake_gh, fake_probe, capsys):
    fake_gh(deploy_wait, vercel("success"))
    probes = fake_probe((503, "cold"), (200, "ok"))

    assert deploy_wait.main([SHA, HEALTH]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-2] == "health 503 (1/3): cold"
    assert out[-1] == f"DEPLOY_RESULT result=success url={URL} health=200"
    assert len(probes) == 2


@pytest.mark.parametrize(
    ("response", "want"), [((503, "db down"), "503"), ((None, "timed out"), "unreachable")]
)
def test_main_reports_unhealthy_after_three_attempts(fake_gh, fake_probe, capsys, response, want):
    fake_gh(deploy_wait, vercel("success"))
    probes = fake_probe(response, response, response)

    assert deploy_wait.main([SHA, HEALTH]) == 1
    assert last_line(capsys) == f"DEPLOY_RESULT result=UNHEALTHY url={URL} health={want}"
    assert len(probes) == 3


class FakeResponse:
    status = 200

    def __init__(self, body: bytes):
        self.body = body

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    ("outcome", "want"),
    [
        (FakeResponse(b'{"status":"ok"}'), (200, '{"status":"ok"}')),
        (
            urllib.error.HTTPError(HEALTH, 503, "Service Unavailable", {}, io.BytesIO(b"down")),
            (503, "down"),
        ),
        (urllib.error.URLError("no host"), (None, "<urlopen error no host>")),
        (TimeoutError("timed out"), (None, "timed out")),
    ],
)
def test_probe_maps_http_outcomes(monkeypatch, outcome, want):
    def urlopen(url, timeout):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(deploy_wait.urllib.request, "urlopen", urlopen)

    assert deploy_wait.probe(HEALTH) == want
