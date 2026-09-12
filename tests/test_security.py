"""Adversarial boundary regressions; synthetic reviews test gate mechanics only."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from devgod import install
from devgod.models import CommandResult, ReviewPayload, ReviewResult
from devgod.service import DevGodService
from devgod.store import Store, StoreError
from devgod.verification import VerificationRunner
from devgod.workspace import Workspace, WorkspaceError


@pytest.mark.parametrize("injection", ["cwd", "pythonpath"])
def test_default_launcher_does_not_import_consuming_repo_code(
    git_repo: Path, tmp_path: Path, injection: str,
) -> None:
    """The actual generated interpreter argv must not import an ambient module."""
    injected = git_repo if injection == "cwd" else tmp_path / "injected"
    injected.mkdir(exist_ok=True)
    marker = tmp_path / "unsandboxed-import"
    (injected / "devgod.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    )
    environment = dict(os.environ)
    if injection == "pythonpath":
        environment["PYTHONPATH"] = str(injected)
    argv = install._argv(None)
    result = subprocess.run(
        [*argv, "--help"], cwd=git_repo, env=environment,
        input="", text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "Repository Python code executed before DevGod started"


@pytest.mark.parametrize("filter_kind", ["clean", "process"])
@pytest.mark.parametrize("filter_name", ["securityprobe", "security=probe"])
def test_workspace_inspection_does_not_execute_git_content_filters(
    git_repo, tmp_path, filter_kind, filter_name,
):
    marker = tmp_path / "unsandboxed-git-filter"
    script = tmp_path / "filter.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        + ("sys.exit(1)\n" if filter_kind == "process" else "sys.stdout.write(sys.stdin.read())\n")
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "config", f"filter.{filter_name}.{filter_kind}",
         shlex.join([sys.executable, str(script)])],
        check=True, capture_output=True,
    )
    (git_repo / ".gitattributes").write_text(f"README.md filter={filter_name}\n")
    original = (git_repo / "README.md").read_text()
    (git_repo / "README.md").write_text("x" * (len(original) - 1) + "\n")
    workspace = Workspace(git_repo, tmp_path / "state")
    workspace.provenance()
    assert not marker.exists(), "Git status invoked an unsandboxed repository content filter"


def test_git_config_cannot_redirect_the_authorized_repository(git_repo, tmp_path):
    unrelated = tmp_path / "unrelated-repository"
    unrelated.mkdir()
    subprocess.run(["git", "-C", str(unrelated), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(git_repo), "config", "core.worktree", str(unrelated)],
        check=True, capture_output=True,
    )
    try:
        workspace = Workspace(git_repo, tmp_path / "state")
    except WorkspaceError:
        return
    assert workspace.root == git_repo, "Consuming Git config selected an unrelated write root"


def test_authorized_repository_subdirectories_still_resolve(git_repo, tmp_path):
    child = git_repo / "nested" / "source"
    child.mkdir(parents=True)
    workspace = Workspace(child, tmp_path / "state")
    assert workspace.root == git_repo


def test_runtime_receipt_stays_outside_ambient_repo_tmpdir(git_repo, tmp_path, monkeypatch):
    from devgod.codex_adapter import CodexAdapter

    monkeypatch.setenv("TMPDIR", str(git_repo))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "private-state"))
    monkeypatch.setattr(tempfile, "tempdir", str(git_repo))
    adapter = CodexAdapter(client_factory=lambda *_args, **_kwargs: object())
    job = adapter._new_invocation("private-control", "command", git_repo)
    assert not Path(job.receipt_path).resolve().is_relative_to(git_repo), (
        "A check can forge its supervisor receipt inside its writable repository"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process recovery boundary")
def test_recovery_does_not_confirm_a_detached_command_child_stopped():
    from devgod.codex_adapter import CodexAdapter, _boot_id, _read_process

    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-I','-c',{child_code!r}], "
        "start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print(child.pid,flush=True); time.sleep(30)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-I", "-c", parent_code], start_new_session=True,
        stdout=subprocess.PIPE, text=True,
    )
    child_pid = None
    child_descriptor = None
    try:
        assert owner.stdout is not None
        child_pid = int(owner.stdout.readline().strip())
        child_descriptor = os.pidfd_open(child_pid)
        identity = _read_process(owner.pid)
        assert identity is not None
        identity.pop("state")
        identity["boot_id"] = _boot_id()
        confirmed = asyncio.run(CodexAdapter().recover_termination(identity))
        child = _read_process(child_pid)
        alive = child is not None and child["state"] not in ("Z", "X")
        assert not confirmed or not alive, "Detached command child survived confirmed termination"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=3)
        if child_descriptor is not None:
            try:
                signal.pidfd_send_signal(child_descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(child_descriptor)


class _PassingAdapter:
    """Known deterministic receipts, without claiming authenticated review."""

    async def run_command(self, spec, candidate, policy, on_event=None, *, invocation_id):
        return CommandResult(
            invocation_id=invocation_id, exit_code=0, argv=spec.argv,
            cwd=str((Path(candidate.repo_root) / spec.cwd).resolve()),
            stdout="Synthetic command receipt for security state-machine regression.\n",
        )

    async def run_review(self, role, candidate, packet, policy, on_event=None, *, invocation_id):
        return ReviewResult(
            invocation_id=invocation_id, role=role,
            candidate_digest=candidate.candidate_digest,
            checks_digest=candidate.checks_digest,
            thread_id=f"thread-{invocation_id}", turn_id=f"turn-{invocation_id}",
            payload=ReviewPayload(
                decision="approve", summary="Synthetic security regression approval.",
                acceptance_ids=packet["acceptance_ids"],
                evidence_refs=[packet["candidate_reference"]],
            ),
        )

    async def cancel(self, invocation_id):
        return None

    def termination_confirmed(self, invocation_id):
        # This synthetic adapter starts no operating-system processes.
        return True


def _delivery(git_repo: Path, tmp_path: Path):
    workspace = Workspace(git_repo, tmp_path / "state")
    store = Store(workspace.state_dir)
    runner = VerificationRunner(workspace, store, _PassingAdapter())
    service = DevGodService(workspace, store, runner)
    started = service.start(
        "Exercise the security completion boundary",
        [{"acceptance_id": "safe", "description": "The current accepted branch is verified"}],
        [{"task_id": "change", "title": "Inspect fixture", "acceptance": ["safe"],
          "allowed_paths": ["."]}],
        [{"name": "check", "argv": [sys.executable, "-V"], "acceptance_ids": ["safe"]}],
    )
    run_id = started["run"]["run_id"]
    service.task_update(run_id, "change", "implementing")
    service.task_update(run_id, "change", "verifying")
    return service, runner, run_id


def test_branch_drift_cannot_verify_a_different_delivery_branch(git_repo, tmp_path):
    async def exercise():
        service, runner, run_id = _delivery(git_repo, tmp_path)
        try:
            expected = service.store.get_run(run_id)["spec"]["branch"]
            service.workspace._git("symbolic-ref", "HEAD", "refs/heads/main")
            try:
                job = await service.verify(run_id)
                await runner.wait(job["job_id"])
                result = service.status(run_id)
            except StoreError:
                return  # Refusing drift is safe; preserving/restoring the branch is also valid.
            observed_branch = service.workspace.provenance()["branch"]
            assert result["run"]["state"] != "verified" or observed_branch == expected
        finally:
            await runner.close()
            service.store.close()

    asyncio.run(exercise())


def test_plan_change_between_gate_evaluation_and_commit_cannot_verify(
    git_repo, tmp_path, monkeypatch,
):
    async def exercise():
        service, runner, run_id = _delivery(git_repo, tmp_path)
        try:
            job = await service.verify(run_id)
            await runner.wait(job["job_id"])
            original = service.store._finalize_gate
            raced = False

            def concurrent_plan_change(selected_run, gate, **kwargs):
                nonlocal raced
                if not raced:
                    raced = True
                    tasks = [item["spec"] for item in service.store.list_tasks(run_id)]
                    checks = [{"name": "new-required-check", "argv": [sys.executable, "-V"],
                               "acceptance_ids": ["safe"]}]
                    service.store.replace_plan(
                        run_id, tasks, checks,
                        expected_updated_at=service.store.get_run(run_id)["updated_at"],
                    )
                return original(selected_run, gate, **kwargs)

            monkeypatch.setattr(service.store, "_finalize_gate", concurrent_plan_change)
            try:
                service.status(run_id)
            except StoreError:
                pass  # An atomic stale-revision rejection must remain non-verified.
            assert raced
            assert service.store.get_run(run_id)["state"] != "verified"
        finally:
            await runner.close()
            service.store.close()

    asyncio.run(exercise())
