from __future__ import annotations

import subprocess
from copy import deepcopy
from types import SimpleNamespace

import pytest

from devgod import hooks


class FakeStore:
    def __init__(self):
        self.observations = {}

    def append_event(self, run_id, kind, payload, *, event_key):
        self.observations[event_key] = (run_id, kind, payload)


class FakeService:
    def __init__(self, state_dir):
        self.workspace = SimpleNamespace(state_dir=state_dir, worktree_id="worktree")
        self.store = FakeStore()
        self.saved = []
        self.data = {
            "run": {"run_id": "run1", "state": "active", "checkpoint": {"context": {"decisions": ["Keep native Codex UX"], "next": "Implement task"}}},
            "tasks": [{"task_id": "t1", "state": "planned"}], "jobs": [],
            "next_action": {"action": "implement", "task_id": "t1", "reason": "Ready"},
        }

    def status(self):
        return deepcopy(self.data)

    def checkpoint(self, run_id, context):
        self.saved.append((run_id, context))
        self.data["run"]["checkpoint"] = {"context": context}


def event(name, turn="turn1", **extra):
    return {"hook_event_name": name, "session_id": "session1", "turn_id": turn, "cwd": "/unused", **extra}


def test_session_restore_and_compaction_preserve_decisions(tmp_path):
    service = FakeService(tmp_path)
    before = service.data["run"]["checkpoint"]["context"]["decisions"]
    restored = hooks.handle_event(event("SessionStart", source="resume"), service=service)
    assert "Keep native Codex UX" in restored["hookSpecificOutput"]["additionalContext"]
    assert hooks.handle_event(event("PreCompact"), service=service) == {}
    assert hooks.handle_event(event("PostCompact"), service=service) == {}
    assert service.saved[-1][1]["decisions"] == before
    assert "transcript" not in str(service.saved)


def test_stop_continues_action_then_bounds_no_progress_and_duplicate_turns(tmp_path):
    service = FakeService(tmp_path)
    first = hooks.handle_event(event("Stop"), service=service)
    assert first["decision"] == "block"
    assert hooks.handle_event(event("Stop"), service=service) == {}
    assert hooks.handle_event(event("Stop", "turn2", stop_hook_active=True), service=service)["decision"] == "block"
    assert hooks.handle_event(event("Stop", "turn3", stop_hook_active=True), service=service) == {}
    service.data["tasks"][0]["state"] = "implementing"
    assert hooks.handle_event(event("Stop", "turn4", stop_hook_active=True), service=service)["decision"] == "block"


def test_continuation_budget_resets_on_genuine_task_progress(tmp_path):
    service = FakeService(tmp_path)
    for i in range(hooks.MAX_STOP_CONTINUATIONS + 3):
        service.data["tasks"].append({"task_id": f"task{i}", "state": "verifying"})
        assert hooks.handle_event(event("Stop", str(i)), service=service)["decision"] == "block"
    assert hooks.handle_event(event("Stop", "last"), service=service)["decision"] == "block"
    service.data["next_action"]["reason"] = "Rewording cannot count as progress"
    assert hooks.handle_event(event("Stop", "unchanged"), service=service) == {}


@pytest.mark.parametrize("state", ["verified", "blocked", "paused", "cancelled", "planning"])
def test_stop_respects_terminal_and_paused_states(tmp_path, state):
    service = FakeService(tmp_path)
    service.data["run"]["state"] = state
    assert hooks.handle_event(event("Stop"), service=service) == {}


@pytest.mark.parametrize("action", ["approval", "report", "unknown"])
def test_stop_does_not_spin_when_nothing_actionable(tmp_path, action):
    service = FakeService(tmp_path)
    service.data["next_action"]["action"] = action
    assert hooks.handle_event(event("Stop"), service=service) == {}


def test_stop_waits_for_active_reviews_without_user_wakeup(tmp_path):
    service = FakeService(tmp_path)
    service.data["run"]["state"] = "verifying"
    service.data["jobs"] = [{"job_id": "gate", "state": "running", "attempt": 1}]
    service.data["next_action"] = {"action": "wait", "inputs": {"job_ids": ["gate"]}}
    for i in range(hooks.MAX_STOP_CONTINUATIONS):
        result = hooks.handle_event(event("Stop", str(i)), service=service)
        assert result["decision"] == "block" and "MCP wait tool" in result["reason"]
    assert hooks.handle_event(event("Stop", "no-progress"), service=service) == {}
    service.data["jobs"][0]["state"] = "succeeded"
    service.data["next_action"] = {"action": "verify"}
    assert hooks.handle_event(event("Stop", "completed"), service=service)["decision"] == "block"


def test_managed_reviewer_noops_all_hooks_without_state_access(tmp_path, monkeypatch):
    service = FakeService(tmp_path)
    monkeypatch.setenv("DEVGOD_MANAGED_REVIEW", "1")
    for name in hooks.EVENTS:
        assert hooks.handle_event(event(name), service=service) == {}
    assert not service.saved and not service.store.observations


def test_subagent_observation_cannot_mark_task_done_or_launch_manager(tmp_path):
    service = FakeService(tmp_path)
    original = deepcopy(service.data["tasks"])
    output = hooks.handle_event(event("SubagentStart", agent_id="child1", agent_type="worker"), service=service)
    assert "assigned specialist scope" in output["hookSpecificOutput"]["additionalContext"]
    assert hooks.handle_event(event("SubagentStop", agent_id="child1", last_assistant_message="Everything passed"), service=service) == {}
    assert service.data["tasks"] == original


def test_untrusted_transcript_paths_are_never_read_and_context_bounded(tmp_path):
    service = FakeService(tmp_path)
    service.data["run"]["checkpoint"]["context"]["notes"] = "a" * 50_000
    path = tmp_path / "transcript"
    path.write_text("secret not for hook output")
    output = hooks.handle_event(event("SessionStart", transcript_path=str(path)), service=service)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "secret" not in context and len(context) <= hooks.MAX_CONTEXT


def test_hook_failure_never_claims_verified(tmp_path):
    service = FakeService(tmp_path)
    service.status = lambda: (_ for _ in ()).throw(RuntimeError("secret failure detail"))
    output = hooks.handle_event(event("Stop"), service=service)
    assert set(output) == {"systemMessage"}
    assert "secret failure detail" not in output["systemMessage"]


@pytest.mark.parametrize("payload", [{}, {"hook_event_name": []}, {"hook_event_name": "PermissionRequest"}])
def test_unknown_or_malformed_events_are_inert(payload):
    assert hooks.handle_event(payload) == {}


def test_malicious_session_id_is_hashed_for_private_path(tmp_path):
    service = FakeService(tmp_path / "state")
    output = hooks.handle_event(event("Stop", session_id="../../outside"), service=service)
    assert output["decision"] == "block"
    files = list((tmp_path / "state/native-hooks").iterdir())
    assert len(files) == 1 and len(files[0].stem) == 64


def test_hook_state_symlink_does_not_write_outside_private_state(tmp_path):
    service = FakeService(tmp_path / "state")
    service.workspace.state_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (service.workspace.state_dir / "native-hooks").symlink_to(outside)
    output = hooks.handle_event(event("Stop"), service=service)
    assert "systemMessage" in output and "decision" not in output
    assert list(outside.iterdir()) == []


def test_real_kernel_restores_run_without_execution_adapter(tmp_path, monkeypatch):
    from devgod.service import DevGodService
    from devgod.store import Store
    from devgod.workspace import Workspace

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "consumer"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-qm", "Initial"], cwd=root, check=True)
    workspace = Workspace(root)
    store = Store(workspace.state_dir)
    service = DevGodService(workspace, store)
    started = service.start("Implement feature", [{"acceptance_id": "ac1", "description": "Feature works"}], [{"task_id": "t1", "title": "Build feature", "acceptance": ["ac1"], "allowed_paths": ["feature.py"]}])
    service.checkpoint(started["run"]["run_id"], {"decisions": {"ux": "Native Codex"}})
    store.close()
    output = hooks.handle_event(event("SessionStart"), repo=root)
    assert "Native Codex" in output["hookSpecificOutput"]["additionalContext"]
    assert hooks.handle_event(event("PreCompact"), repo=root) == {}
    output = hooks.handle_event(event("Stop"), repo=root)
    assert output["decision"] == "block"
    store = Store(workspace.state_dir)
    observed = store.events(started["run"]["run_id"])
    assert any(item["kind"] == "native_hook" for item in observed)
    assert not store.list_jobs(started["run"]["run_id"])
    store.close()
