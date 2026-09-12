from __future__ import annotations

import asyncio
import os

import pytest
from pydantic import ValidationError

from devgod.models import CommandResult, ReviewResult, TaskSpec
from devgod.service import DevGodService, ServiceError, scopes_overlap, validate_plan
from devgod.store import StoreError
from devgod.verification import VerificationRunner
from devgod.workspace import Workspace


def task(task_id="one", paths=None, deps=None):
    return {"task_id": task_id, "title": f"Implement {task_id}", "acceptance": ["AC-1"],
            "allowed_paths": paths or ["src/"], "depends_on": deps or []}


@pytest.fixture
def service(git_repo, tmp_path):
    workspace = Workspace(git_repo, state_root=tmp_path / "state")
    value = DevGodService(workspace)
    yield value
    value.store.close()


def start(service, tasks=None):
    return service.start("Deliver example", [{"acceptance_id": "AC-1", "description": "Example works"}],
                         tasks=tasks or [task()], checks=[{"name": "unit", "argv": ["python", "-V"], "acceptance_ids": ["AC-1"]}])


def test_start_is_idempotent_and_preserves_preexisting_work(service):
    (service.workspace.root / "README.md").write_text("User work\n")
    first = start(service)
    repeated = start(service)
    assert first["run"]["run_id"] == repeated["run"]["run_id"]
    assert (service.workspace.root / "README.md").read_text() == "User work\n"
    assert first["run"]["spec"]["branch"].startswith("devgod/")
    assert first["next_action"]["action"] == "implement"
    with pytest.raises(ServiceError, match="accepted plan"):
        service.start("Deliver example", [{"acceptance_id": "AC-2", "description": "Changed"}])


def test_cycle_unknown_dependency_and_invalid_scope_rejected_before_branch(service):
    with pytest.raises(ServiceError, match="cycle"):
        start(service, [task("one", deps=["two"]), task("two", deps=["one"])])
    with pytest.raises(ServiceError, match="unknown tasks"):
        start(service, [task(deps=["missing"])])
    with pytest.raises(ValidationError, match="repository"):
        start(service, [task(paths=["../outside"])])
    assert service.store.list_runs() == []


def test_dependency_claim_unlocks_next_task_without_granting_verification(service):
    run_id = start(service, [task("one"), task("two", deps=["one"])])["run"]["run_id"]
    with pytest.raises(StoreError, match="dependencies"):
        service.task_update(run_id, "two", "implementing")
    service.task_update(run_id, "one", "implementing")
    result = service.task_update(run_id, "one", "verifying", "Implemented and locally inspected")
    assert result["run"]["state"] != "verified"
    assert result["next_action"]["task_id"] == "two"
    service.task_update(run_id, "two", "implementing")
    result = service.task_update(run_id, "two", "verifying")
    assert result["next_action"]["action"] == "verify"
    with pytest.raises(ServiceError, match="only observed"):
        service.task_update(run_id, "two", "verified")


def test_concurrent_scopes_are_serialized_and_disjoint_work_allowed(service):
    run_id = start(service, [task("one", ["src/*.py"]), task("two", ["src/other.py"]), task("three", ["docs/"])])["run"]["run_id"]
    service.task_update(run_id, "one", "implementing")
    with pytest.raises(StoreError, match="Write scope"):
        service.task_update(run_id, "two", "implementing")
    service.task_update(run_id, "three", "implementing")
    assert scopes_overlap(task(paths=["."]), task(paths=["elsewhere/file"]))


def test_checkpoint_and_resume_need_no_manual_state_files(service):
    run_id = start(service)["run"]["run_id"]
    saved = service.checkpoint(run_id, {"decisions": {"interface": "Codex"}, "next": "implementation"})
    service.store.set_run_state(run_id, "paused", reason="Host closed")
    resumed = service.resume()
    assert resumed["run"]["checkpoint"] == saved
    assert resumed["next_action"]["action"] == "implement"


def test_public_schema_cannot_ingest_completion_evidence(service):
    with pytest.raises(ValidationError, match="Extra inputs"):
        start(service, [task() | {"verified": True, "review_receipt": {"approved": True}}])
    run_id = start(service)["run"]["run_id"]
    assert not hasattr(service, "record_evidence")
    assert not hasattr(service, "force_complete")
    with pytest.raises(ServiceError, match="implementation claim"):
        asyncio.run(service.verify(run_id))


def test_status_redacts_lease_credentials(service):
    run_id = start(service)["run"]["run_id"]
    candidate = service._candidate(service.store.get_run(run_id))
    job = service.store.enqueue_job(run_id, "verification", candidate, idempotency_key="example")
    lease = service.store.claim_job(job["job_id"], "private-owner")
    result = service.status(run_id)
    assert "baseline" not in result["run"]
    assert "lease_token" not in result["jobs"][0]
    assert lease.lease_token not in str(result)


def test_missing_execution_adapter_remains_unverified(service):
    run_id = start(service)["run"]["run_id"]
    service.task_update(run_id, "one", "implementing")
    service.task_update(run_id, "one", "verifying")
    with pytest.raises(ServiceError, match="adapter is unavailable"):
        asyncio.run(service.verify(run_id))
    assert service.status(run_id)["run"]["state"] != "verified"


def test_dependency_validation_accepts_valid_dag():
    validate_plan([TaskSpec.model_validate(task("one")), TaskSpec.model_validate(task("two", deps=["one"]))],
                  [{"acceptance_id": "AC-1", "description": "Works"}])


def test_empty_initial_plan_can_be_amended_without_recreating_run(service):
    initial = service.start("Deliver example", [{"acceptance_id": "AC-1", "description": "Works"}])
    run_id = initial["run"]["run_id"]
    planned = service.plan(run_id, [task()], checks=[{"name": "unit", "argv": ["python", "-V"]}])
    assert planned["run"]["spec"]["checks"][0]["name"] == "unit"
    assert planned["next_action"]["action"] == "implement"
    before = service._candidate(planned["run"])
    service.plan(run_id, checks=[{"name": "unit", "argv": ["python", "-VV"]}])
    after = service._candidate(service.store.get_run(run_id))
    assert before.checks_digest != after.checks_digest
    assert len(service.store.list_runs()) == 1


def test_amended_task_invalidates_dependent_claims_and_dag_atomically(service):
    run_id = start(service, [task("one"), task("two", deps=["one"])])["run"]["run_id"]
    for name in ("one", "two"):
        service.task_update(run_id, name, "implementing")
        service.task_update(run_id, name, "verifying")
    amended = service.plan(run_id, [task("one", paths=["src/", "docs/"])])
    assert [t["state"] for t in amended["tasks"]] == ["repair", "repair"]
    with pytest.raises(ServiceError, match="cycle"):
        service.plan(run_id, [task("one", deps=["two"])])
    assert service.store.list_tasks(run_id)[0]["spec"]["depends_on"] == []


def test_reopening_prerequisite_requires_fresh_dependent_implementation_claim(service):
    run_id = start(service, [task("one"), task("two", deps=["one"])])["run"]["run_id"]
    for name in ("one", "two"):
        service.task_update(run_id, name, "implementing")
        service.task_update(run_id, name, "verifying")
    repaired = service.task_update(run_id, "one", "repair", "Fix a discovered defect")
    assert [t["state"] for t in repaired["tasks"]] == ["repair", "repair"]


def test_reconciled_coordinator_returns_automatic_dispatch_action(service):
    run_id = start(service)["run"]["run_id"]
    for state in ("implementing", "verifying"):
        service.task_update(run_id, "one", state)
    candidate = service._candidate(service.store.get_run(run_id))
    job = service.store.enqueue_job(run_id, "verification", candidate, idempotency_key="crashed")
    service.store.claim_job(job["job_id"], "departed-host")
    service.store.reconcile_jobs(now=service.store.get_job(job["job_id"])["lease_expires_at"] + 1)
    resumed = service.resume(run_id)
    assert resumed["next_action"]["action"] == "verify"
    assert resumed["next_action"]["inputs"]["job_ids"] == [job["job_id"]]


def test_status_poll_recovers_crashed_coordinator_and_interrupted_check(service, monkeypatch):
    run_id = start(service)["run"]["run_id"]
    for state in ("implementing", "verifying"):
        service.task_update(run_id, "one", state)
    candidate = service._candidate(service.store.get_run(run_id))
    parent = service.store.enqueue_job(run_id, "verification", candidate, idempotency_key="crashed-parent")
    child = service.store.enqueue_job(run_id, "check", candidate, idempotency_key="crashed-check",
                                      payload={"parent_job_id": parent["job_id"]})
    sibling = service.store.enqueue_job(run_id, "check", candidate, idempotency_key="undispatched-check",
                                        payload={"parent_job_id": parent["job_id"]})
    parent_lease = service.store.claim_job(parent["job_id"], "dead-manager")
    child_lease = service.store.claim_job(child["job_id"], "dead-manager")
    expired = max(service.store.get_job(job_id)["lease_expires_at"] for job_id in (parent["job_id"], child["job_id"])) + 1
    monkeypatch.setattr("devgod.store.time.time", lambda: expired)
    status = service.status(run_id)
    assert status["next_action"]["action"] == "inspect"
    assert service.store.get_job(parent["job_id"])["state"] == "interrupted"
    assert service.store.get_job(child["job_id"])["state"] == "interrupted"
    assert service.store.get_job(sibling["job_id"])["state"] == "queued"
    assert not service.store.finish_job(parent_lease, "succeeded")
    assert not service.store.finish_job(child_lease, "succeeded")


def test_queued_coordinator_dispatches_despite_interrupted_read_only_review(service):
    run_id = start(service)["run"]["run_id"]
    candidate = service._candidate(service.store.get_run(run_id))
    parent = service.store.enqueue_job(run_id, "verification", candidate, idempotency_key="resume-reviews")
    review = service.store.enqueue_job(run_id, "review", candidate, role="reviewer", idempotency_key="read-only-review",
                                       payload={"parent_job_id": parent["job_id"]})
    lease = service.store.claim_job(review["job_id"], "closed-session")
    service.store.finish_job(lease, "interrupted")
    status = service.status(run_id)
    assert status["next_action"]["action"] == "verify"
    assert status["next_action"]["inputs"]["job_ids"] == [parent["job_id"]]


@pytest.mark.parametrize("failed_check", [False, True])
def test_public_report_exposes_actionable_current_findings_and_check_logs(service, failed_check):
    class DiagnosticTransport:
        def __init__(self):
            self.finished = set()

        def termination_confirmed(self, invocation_id):
            return invocation_id in self.finished

        async def run_command(self, spec, candidate, policy, on_event=None, invocation_id=None):
            self.finished.add(invocation_id)
            return CommandResult(invocation_id=invocation_id, exit_code=1 if failed_check else 0,
                                 argv=list(spec.argv), cwd=str(service.workspace.root),
                                 stderr="Expected subtotal 12, observed 9.\n" if failed_check else "")

        async def run_review(self, role, candidate, packet, policy, on_event=None, invocation_id=None):
            self.finished.add(invocation_id)
            rejected = role == "reviewer"
            return ReviewResult(invocation_id=invocation_id, role=role,
                                candidate_digest=candidate.candidate_digest, checks_digest=candidate.checks_digest,
                                thread_id=f"thread-{role}", turn_id=f"turn-{role}", payload={
                                    "decision": "request_changes" if rejected else "approve",
                                    "summary": "The subtotal calculation requires repair." if rejected else "Accepted.",
                                    "acceptance_ids": ["AC-1"], "evidence_refs": [packet["candidate_reference"]],
                                    "findings": [{"severity": "high", "summary": "Subtotal drops an item.",
                                                  "path": "README.md", "line": 1,
                                                  "recommendation": "Include every quantity in the subtotal."}] if rejected else [],
                                })

        async def cancel(self, invocation_id):
            return None

    async def exercise():
        runner = VerificationRunner(service.workspace, service.store, DiagnosticTransport())
        service.verification = runner
        try:
            run_id = start(service)["run"]["run_id"]
            service.task_update(run_id, "one", "implementing")
            service.task_update(run_id, "one", "verifying")
            dispatched = await service.verify(run_id)
            await runner.wait(dispatched["job_id"])
            report = service.verification_status(dispatched["job_id"])
            assert report["run"]["state"] == "repair"
            if failed_check:
                check = report["evidence"]["checks"][0]
                assert check["exit_code"] == 1
                assert any("Expected subtotal 12" in output["text"] for output in check["output_excerpts"])
            else:
                assert report["evidence"]["reviews"], [(item["kind"], item["state"], item["error"]) for item in report["jobs"]]
                review = next(item for item in report["evidence"]["reviews"] if item["role"] == "reviewer")
                assert review["decision"] == "request_changes"
                assert review["findings"][0]["path"] == "README.md"
                assert review["findings"][0]["line"] == 1
                assert review["findings"][0]["recommendation"] == "Include every quantity in the subtotal."
                assert review["evidence_id"]
                assert "lease_token" not in str(report)
            (service.workspace.root / "README.md").write_text("A material repair changes the current candidate.\n")
            current = service.status(run_id)["evidence"]
            assert current["reviews"] == []
            assert current["checks"] == []
        finally:
            await runner.close()

    asyncio.run(exercise())


def test_public_diagnostics_do_not_follow_secret_links_or_block_on_fifo(service, tmp_path):
    artifacts = service.store.state_dir / "artifacts"
    artifacts.mkdir()
    secret = tmp_path / "private-secret"
    secret.write_text("secret-value-that-must-not-be-returned")
    linked = artifacts / "stdout.log"
    linked.symlink_to(secret)
    assert "secret-value" not in service._excerpt({"path": str(linked)})
    pipe = artifacts / "provider-pipe"
    os.mkfifo(pipe)
    assert "not a regular file" in service._excerpt({"path": str(pipe)})
