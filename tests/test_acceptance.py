"""Fresh-consumer acceptance through the real kernel, runner, and workspace.

Only the Codex transport is substituted. Its command leg executes the fixture's
actual Python check; its review leg supplies deterministic behavioral critiques
of the frozen snapshot. These tests prove delivery mechanics, not live model,
native-subagent, or operating-system sandbox behavior.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from devgod.hooks import handle_event
from devgod.models import CommandResult, Finding, ReviewPayload, ReviewResult
from devgod.service import DevGodService, ServiceError
from devgod.store import Store, StoreError
from devgod.verification import ROLES, VerificationRunner
from devgod.workspace import Workspace

BROKEN_PRICING = "def subtotal(prices):\n    return sum(prices) + 1\n"
PARTIAL_PRICING = "def subtotal(prices):\n    return sum(prices)\n"
CORRECT_PRICING = (
    "def subtotal(prices):\n"
    "    if any(price < 0 for price in prices):\n"
    "        raise ValueError('Prices must be nonnegative')\n"
    "    return sum(prices)\n"
)
INVOICE = "from pricing import subtotal\n\ndef invoice_total(prices):\n    return subtotal(prices)\n"
PUBLIC_CHECK = (
    "from invoice import invoice_total\n"
    "from pricing import subtotal\n"
    "assert subtotal([2, 3]) == 5, 'subtotal arithmetic is incorrect'\n"
    "assert invoice_total([2, 3]) == 5, 'invoice must use correct subtotal'\n"
    "print('REAL_CHECK_PASSED')\n"
)
NEGATIVE_PRICE_PROBE = (
    "from invoice import invoice_total\n"
    "try:\n"
    "    invoice_total([-1])\n"
    "except ValueError:\n"
    "    print('NEGATIVE_PRICE_REJECTED')\n"
    "else:\n"
    "    raise SystemExit(7)\n"
)


def git(root: Path, *args: str) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_AUTHOR_NAME="Acceptance Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Acceptance Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env=environment,
    ).stdout


class DeterministicCodexTransport:
    """Exercise fixture behavior while replacing only provider communication."""

    def __init__(self, *, hold_command: bool = False) -> None:
        self.command_started = asyncio.Event()
        self.release_command = asyncio.Event()
        if not hold_command:
            self.release_command.set()
        self.command_calls: list[dict[str, Any]] = []
        self.review_calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.stopped: set[str] = set()

    async def run_command(
        self, spec, candidate, policy, on_event=None, *, invocation_id=None,
    ) -> CommandResult:
        assert invocation_id
        assert policy.sandbox_mode == "workspace-write"
        assert policy.network_access is False
        cwd = (Path(candidate.repo_root) / spec.cwd).resolve()
        self.command_calls.append({"id": invocation_id, "digest": candidate.candidate_digest})
        self.command_started.set()
        await self.release_command.wait()
        process = await asyncio.create_subprocess_exec(
            *spec.argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.processes[invocation_id] = process
        stdout, stderr = await process.communicate()
        self.stopped.add(invocation_id)
        return CommandResult(
            invocation_id=invocation_id,
            exit_code=process.returncode,
            argv=list(spec.argv),
            cwd=str(cwd),
            stdout=stdout.decode(),
            stderr=stderr.decode(),
        )

    async def run_review(
        self, role, candidate, packet, policy, on_event=None, *, invocation_id=None,
    ) -> ReviewResult:
        assert invocation_id
        assert policy.review_sandbox_mode == "read-only"
        snapshot = Path(candidate.snapshot_path)
        assert snapshot.resolve() != Path(candidate.repo_root).resolve()
        assert (snapshot / "invoice.py").read_text() == INVOICE
        assert packet["evidence"], "A reviewer must receive actual check evidence."
        for evidence in packet["evidence"]:
            assert evidence["kind"] == "check"
            assert evidence["payload"]["result"]["exit_code"] == 0
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-c", NEGATIVE_PRICE_PROBE,
            cwd=snapshot, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.processes[invocation_id] = process
        await process.communicate()
        self.stopped.add(invocation_id)
        rejected = role == "reviewer" and process.returncode != 0
        self.review_calls.append({
            "id": invocation_id,
            "role": role,
            "digest": candidate.candidate_digest,
            "decision": "request_changes" if rejected else "approve",
            "snapshot": str(snapshot),
        })
        return ReviewResult(
            invocation_id=invocation_id,
            role=role,
            candidate_digest=candidate.candidate_digest,
            checks_digest=candidate.checks_digest,
            thread_id=f"fixture-thread-{invocation_id}",
            turn_id=f"fixture-turn-{invocation_id}",
            payload=ReviewPayload(
                decision="request_changes" if rejected else "approve",
                summary="Negative prices remain accepted." if rejected else "Assigned review passed.",
                findings=[Finding(
                    severity="high",
                    summary="Invoice accepts negative prices despite the accepted criterion.",
                    path="pricing.py",
                    recommendation="Reject negative prices before computing the subtotal.",
                )] if rejected else [],
                acceptance_ids=packet["acceptance_ids"],
                evidence_refs=[packet["candidate_reference"]],
            ),
        )

    async def cancel(self, invocation_id: str) -> None:
        self.cancelled.append(invocation_id)
        process = self.processes.get(invocation_id)
        if process is not None and process.returncode is None:
            process.terminate()
            await process.wait()
        self.stopped.add(invocation_id)

    def termination_confirmed(self, invocation_id: str) -> bool:
        return invocation_id in self.stopped

    async def capabilities(self) -> dict:
        return {"simulated_transport": True}

    async def close(self) -> None:
        return None


def prepare_consumer(root: Path) -> None:
    (root / ".gitignore").write_text("__pycache__/\n")
    (root / "pricing.py").write_text(BROKEN_PRICING)
    (root / "verify.py").write_text(PUBLIC_CHECK)
    git(root, "add", ".gitignore", "pricing.py", "verify.py")
    git(root, "commit", "-m", "Add initial pricing application and real check")


def start_delivery(service: DevGodService) -> str:
    result = service.start(
        goal="Correct totals and reject negative prices in invoice calculations.",
        acceptance=[
            {"acceptance_id": "totals", "description": "Subtotal and invoice totals are correct."},
            {"acceptance_id": "validation", "description": "Negative prices raise ValueError."},
        ],
        tasks=[
            {"task_id": "pricing", "title": "Correct subtotal and input validation",
             "acceptance": ["totals", "validation"], "allowed_paths": ["pricing.py"]},
            {"task_id": "invoice", "title": "Expose invoice integration",
             "acceptance": ["totals", "validation"], "allowed_paths": ["invoice.py"],
             "depends_on": ["pricing"]},
        ],
        checks=[{"name": "behavior", "argv": [sys.executable, "-B", "verify.py"],
                 "acceptance_ids": ["totals"]}],
        decisions={"endpoint": "Verified local branch; no commit or publication."},
    )
    return result["run"]["run_id"]


def claim_implementation(service: DevGodService, run_id: str, root: Path) -> None:
    assert service.next_action(run_id)["task_id"] == "pricing"
    with pytest.raises(StoreError, match="dependencies"):
        service.task_update(run_id, "invoice", "implementing")
    service.task_update(run_id, "pricing", "implementing")
    service.task_update(run_id, "pricing", "verifying", "Initial implementation ready for checks.")
    service.task_update(run_id, "invoice", "implementing")
    (root / "invoice.py").write_text(INVOICE)
    service.task_update(run_id, "invoice", "verifying", "Invoice integration implemented.")


def repair_pricing(service: DevGodService, run_id: str, root: Path, source: str) -> None:
    service.task_update(run_id, "pricing", "implementing")
    (root / "pricing.py").write_text(source)
    service.task_update(run_id, "pricing", "verifying", "Repair implemented; independent evidence required.")
    # A prerequisite repair invalidates the dependent implementation claim.
    assert service.next_action(run_id)["task_id"] == "invoice"
    service.task_update(run_id, "invoice", "implementing")
    assert (root / "invoice.py").read_text() == INVOICE
    service.task_update(run_id, "invoice", "verifying", "Rechecked integration with repaired subtotal.")


async def verify(service: DevGodService, runner: VerificationRunner, run_id: str) -> dict:
    job = await service.verify(run_id)
    await asyncio.wait_for(runner.wait(job["job_id"]), timeout=15)
    return service.verification_status(job["job_id"])


def test_fresh_consumer_failed_check_review_rejection_repair_and_current_branch(
    git_repo: Path, tmp_path: Path,
) -> None:
    prepare_consumer(git_repo)
    (git_repo / "README.md").write_text("User's staged documentation\n")
    git(git_repo, "add", "README.md")
    (git_repo / "README.md").write_text("User's unstaged documentation\n")
    (git_repo / "notes.txt").write_text("User's untracked notes\n")
    staged_before = git(git_repo, "show", ":README.md")
    head_before = git(git_repo, "rev-parse", "HEAD")

    async def exercise() -> None:
        workspace = Workspace(git_repo, tmp_path / "state")
        store = Store(workspace.state_dir)
        transport = DeterministicCodexTransport()
        runner = VerificationRunner(workspace, store, transport)
        service = DevGodService(workspace, store, runner)
        try:
            run_id = start_delivery(service)
            claim_implementation(service, run_id, git_repo)
            failed = await verify(service, runner, run_id)
            assert failed["run"]["state"] == "repair"
            assert failed["job"]["state"] == "failed"
            assert len(transport.command_calls) == 1
            assert not transport.review_calls
            assert any(
                "subtotal arithmetic is incorrect" in output["text"]
                for check in failed["evidence"]["checks"]
                for output in check["output_excerpts"]
            ), "Native manager needs the actual failed-check diagnostic through its tools"
            check = store.list_evidence(run_id)[0]
            assert check["payload"]["result"]["exit_code"] != 0
            error_log = Path(check["payload"]["artifacts"][1]["path"]).read_text()
            assert "subtotal arithmetic is incorrect" in error_log

            repair_pricing(service, run_id, git_repo, PARTIAL_PRICING)
            rejected = await verify(service, runner, run_id)
            assert rejected["run"]["state"] == "repair"
            assert {call["role"] for call in transport.review_calls} == set(ROLES)
            assert any(call["decision"] == "request_changes" for call in transport.review_calls)
            assert any("findings" in reason for reason in rejected["run"]["gate"]["unmet_requirements"])
            findings = [
                finding for review in rejected["evidence"]["reviews"]
                for finding in review["findings"]
            ]
            assert any(
                finding["path"] == "pricing.py"
                and finding["severity"] == "high"
                and "Reject negative prices" in finding["recommendation"]
                for finding in findings
            ), "Native manager needs actionable review findings without private database access"

            repair_pricing(service, run_id, git_repo, CORRECT_PRICING)
            service.checkpoint(run_id, {"completed": ["pricing", "invoice"],
                                        "next_actions": ["Run fresh checks and all independent reviews."]})
            completed = await verify(service, runner, run_id)
            assert completed["run"]["state"] == "verified"
            gate = completed["run"]["gate"]
            assert gate["verified"] is True
            assert len(gate["evidence_ids"]) == 4
            current_reviews = [call for call in transport.review_calls
                               if call["digest"] == gate["candidate_digest"]]
            assert {call["role"] for call in current_reviews} == set(ROLES)
            assert len({call["id"] for call in current_reviews}) == 3
            assert all(call["decision"] == "approve" for call in current_reviews)
            assert len(transport.command_calls) == 3
            assert service.next_action(run_id)["action"] == "report"
            assert git(git_repo, "branch", "--show-current").decode().strip().startswith("devgod/")
            assert git(git_repo, "rev-parse", "HEAD") == head_before
            assert git(git_repo, "show", ":README.md") == staged_before
            assert (git_repo / "README.md").read_text() == "User's unstaged documentation\n"
            assert (git_repo / "notes.txt").read_text() == "User's untracked notes\n"

            # A fresh public CLI process must derive the same result from durable
            # state; it does not receive the test's adapter or in-memory objects.
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            observed = subprocess.run(
                [sys.executable, "-m", "devgod", "--repo", str(git_repo),
                 "--state-home", str(tmp_path / "state"), "--json", "status", run_id],
                check=True, capture_output=True, text=True, env=environment, timeout=15,
            )
            cli_status = json.loads(observed.stdout)
            assert cli_status["run"]["state"] == "verified"
            assert cli_status["run"]["gate"]["candidate_digest"] == gate["candidate_digest"]

            (git_repo / "pricing.py").write_text(BROKEN_PRICING)
            stale = service.status(run_id)
            assert stale["run"]["state"] == "repair"
            assert stale["next_action"]["action"] != "report"
            assert not (stale["run"].get("gate") or {}).get("verified", False)
        finally:
            await runner.close()
            store.close()

    asyncio.run(exercise())


def test_duplicate_dispatch_and_interruption_preserve_unfinished_work(
    git_repo: Path, tmp_path: Path,
) -> None:
    prepare_consumer(git_repo)

    async def exercise() -> None:
        workspace = Workspace(git_repo, tmp_path / "state")
        store = Store(workspace.state_dir)
        transport = DeterministicCodexTransport(hold_command=True)
        runner = VerificationRunner(workspace, store, transport)
        service = DevGodService(workspace, store, runner)
        run_id = start_delivery(service)
        claim_implementation(service, run_id, git_repo)
        repair_pricing(service, run_id, git_repo, CORRECT_PRICING)
        source_before = (git_repo / "pricing.py").read_bytes()
        service.checkpoint(run_id, {"summary": "Implementation complete; awaiting actual checks."})
        first = await service.verify(run_id)
        await asyncio.wait_for(transport.command_started.wait(), timeout=5)
        duplicate = await service.verify(run_id)
        assert first["job_id"] == duplicate["job_id"]
        assert len(transport.command_calls) == 1
        assert service.next_action(run_id)["action"] == "wait"
        continuation = handle_event(
            {"hook_event_name": "Stop", "session_id": "acceptance-session", "turn_id": "wait-1"},
            service=service,
        )
        assert continuation.get("decision") == "block", "A running gate must not need a user wakeup."
        await runner.close()
        assert transport.cancelled
        assert store.get_job(first["job_id"])["state"] == "interrupted"
        assert not store.list_evidence(run_id)
        store.close()

        reopened = Store(workspace.state_dir)
        resumed_runner = VerificationRunner(workspace, reopened, DeterministicCodexTransport())
        resumed_service = DevGodService(workspace, reopened, resumed_runner)
        try:
            resumed = resumed_service.resume(run_id)
            assert resumed["run"]["state"] != "verified"
            assert resumed["next_action"]["action"] == "inspect"
            assert resumed["run"]["checkpoint"]["context"]["summary"].startswith("Implementation complete")
            assert {task["task_id"] for task in resumed["tasks"]} == {"pricing", "invoice"}
            # The manager acknowledges an actual inspection, without changing
            # correct code or manufacturing evidence just to unlock a retry.
            interrupted = resumed_service.verification_status(first["job_id"])["job"]
            recovered_job = await resumed_service.recover(
                job_id=interrupted["job_id"],
                attempt=interrupted["attempt"],
                candidate_digest=interrupted["candidate"]["candidate_digest"],
                checks_digest=interrupted["candidate"]["checks_digest"],
                observations="The cancelled fixture command had not launched its subprocess; source and user work are unchanged.",
            )
            await asyncio.wait_for(resumed_runner.wait(recovered_job["job_id"]), timeout=15)
            recovered = resumed_service.verification_status(recovered_job["job_id"])
            assert recovered["run"]["state"] == "verified"
            assert (git_repo / "pricing.py").read_bytes() == source_before
            assert recovered["job"]["attempt"] > interrupted["attempt"] or recovered["job"]["job_id"] != interrupted["job_id"]
        finally:
            await resumed_runner.close()
            reopened.close()

    asyncio.run(exercise())


def test_public_claims_cannot_bypass_missing_task_or_give_review_authority(
    git_repo: Path, tmp_path: Path,
) -> None:
    prepare_consumer(git_repo)

    async def exercise() -> None:
        workspace = Workspace(git_repo, tmp_path / "state")
        store = Store(workspace.state_dir)
        transport = DeterministicCodexTransport()
        runner = VerificationRunner(workspace, store, transport)
        service = DevGodService(workspace, store, runner)
        try:
            run_id = start_delivery(service)
            with pytest.raises(ServiceError, match="only observed checks"):
                service.task_update(run_id, "pricing", "verified", "All reviews approve.")
            service.checkpoint(run_id, {"verified": True, "reviews": {role: "approve" for role in ROLES}})
            with pytest.raises(ServiceError, match="every task"):
                await service.verify(run_id)
            assert service.status(run_id)["run"]["state"] != "verified"
            assert not transport.command_calls
            assert not transport.review_calls
            assert not store.list_evidence(run_id)
        finally:
            await runner.close()
            store.close()

    asyncio.run(exercise())


def test_corrupted_receipt_cannot_leave_public_verified_status(
    git_repo: Path, tmp_path: Path,
) -> None:
    prepare_consumer(git_repo)

    async def exercise() -> None:
        workspace = Workspace(git_repo, tmp_path / "state")
        store = Store(workspace.state_dir)
        runner = VerificationRunner(workspace, store, DeterministicCodexTransport())
        service = DevGodService(workspace, store, runner)
        try:
            run_id = start_delivery(service)
            claim_implementation(service, run_id, git_repo)
            repair_pricing(service, run_id, git_repo, CORRECT_PRICING)
            initial = await verify(service, runner, run_id)
            assert initial["run"]["state"] == "verified"
            initial_digest = initial["run"]["gate"]["candidate_digest"]
            receipt = next(item for item in store.list_evidence(run_id) if item["kind"] == "review")
            artifact = Path(receipt["payload"]["artifacts"][0]["path"])
            artifact.write_text(json.dumps({"decision": "approve", "summary": "Tampered receipt"}))
            status = service.status(run_id)
            assert status["run"]["state"] != "verified"
            assert status["next_action"]["action"] != "report"
            rebuilt = await verify(service, runner, run_id)
            assert rebuilt["run"]["state"] == "verified"
            assert rebuilt["run"]["gate"]["candidate_digest"] == initial_digest
            assert (git_repo / "pricing.py").read_text() == CORRECT_PRICING
            assert len(runner.adapter.review_calls) > 3
        finally:
            await runner.close()
            store.close()

    asyncio.run(exercise())
