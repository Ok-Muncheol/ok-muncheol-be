"""PR 게이트(automation/bin/pr_gate.py)를 gh 없이 확인한다.

판정은 fixture JSON으로, 폴링은 gh 대역으로 본다.
"""

import json
import subprocess

import pytest

from automation.bin import pr_gate

ANCHORS = ["check", "gitleaks"]
API_ERROR = subprocess.CalledProcessError(1, ["gh"])


def run(name: str, conclusion: str | None = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def commit_status(context: str, state: str) -> dict:
    return {"__typename": "StatusContext", "context": context, "state": state}


def view(*checks: dict, mergeable: str = "MERGEABLE") -> dict:
    return {"mergeable": mergeable, "statusCheckRollup": list(checks)}


def last_line(capsys) -> str:
    return capsys.readouterr().out.splitlines()[-1]


@pytest.mark.parametrize(
    ("pr", "anchors", "want"),
    [
        pytest.param(view(run("check"), run("gitleaks")), ANCHORS, "PASS", id="all-pass"),
        pytest.param(
            view(run("check", "FAILURE"), run("gitleaks")),
            ANCHORS,
            "FAIL failed=check mergeable=MERGEABLE",
            id="anchor-failed",
        ),
        # 앵커가 아닌 체크도 게이트 대상이다 (전수 검사)
        pytest.param(
            view(run("check"), run("gitleaks"), run("e2e", "FAILURE")),
            ANCHORS,
            "FAIL failed=e2e mergeable=MERGEABLE",
            id="non-anchor-failed",
        ),
        # 앵커가 아직 등록되지 않았으면 통과가 아니다 (vacuous pass 방지)
        pytest.param(
            view(run("check")),
            ANCHORS,
            "PENDING anchors_absent=gitleaks pending= mergeable=MERGEABLE",
            id="anchor-absent",
        ),
        pytest.param(
            view(run("check"), run("gitleaks", None, "IN_PROGRESS")),
            ANCHORS,
            "PENDING anchors_absent= pending=gitleaks mergeable=MERGEABLE",
            id="in-progress",
        ),
        pytest.param(
            view(run("check"), run("gitleaks"), mergeable="CONFLICTING"),
            ANCHORS,
            "FAIL failed= mergeable=CONFLICTING",
            id="conflicting",
        ),
        pytest.param(
            view(),
            ["check"],
            "PENDING anchors_absent=check pending= mergeable=MERGEABLE",
            id="no-checks-with-anchor",
        ),
        # commit status(StatusContext)와 NEUTRAL·SKIPPED도 같은 규칙으로 정규화한다
        pytest.param(
            view(run("check"), run("gitleaks", "SKIPPED"), commit_status("deploy", "SUCCESS")),
            ANCHORS,
            "PASS",
            id="skipped-and-status-pass",
        ),
        pytest.param(
            view(run("check"), run("gitleaks"), commit_status("deploy", "EXPECTED")),
            ANCHORS,
            "PENDING anchors_absent= pending=deploy mergeable=MERGEABLE",
            id="status-pending",
        ),
        pytest.param(
            view(run("check"), run("gitleaks"), commit_status("deploy", "ERROR")),
            ANCHORS,
            "FAIL failed=deploy mergeable=MERGEABLE",
            id="status-error",
        ),
        # 앵커 없음 모드 (CI 없는 레포) — 체크 존재를 요구하지 않는다
        pytest.param(view(), [], "PASS", id="no-anchor-no-checks"),
        pytest.param(
            {"mergeable": "UNKNOWN", "statusCheckRollup": None},
            [],
            "PENDING anchors_absent= pending= mergeable=UNKNOWN",
            id="no-anchor-unknown",
        ),
        pytest.param(
            view(run("x", "FAILURE")),
            [],
            "FAIL failed=x mergeable=MERGEABLE",
            id="no-anchor-failed",
        ),
    ],
)
def test_judge(pr, anchors, want):
    assert pr_gate.judge(pr, anchors) == want


def test_main_waits_until_gate_passes(fake_gh, capsys):
    calls = fake_gh(
        pr_gate,
        json.dumps(view(run("check", None, "QUEUED"), run("gitleaks"))),
        json.dumps(view(run("check"), run("gitleaks"))),
    )

    assert pr_gate.main(["12", "600", *ANCHORS]) == 0
    assert last_line(capsys) == "PR_GATE_DONE result=PASS"
    assert calls == [("pr", "view", "12", "--json", "mergeable,statusCheckRollup")] * 2


def test_main_stops_on_failed_check(fake_gh, capsys):
    fake_gh(pr_gate, json.dumps(view(run("check", "FAILURE"), run("gitleaks"))))

    assert pr_gate.main(["12", "600", *ANCHORS]) == 1
    assert last_line(capsys) == "PR_GATE_DONE result=FAIL failed=check mergeable=MERGEABLE"


def test_main_times_out_while_pending(fake_gh, capsys):
    fake_gh(pr_gate, json.dumps(view(run("check"))))

    assert pr_gate.main(["12", "0", *ANCHORS]) == 1
    assert last_line(capsys) == (
        "PR_GATE_DONE result=TIMEOUT PENDING anchors_absent=gitleaks pending= mergeable=MERGEABLE"
    )


def test_main_reports_api_error_after_three_failures(fake_gh, capsys):
    fake_gh(pr_gate, API_ERROR, API_ERROR, API_ERROR)

    assert pr_gate.main(["12", "600", *ANCHORS]) == 1
    assert last_line(capsys) == "PR_GATE_DONE result=API_ERROR"


def test_main_counts_only_consecutive_api_errors(fake_gh):
    pending = json.dumps(view(run("check")))
    passed = json.dumps(view(run("check"), run("gitleaks")))
    fake_gh(pr_gate, API_ERROR, API_ERROR, pending, API_ERROR, API_ERROR, passed)

    assert pr_gate.main(["12", "600", *ANCHORS]) == 0


# 판정 입력이 이상하면 TIMEOUT까지 기다리지 않고 바로 ERROR로 끝난다 (fail-closed)
@pytest.mark.parametrize("response", ["", "not json", "[]", json.dumps({"statusCheckRollup": []})])
def test_main_fails_closed_on_malformed_output(fake_gh, capsys, response):
    fake_gh(pr_gate, response)

    assert pr_gate.main(["12", "600", *ANCHORS]) == 1
    assert last_line(capsys) == "PR_GATE_DONE result=ERROR"


def test_main_fails_closed_without_gh(fake_gh, capsys):
    fake_gh(pr_gate, FileNotFoundError("gh"))

    assert pr_gate.main(["12", "600", *ANCHORS]) == 1
    assert last_line(capsys) == "PR_GATE_DONE result=ERROR"


@pytest.mark.parametrize("argv", [["12"], ["12", "ten", "check"]])
def test_main_rejects_bad_arguments(capsys, argv):
    assert pr_gate.main(argv) == 2
    assert "usage" in capsys.readouterr().err
