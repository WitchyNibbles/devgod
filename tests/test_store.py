from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from devgod.models import GateResult
from devgod.store import Store, StoreError


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "private")
    value.create_run({"worktree_id": "tree-a", "run_id": "run-a", "goal": "Example"}, [
        {"task_id": "one", "depends_on": [], "allowed_paths": ["src/"]}
    ])
    yield value
    value.close()


def candidate(digest="a"):
    return {"candidate_digest": digest * 64, "checks_digest": "b" * 64, "snapshot_path": "/private/snapshot"}


def job(store, kind="check", key="first"):
    return store.enqueue_job("run-a", kind, candidate(), idempotency_key=key)


def receipt():
    return {"kind": "check", "invocation_id": "observed", "candidate_digest": "a" * 64,
            "checks_digest": "b" * 64, "payload": {"succeeded": True}}


def test_parallel_enqueue_and_claim_are_exactly_once(store):
    second = Store(store.state_dir)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(lambda item: job(item), (store, second)))
            claims = list(pool.map(lambda item: item.claim_job(records[0]["job_id"], "executor"), (store, second)))
        assert records[0]["job_id"] == records[1]["job_id"]
        assert sum(claim is not None for claim in claims) == 1
        assert len(store.list_jobs("run-a")) == 1
    finally:
        second.close()


def test_duplicate_snapshot_identity_reuses_original_job(store):
    original = job(store)
    altered = candidate()
    altered["snapshot_path"] = "/private/second-identical-snapshot"
    same = store.enqueue_job("run-a", "check", altered, idempotency_key="first")
    assert same["candidate"]["snapshot_path"] == original["candidate"]["snapshot_path"]
    with pytest.raises(StoreError, match="different operation"):
        store.enqueue_job("run-a", "check", candidate("c"), idempotency_key="first")


def test_expired_check_is_fenced_and_never_blindly_replayed(store):
    record = job(store)
    lease = store.claim_job(record["job_id"], "dead-owner")
    recovered = store.reconcile_jobs(now=store.get_job(record["job_id"])["lease_expires_at"] + 1)
    assert recovered[0]["state"] == "interrupted"
    assert not store.finish_job(lease, "succeeded")
    with pytest.raises(StoreError, match="Expired or superseded"):
        store._record_evidence(lease, receipt())
    with pytest.raises(StoreError, match="Inspect"):
        store.retry_job(record["job_id"], reason="automatic retry")
    assert store.list_evidence("run-a") == []


def test_expired_review_can_retry_but_old_generation_cannot_publish(store):
    record = store.enqueue_job("run-a", "review", candidate(), role="reviewer", idempotency_key="review")
    stale = store.claim_job(record["job_id"], "owner-one")
    store.reconcile_jobs(now=store.get_job(record["job_id"])["lease_expires_at"] + 1)
    current = store.claim_job(record["job_id"], "owner-two")
    assert current.attempt == stale.attempt + 1
    assert not store.finish_job(stale, "succeeded")
    assert store.finish_job(current, "failed", error={"message": "Malformed result", "next_action": "repair"})
    assert store.get_job(record["job_id"])["error"]["next_action"] == "repair"


def test_known_live_process_is_not_requeued_on_lease_expiry(store):
    record = job(store, "verification")
    lease = store.claim_job(record["job_id"], "executor")
    store.heartbeat(lease, process_id=123, process_identity="start-time")
    result = store.reconcile_jobs(now=store.get_job(record["job_id"])["lease_expires_at"] + 1,
                                  process_alive=lambda pid, identity: True)
    assert result[0]["state"] == "interrupted"


def test_evidence_ingestion_is_bound_idempotent_and_terminal(store):
    record = job(store)
    lease = store.claim_job(record["job_id"], "executor")
    wrong = receipt() | {"candidate_digest": "c" * 64}
    with pytest.raises(StoreError, match="candidate_digest"):
        store._record_evidence(lease, wrong)
    accepted = store._record_evidence(lease, receipt())
    assert store._record_evidence(lease, receipt())["evidence_id"] == accepted["evidence_id"]
    assert store.list_evidence("run-a") == []
    with pytest.raises(StoreError, match="Conflicting"):
        store._record_evidence(lease, receipt() | {"payload": {"succeeded": False}})
    store.finish_job(lease, "succeeded")
    assert len(store.list_evidence("run-a", "a" * 64, "b" * 64)) == 1
    assert store.list_evidence("run-a", "c" * 64) == []


def test_cancel_fences_all_owned_work_and_preserves_receipts(store):
    record = job(store)
    lease = store.claim_job(record["job_id"], "executor")
    owned = store.cancel_run("run-a")
    assert owned[0]["job_id"] == record["job_id"]
    assert store.get_run("run-a")["state"] == "cancelled"
    assert not store.finish_job(lease, "succeeded")
    with pytest.raises(StoreError, match="Cancelled"):
        store.update_task("run-a", "one", "implementing")


def test_task_claim_and_empty_gate_cannot_force_verified(store):
    with pytest.raises(StoreError, match="verification owns"):
        store.update_task("run-a", "one", "verified")
    store.update_task("run-a", "one", "implementing")
    store.update_task("run-a", "one", "verifying")
    with pytest.raises(StoreError, match="Incomplete evidence"):
        store._finalize_gate("run-a", GateResult(verified=True, candidate_digest="a" * 64, checks_digest="b" * 64))
    assert store.get_run("run-a")["state"] != "verified"


def test_runs_distinguish_worktrees_and_restore_checkpoint(store):
    with pytest.raises(StoreError, match="active run"):
        store.create_run({"worktree_id": "tree-a", "run_id": "another"})
    store.create_run({"worktree_id": "tree-b", "run_id": "run-b"})
    saved = store.save_checkpoint("run-a", {"next": "implement", "decisions": {"ux": "native"}})
    assert store.get_run("run-a")["checkpoint"] == saved
    assert len(store.list_runs("tree-a")) == 1
    store.append_event("run-a", "hook.stop", {"attempt": 1}, event_key="stop-event")
    store.append_event("run-a", "hook.stop", {"attempt": 1}, event_key="stop-event")
    assert len([e for e in store.events("run-a") if e["kind"] == "hook.stop"]) == 1


def test_competing_run_creation_claims_before_branch_preparation(store):
    second = Store(store.state_dir)
    preparations = []

    def create(pair):
        selected, run_id = pair
        spec = {"worktree_id": "new-tree", "run_id": run_id}

        def prepare():
            preparations.append(run_id)
            return spec, {}

        try:
            return selected.create_run(spec, _prepare=prepare)
        except StoreError:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            created = list(pool.map(create, ((store, "first-run"), (second, "second-run"))))
        assert sum(item is not None for item in created) == 1
        assert len(preparations) == 1
    finally:
        second.close()


def test_large_derived_baseline_is_not_limited_like_model_context(store):
    baseline = {
        f"src/components/{number:08d}/implementation_long_filename.py": {
            "sha256": "a" * 64, "mode": 420, "size": 123, "kind": "file"
        }
        for number in range(15_000)
    }
    run = store.create_run({"worktree_id": "large-tree", "run_id": "large-run"}, baseline=baseline)
    assert run["baseline"] == baseline
    with pytest.raises(StoreError, match="2 MB"):
        store.save_checkpoint("large-run", {"text": "x" * 2_000_001})
