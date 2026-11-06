"""민감 파일 커밋 차단을 확인한다 — 파이프라인 게이트(sensitive_gate.py)와 하네스 훅.

스크래치 리포에서 돌려 사용자 리포의 인덱스·워킹트리와 전역 git 설정을 건드리지 않는다.
훅(validate-git-sensitive.sh)에는 Claude Code·Codex가 주는 stdin JSON을 그대로 흘린다.
훅은 bash·jq로 도는 macOS·Linux 하네스용이라 Windows와 bash·jq가 없는 환경에서는 건너뛴다.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from automation.bin import sensitive_gate

HOOK = Path(__file__).resolve().parents[2] / "automation" / "hooks" / "validate-git-sensitive.sh"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or not (shutil.which("bash") and shutil.which("jq")),
    reason="훅은 bash·jq로 도는 macOS·Linux 하네스용이다",
)


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)
    git("init", "-q")
    (tmp_path / "note.txt").write_text("hi\n")
    git("add", "note.txt")
    git("-c", "user.email=smoke@test", "-c", "user.name=smoke", "commit", "-qm", "init")
    return tmp_path


def stage(repo: Path, path: str) -> None:
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("secret\n")
    git("add", "-f", path)


def gate(capsys) -> tuple[int, list[str]]:
    code = sensitive_gate.main()
    return code, capsys.readouterr().out.splitlines()


def test_gate_passes_without_sensitive_files(repo, capsys):
    stage(repo, "app.py")

    assert gate(capsys) == (0, ["SENSITIVE_GATE result=PASS"])


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "deploy/.env.prod",
        "AuthKey_test.p8",
        ".claude/settings.local.json",
        ".mcp.json",
        ".vercel/project.json",
        "certs/dev.crt",
        "data/private/aihub/case-001.mp4",
        "firebase-adminsdk-abc.json",
        "keys/id_rsa",
        # 한글 경로도 그대로 받는다 — git 기본 출력은 경로를 따옴표·8진수로 바꿔 정규식을 빠져나간다
        "비밀/.env",
    ],
)
def test_gate_blocks_staged_sensitive_file(repo, capsys, path):
    stage(repo, path)

    assert gate(capsys) == (1, ["차단된 민감 파일:", f"  {path}", "SENSITIVE_GATE result=BLOCKED"])


def test_gate_allows_example_template(repo, capsys):
    stage(repo, ".env.example")

    assert gate(capsys) == (0, ["SENSITIVE_GATE result=PASS"])


def test_gate_fails_closed_when_git_fails(repo, capsys, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(repo / "missing"))

    assert gate(capsys) == (1, ["SENSITIVE_GATE result=ERROR"])


def decide(command: str) -> str:
    """훅에 하네스 stdin JSON을 흘려 deny/pass를 받는다. 훅은 판정과 상관없이 0으로 끝나야 한다."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    done = subprocess.run(
        ["bash", str(HOOK)], input=payload, capture_output=True, text=True, check=True
    )
    if not done.stdout.strip():
        return "pass"
    return json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"]


@needs_bash
@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("make test", "pass"),  # git 외 명령
        ("git status --short", "pass"),
        ("git add .env", "deny"),
        ("git add -A && git commit -m x", "deny"),  # 광역 add가 untracked .env를 끌어들인다
        ("git add note.txt", "pass"),
        ("git commit -m 'x'", "pass"),  # 스테이징된 민감 파일이 없다
    ],
)
def test_hook_with_untracked_env(repo, command, want):
    (repo / ".env").write_text("secret\n")

    assert decide(command) == want


@needs_bash
def test_hook_denies_commit_with_staged_env(repo):
    stage(repo, ".env")

    assert decide("git commit -m 'x'") == "deny"


# 비공개 데이터 폴더(원본 영상·가중치 등)는 확장자와 상관없이 막는다
@needs_bash
@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("git add data/private/case-001.mp4", "deny"),
        ("git add -A", "deny"),  # 광역 add가 untracked 원본 영상을 끌어들인다
        ("git add data/corpora/README.md", "pass"),
    ],
)
def test_hook_with_untracked_private_data(repo, command, want):
    (repo / "data" / "private").mkdir(parents=True)
    (repo / "data" / "private" / "case-001.mp4").write_text("video\n")

    assert decide(command) == want


# .gitignore가 막는 민감 파일을 -f로 끌어들이는 경로 — 명시 인자 파싱이 광역 판정으로 새지 않아야
# 잡힌다 (macOS sed는 \b를 몰라 원본 훅의 파싱이 광역 판정으로 떨어졌다 — 회귀 방지)
@needs_bash
@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("git add -f .env", "deny"),
        ("git add -A -f", "deny"),  # 광역 + 강제는 ignored 파일까지 스테이징한다
        ("git add .", "pass"),  # git도 ignored 파일은 add하지 않는다
        ("git add -f note.txt", "pass"),  # 강제지만 명시·비민감
        ("git add -- .env", "deny"),
    ],
)
def test_hook_with_ignored_env(repo, command, want):
    (repo / ".gitignore").write_text(".env*\n")
    (repo / ".env").write_text("secret\n")

    assert decide(command) == want
