"""Exercise the installed administrative interface through its real entry point."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from devgod.cli import _criteria, _dispatch, _parser, _read_json


def cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "devgod", "--repo", str(repo),
         "--state-home", str(repo.parent / "state"), "--json", *args],
        env=environment, capture_output=True, text=True, timeout=20, check=False,
    )


@pytest.fixture
def cli_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "baseline"], check=True)
    return repo


def test_acceptance_parser_rejects_missing_identifiers() -> None:
    assert _criteria(["AC-1:Working output"])[0]["description"] == "Working output"
    with pytest.raises(ValueError, match="ID:DESCRIPTION"):
        _criteria(["No stable identifier"])


def test_claims_cannot_set_verified_state() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["task", "update", "task-1", "--state", "verified"])


def test_json_input_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (1_048_576 + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        _read_json(path)


def test_help_and_bad_argument_exit_codes(cli_repo: Path) -> None:
    help_result = cli(cli_repo, "--help")
    assert help_result.returncode == 0
    assert "mcp" in help_result.stdout
    invalid = cli(cli_repo, "task", "update", "task-1", "--state", "verified")
    assert invalid.returncode == 2


def test_status_returns_json_without_creating_a_run(cli_repo: Path) -> None:
    result = cli(cli_repo, "status")
    assert result.returncode == 0, result.stderr
    assert isinstance(json.loads(result.stdout), dict)


def test_start_task_checkpoint_and_cancel(cli_repo: Path) -> None:
    start = cli(cli_repo, "start", "--goal", "Build the greeting", "--acceptance", "AC-1:Print hello")
    assert start.returncode == 0, start.stdout + start.stderr
    started = json.loads(start.stdout)
    run_id = started.get("run_id") or started.get("run", {}).get("run_id")
    assert run_id
    added = cli(cli_repo, "task", "add", "--run", run_id, "--id", "greeting", "--title", "Greeting",
                "--role", "implementation", "--acceptance", "AC-1", "--path", "hello.py")
    assert added.returncode == 0, added.stdout + added.stderr
    checkpoint = cli(cli_repo, "checkpoint", "save", "--run", run_id, "--summary", "Ready to implement")
    assert checkpoint.returncode == 0, checkpoint.stdout + checkpoint.stderr
    shown = cli(cli_repo, "checkpoint", "show", "--run", run_id)
    assert json.loads(shown.stdout)["checkpoint"]["context"]["summary"] == "Ready to implement"
    checks = cli_repo.parent / "checks.json"
    checks.write_text(json.dumps([{"name": "greeting", "argv": ["python", "hello.py"],
                                   "acceptance_ids": ["AC-1"]}]))
    amended = cli(cli_repo, "plan", "--run", run_id, "--checks", str(checks))
    assert amended.returncode == 0, amended.stdout + amended.stderr
    assert json.loads(amended.stdout)["run"]["spec"]["checks"][0]["name"] == "greeting"
    cancelled = cli(cli_repo, "cancel", run_id)
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr


def test_invalid_repo_reports_actionable_json(tmp_path: Path) -> None:
    result = cli(tmp_path, "start", "--goal", "Do work", "--acceptance", "AC-1:Done")
    assert result.returncode != 0
    error = json.loads(result.stdout)["error"]
    assert error["message"]


def test_install_doctor_and_removal_through_cli(cli_repo: Path) -> None:
    instructions = cli_repo / "AGENTS.md"
    instructions.write_text("Project instructions remain here.\n")
    installed = cli(cli_repo, "init")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert json.loads(installed.stdout)["installed"] is True
    checked = cli(cli_repo, "doctor")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    runtime = json.loads(checked.stdout)["runtime"]
    assert runtime["command_protocol"] == "command/exec"
    assert runtime["live_authenticated"] is None
    removed = cli(cli_repo, "uninstall")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert instructions.read_text() == "Project instructions remain here.\n"


def test_mcp_startup_error_never_uses_transport_stdout(tmp_path: Path) -> None:
    failed = cli(tmp_path, "mcp")
    assert failed.returncode != 0
    assert failed.stdout == ""
    assert failed.stderr


def test_verify_exit_code_requires_fresh_final_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finished successful job cannot hide a candidate mutation during final reporting."""
    closed = []

    async def start(run_id):
        return {"job_id": "job_demo", "state": "queued"}

    async def wait(job_id):
        return {"job_id": job_id, "state": "succeeded"}

    async def close():
        closed.append(True)

    final_status = {"job": {"job_id": "job_demo", "state": "succeeded"},
                    "run": {"state": "repair"}, "next_action": {"action": "verify"}}
    runtime = SimpleNamespace(
        service=SimpleNamespace(verify=start, verification_status=lambda job_id: final_status),
        runner=SimpleNamespace(wait=wait), close=close,
    )
    monkeypatch.setattr("devgod.mcp_server.open_runtime", lambda *args: runtime)
    result, code = asyncio.run(_dispatch(_parser().parse_args(["verify"])))
    assert result == final_status
    assert code == 1
    assert closed == [True]


def test_recover_owns_execution_until_a_fresh_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI recovery cannot abandon its retry when the administrative process exits."""
    observed = []

    async def recover(*args):
        observed.append(("recover", args))
        return {"job_id": "job_demo", "state": "queued"}

    async def wait(job_id):
        observed.append(("wait", job_id))
        return {"job_id": job_id, "state": "succeeded"}

    async def close():
        observed.append(("close",))

    final = {"job": {"job_id": "job_demo", "state": "succeeded"},
             "run": {"state": "verified"}}
    runtime = SimpleNamespace(
        service=SimpleNamespace(recover=recover, verification_status=lambda job_id: final),
        runner=SimpleNamespace(wait=wait), close=close,
    )
    monkeypatch.setattr("devgod.mcp_server.open_runtime", lambda *args: runtime)
    args = _parser().parse_args([
        "recover", "job_demo", "--attempt", "1", "--candidate-digest", "a" * 64,
        "--checks-digest", "b" * 64, "--observations", "No interrupted effects remain.",
    ])
    result, code = asyncio.run(_dispatch(args))
    assert code == 0 and result == final
    assert observed == [
        ("recover", ("job_demo", 1, "a" * 64, "b" * 64, "No interrupted effects remain.")),
        ("wait", "job_demo"), ("close",),
    ]
