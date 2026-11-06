"""main 머지 커밋의 CI run 대기(automation/bin/run_wait.py)를 gh 대역으로 확인한다."""

import json
import subprocess

import pytest

from automation.bin import run_wait

ARGS = ["ci.yml", "main", "abc123", "600"]
API_ERROR = subprocess.CalledProcessError(1, ["gh"])


def runs(status: str | None = None, conclusion: str = "") -> str:
    """`gh run list --json status,conclusion,databaseId` 출력. status가 없으면 run이 아직 없다."""
    if status is None:
        return "[]"
    return json.dumps([{"status": status, "conclusion": conclusion, "databaseId": 7}])


def last_line(capsys) -> str:
    return capsys.readouterr().out.splitlines()[-1]


def test_main_waits_until_run_succeeds(fake_gh, capsys):
    calls = fake_gh(run_wait, runs(), runs("in_progress"), runs("completed", "success"))

    assert run_wait.main(ARGS) == 0
    assert last_line(capsys) == "RUN_RESULT result=success run_id=7"
    assert calls[0] == (
        "run",
        "list",
        "--branch",
        "main",
        "--workflow",
        "ci.yml",
        "--commit",
        "abc123",
        "--limit",
        "1",
        "--json",
        "status,conclusion,databaseId",
    )


def test_main_reports_failed_conclusion(fake_gh, capsys):
    fake_gh(run_wait, runs("completed", "failure"))

    assert run_wait.main(ARGS) == 1
    assert last_line(capsys) == "RUN_RESULT result=failure run_id=7"


# run이 한 번도 안 보였으면 미트리거(NOT_FOUND), 보였는데 안 끝났으면 TIMEOUT이다
@pytest.mark.parametrize(
    ("response", "want"),
    [(runs(), "RUN_RESULT result=NOT_FOUND"), (runs("in_progress"), "RUN_RESULT result=TIMEOUT")],
)
def test_main_distinguishes_not_found_from_timeout(fake_gh, capsys, response, want):
    fake_gh(run_wait, response)

    assert run_wait.main(["ci.yml", "main", "abc123", "0"]) == 1
    assert last_line(capsys) == want


def test_main_reports_api_error_after_three_failures(fake_gh, capsys):
    fake_gh(run_wait, API_ERROR, API_ERROR, API_ERROR)

    assert run_wait.main(ARGS) == 1
    assert last_line(capsys) == "RUN_RESULT result=API_ERROR"


@pytest.mark.parametrize(
    "response",
    ["", "not json", json.dumps({"runs": []}), json.dumps([{"status": "completed"}])],
)
def test_main_fails_closed_on_malformed_output(fake_gh, capsys, response):
    fake_gh(run_wait, response)

    assert run_wait.main(ARGS) == 1
    assert last_line(capsys) == "RUN_RESULT result=ERROR"


def test_main_fails_closed_without_gh(fake_gh, capsys):
    fake_gh(run_wait, FileNotFoundError("gh"))

    assert run_wait.main(ARGS) == 1
    assert last_line(capsys) == "RUN_RESULT result=ERROR"


@pytest.mark.parametrize("argv", [["ci.yml", "main"], ["ci.yml", "main", "abc123", "soon"]])
def test_main_rejects_bad_arguments(capsys, argv):
    assert run_wait.main(argv) == 2
    assert "usage" in capsys.readouterr().err
