"""Small lifecycle handlers; advisory context and bounded Stop continuation only.

Hooks consume supported event fields. They never read transcripts, run project
commands, authorize tools, or mark implementation/reviews verified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

MAX_INPUT = 64 * 1024
MAX_CONTEXT = 4800
MAX_STOP_CONTINUATIONS = 8
MAX_SAME_ACTION = 2
EVENTS = {"SessionStart", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "Stop"}
ACTIONABLE = {"implement", "continue", "inspect", "verify", "repair", "restore", "resume", "plan", "wait"}


def _context(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text[:MAX_CONTEXT]}}


def _summary(status: dict[str, Any]) -> str:
    run = status.get("run") or {}
    checkpoint = run.get("checkpoint") or {}
    data = {
        "run_id": run.get("run_id"), "state": run.get("state"),
        "checkpoint": checkpoint.get("context"), "next_action": status.get("next_action"),
    }
    # Treat recorded decisions as data, not arbitrary hook-generated authority.
    return "DevGod persisted workflow data. Restore accepted scope through MCP status; follow current user steering.\n" + json.dumps(data, ensure_ascii=True, default=str)[:MAX_CONTEXT - 220]


def _observation(service: Any, run_id: str, event: str, payload: Mapping[str, Any]) -> None:
    store = getattr(service, "store", None)
    if store is None:
        return
    data = {key: value[:256] for key in ("session_id", "turn_id", "agent_id", "agent_type", "source", "trigger") if isinstance((value := payload.get(key)), str)}
    key = "hook:" + hashlib.sha256(json.dumps([run_id, event, data], sort_keys=True).encode()).hexdigest()
    store.append_event(run_id, "native_hook", {"event": event, **data}, event_key=key)


def _checkpoint(service: Any, run: dict[str, Any], event: str, payload: Mapping[str, Any]) -> None:
    context = dict((run.get("checkpoint") or {}).get("context") or {})
    context["native_lifecycle"] = {"event": event, "session_id": str(payload.get("session_id", ""))[:256], "turn_id": str(payload.get("turn_id", ""))[:256]}
    # Retain the manager's decisions; observing compaction cannot infer new ones.
    service.checkpoint(run["run_id"], context)


def _continuation(service: Any, status: dict[str, Any], payload: Mapping[str, Any]) -> bool:
    """Lock a tiny private per-session counter, bounding retries even across restart."""
    workspace = getattr(service, "workspace", None)
    if workspace is None:
        return False
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 256:
        return False
    directory = Path(workspace.state_dir) / "native-hooks"
    if any(parent.is_symlink() for parent in [*directory.parents, directory]):
        raise ValueError("Private hook state refuses symlink parents")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256((str(workspace.worktree_id) + ":" + session).encode()).hexdigest()
    path = directory / f"{key}.json"
    # On supported local Unix hosts flock serializes concurrent duplicate hooks.
    import fcntl

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        raw = stream.read(8193)
        try:
            state = json.loads(raw) if raw else {}
            if not isinstance(state, dict) or len(raw) > 8192:
                state = {}
        except ValueError:
            state = {}
        run = status["run"]
        if state.get("run_id") != run["run_id"]:
            state = {"run_id": run["run_id"], "count": 0, "same": 0}
        action = status.get("next_action") or {}
        # Checkpoint timestamps and hook events are not evidence of progress.
        progress = {
            "state": run.get("state"),
            "tasks": [{k: item.get(k) for k in ("task_id", "state")} for item in status.get("tasks", [])],
            "jobs": [{k: item.get(k) for k in ("job_id", "state", "attempt")} for item in status.get("jobs", [])],
        }
        fingerprint = hashlib.sha256(json.dumps(progress, sort_keys=True, default=str).encode()).hexdigest()
        turn = str(payload.get("turn_id", ""))[:256]
        if turn and state.get("last_turn") == turn:
            return False
        unchanged = state.get("fingerprint") == fingerprint
        same = state.get("same", 0) + 1 if unchanged else 1
        count = state.get("count", 0) if unchanged else 0
        limit = MAX_STOP_CONTINUATIONS if action.get("action") == "wait" else MAX_SAME_ACTION
        if count >= limit or same > limit:
            return False
        state.update({"count": count + 1, "same": same, "fingerprint": fingerprint, "last_turn": turn})
        stream.seek(0)
        stream.write(json.dumps(state))
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        return True


def handle_event(payload: Mapping[str, Any], *, repo: Path | str | None = None, state_home: Path | str | None = None, service: Any = None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or event not in EVENTS or os.environ.get("DEVGOD_MANAGED_REVIEW") == "1":
        return {}
    owned_store = None
    try:
        if service is None:
            from .service import DevGodService
            from .store import Store
            from .workspace import Workspace

            cwd = repo if repo is not None else payload.get("cwd")
            if not isinstance(cwd, (str, Path)):
                return {}
            workspace = Workspace(Path(cwd), state_root=state_home)
            # Do not create operational state in repositories without a run.
            if not (workspace.state_dir / "state.sqlite3").exists():
                return {}
            owned_store = Store(workspace.state_dir)
            if not owned_store.list_runs(workspace.worktree_id):
                return {}
            service = DevGodService(workspace, owned_store)
        status = service.status()
        run = status.get("run")
        if not isinstance(run, dict) or not run.get("run_id"):
            return {}
        _observation(service, run["run_id"], event, payload)
        if event in {"PreCompact", "PostCompact", "SubagentStop", "Stop"}:
            _checkpoint(service, run, event, payload)
        if event == "SubagentStart":
            return _context(event, "Execute only your assigned specialist scope and report to the parent manager. Do not create another DevGod run or launch managed verification unless the manager assigned that integration action. Honor existing repository controls.\n" + _summary(status))
        if event == "SessionStart":
            return _context(event, _summary(status))
        if event != "Stop":
            return {}
        if run.get("state") in {"verified", "blocked", "paused", "cancelled", "planning"} or payload.get("permission_mode") == "plan":
            return {}
        action = status.get("next_action") or {}
        if action.get("action") not in ACTIONABLE:
            return {}
        if not _continuation(service, status, payload):
            return {}
        waiting = action.get("action") == "wait"
        reason = (
            "Continue the accepted DevGod task using MCP status and next action. "
            + ("Verification jobs remain active. Use the MCP wait tool for the recorded job IDs, observe their completion, and continue verification or repair without requesting a user wakeup. " if waiting else "")
            +
            "Handle routine implementation, verification dispatch, repair and checkpointing automatically. "
            "Respect current user steering, cancellation and actual host permission boundaries. "
            "Do not claim verified completion without the kernel gate. Recorded next action: "
            + json.dumps(action, ensure_ascii=True, default=str)
        )
        return {"decision": "block", "reason": reason[:MAX_CONTEXT]}
    except Exception:
        # Hooks may fail open. An unavailable hook never creates passing evidence.
        return {"systemMessage": "DevGod lifecycle state was unavailable. The manager can recover through MCP status/resume; verified completion still requires the kernel gate."}
    finally:
        if owned_store is not None and hasattr(owned_store, "close"):
            owned_store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DevGod lifecycle hook")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--state-home", type=Path)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        payload = json.loads(raw) if len(raw) <= MAX_INPUT else None
        result = handle_event(payload, repo=args.repo, state_home=args.state_home) if isinstance(payload, dict) else {}
    except (ValueError, UnicodeError):
        result = {}
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
