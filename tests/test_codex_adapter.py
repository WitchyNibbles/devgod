"""Contract and failure tests using SDK-shaped messages, not live evidence."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import errno
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from devgod.codex_adapter import (
    AdapterError,
    CodexAdapter,
    _boot_id,
    _read_process,
    _schema,
    deny_approval,
)
from devgod.launcher import pidfd_open, pidfd_send_signal
from devgod.models import Candidate, CheckSpec, ModelRoute, Policy


@pytest.fixture(autouse=True)
def private_receipt_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "private-state"))


def event(method: str, payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=payload)


class FakeClient:
    def __init__(self, config: Any, *, approval_handler: Any) -> None:
        self.config = config
        self.approval_handler = approval_handler
        self.requests: list[tuple[str, Any]] = []
        self.global_events: queue.Queue[Any] = queue.Queue()
        self.turn_events: queue.Queue[Any] = queue.Queue()
        self.terminated = threading.Event()
        self.closed = False
        self.started = False
        self.block_command = False
        self.block_startup = False
        self.startup_release = threading.Event()
        self.startup_entered = threading.Event()
        self.command_chunks = [("stdout", b"ok\n", False)]
        self.command_process_id: str | None = None
        self.tools: dict[str, Any] = {}
        self.inventory_error: str | None = None
        self.inventory_status: str | None = None
        self.authenticated = True
        self.runtime_version = "0.154.0"
        self.review_status = "completed"
        self.review_output = json.dumps(
            {
                "decision": "approve",
                "summary": "Inspected code and observed evidence.",
                "findings": [],
                "acceptance_ids": ["A1"],
                "evidence_refs": ["check_1"],
            }
        )
        self.review_events: list[Any] | None = None
        self.buffered_stdout = ""
        self.source_model: str | None = None
        self.source_reasoning_effort: str | None = None

    def start(self) -> None:
        if self.block_startup:
            self.startup_entered.set()
            self.startup_release.wait(3)
        self.started = True

    def initialize(self) -> Any:
        return SimpleNamespace(serverInfo=SimpleNamespace(version=self.runtime_version))

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.terminated.set()
            self.global_events.put(RuntimeError("closed"))
            self.turn_events.put(RuntimeError("closed"))

    def account_read(self, params: Any) -> Any:
        return SimpleNamespace(account=object() if self.authenticated else None)

    def request(self, method: str, params: Any, *, response_model: Any) -> Any:
        self.requests.append((method, params))
        if method == "command/exec":
            for stream, chunk, truncated in self.command_chunks:
                self.global_events.put(
                    event(
                        "command/exec/outputDelta",
                        {
                            "processId": self.command_process_id or params["processId"],
                            "stream": stream,
                            "deltaBase64": base64.b64encode(chunk).decode(),
                            "capReached": truncated,
                        },
                    )
                )
            if self.block_command:
                self.terminated.wait(4)
            return SimpleNamespace(exit_code=0, stdout=self.buffered_stdout, stderr="")
        if method == "command/exec/terminate":
            self.terminated.set()
            return SimpleNamespace()
        if method == "config/read":
            config: dict[str, Any] = {
                "mcp_servers": {"devgod": {"command": "private"}, "other": {}},
                "plugins": {"devgod@local": {}, "other@local": {}},
                "apps": {"configured": {"enabled": True}},
            }
            if params["cwd"] != self.config.cwd and self.source_model is not None:
                config["model"] = self.source_model
                config["model_reasoning_effort"] = self.source_reasoning_effort or "high"
            return SimpleNamespace(config=config)
        if method == "mcpServerStatus/list":
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        tools=self.tools,
                        tools_error=self.inventory_error,
                        runtime_status=self.inventory_status,
                    )
                ]
                if self.tools or self.inventory_error or self.inventory_status
                else [],
                next_cursor=None,
            )
        raise AssertionError(method)

    def next_notification(self) -> Any:
        item = self.global_events.get(timeout=5)
        if isinstance(item, Exception):
            raise item
        return item

    def thread_start(self, params: Any) -> Any:
        self.requests.append(("thread/start", params))
        return SimpleNamespace(
            thread=SimpleNamespace(id=f"thread-{id(self)}"),
            model="inherited-default",
            sandbox={"type": "readOnly", "networkAccess": False},
            approval_policy="never",
        )

    def turn_start(self, thread_id: str, prompt: str, params: Any) -> Any:
        self.requests.append(("turn/start", {"thread_id": thread_id, "prompt": prompt, **params}))
        default_events = [
            event("item/completed", {"item": {"type": "agentMessage", "text": self.review_output}}),
            event("turn/completed", {"turn": {"id": "turn-1", "status": self.review_status}}),
        ]
        for item in self.review_events if self.review_events is not None else default_events:
            self.turn_events.put(item)
        return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

    def next_turn_notification(self, turn_id: str) -> Any:
        item = self.turn_events.get(timeout=5)
        if isinstance(item, Exception):
            raise item
        return item

    def turn_interrupt(self, thread_id: str, turn_id: str) -> None:
        self.requests.append(("turn/interrupt", {"thread_id": thread_id, "turn_id": turn_id}))
        self.terminated.set()


class Factory:
    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        self.clients: list[FakeClient] = []

    def __call__(self, *args: Any, **kwargs: Any) -> FakeClient:
        client = FakeClient(*args, **kwargs)
        for key, value in self.settings.items():
            setattr(client, key, value)
        self.clients.append(client)
        return client


@pytest.fixture
def candidate(tmp_path: Path) -> Candidate:
    repo, snapshot = tmp_path / "repo", tmp_path / "snapshot"
    repo.mkdir()
    snapshot.mkdir()
    return Candidate(
        repo_id="repo",
        repo_root=str(repo),
        branch="work",
        base_revision="abc",
        head_revision="abc",
        candidate_digest="a" * 64,
        checks_digest="b" * 64,
        snapshot_path=str(snapshot),
    )


def test_no_approval_path_accepts_privileges() -> None:
    for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        assert deny_approval(method, {}) == {"decision": "decline"}
    assert deny_approval("item/permissions/requestApproval", {}) == {
        "permissions": {},
        "scope": "turn",
    }
    with pytest.raises(AdapterError):
        deny_approval("unknown/approval", {})


def test_review_schema_uses_the_portable_strict_output_subset() -> None:
    """Fine-tuned review providers reject Pydantic's array/string constraints."""
    schema = _schema()
    forbidden = {
        "default", "title", "description", "minLength", "maxLength", "pattern", "format",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "minItems", "maxItems", "patternProperties",
    }

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            assert not forbidden.intersection(node)
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)


def test_command_streams_under_explicit_sandbox_and_preserves_identity(
    candidate: Candidate,
) -> None:
    factory = Factory(command_chunks=[("stdout", b"\xe2", False), ("stdout", b"\x82\xac", False)])
    adapter = CodexAdapter(client_factory=factory)
    observed: list[dict[str, Any]] = []

    async def record(value: dict[str, Any]) -> None:
        observed.append(value)

    result = asyncio.run(
        adapter.run_command(
            CheckSpec(name="check", argv=["python", "-c", "print(1)"]),
            candidate,
            Policy(),
            record,
            invocation_id="owned-command",
        )
    )
    assert result.succeeded and result.stdout == "€"
    assert result.invocation_id == "owned-command" and result.cwd == candidate.repo_root
    client = factory.clients[0]
    params = next(payload for method, payload in client.requests if method == "command/exec")
    sandbox = params["sandboxPolicy"]
    assert sandbox["networkAccess"] is False
    assert sandbox["type"] == "workspaceWrite"
    assert sandbox["excludeSlashTmp"] is sandbox["excludeTmpdirEnvVar"] is True
    assert sandbox["writableRoots"][0] == candidate.repo_root
    assert "/tmp" not in sandbox["writableRoots"]
    assert params["env"]["TMPDIR"] == sandbox["writableRoots"][1]
    assert not Path(params["env"]["TMPDIR"]).exists()
    assert params["streamStdoutStderr"] is True
    assert client.approval_handler is deny_approval and client.closed
    assert observed[0]["invocation_id"] == "owned-command"
    assert "".join(value["text"] for value in observed if value["kind"] == "output") == "€"


def test_command_output_is_bounded_even_if_provider_exceeds_cap(candidate: Candidate) -> None:
    factory = Factory(command_chunks=[("stdout", b"x" * 2000, True)])
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_command(
            CheckSpec(name="check", argv=["test"]),
            candidate,
            Policy(max_output_bytes=1024),
        )
    )
    assert len(result.stdout.encode()) == 1024 and result.truncated


@pytest.mark.parametrize(
    "settings",
    [
        {"command_process_id": "wrong-process"},
        {"buffered_stdout": "unexpected"},
        {"runtime_version": "0.153.0"},
    ],
)
def test_command_rejects_unbound_or_incompatible_protocol(
    candidate: Candidate, settings: Any
) -> None:
    factory = Factory(**settings)
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_command(
            CheckSpec(name="check", argv=["test"]),
            candidate,
            Policy(),
            invocation_id="expected",
        )
    )
    assert not result.succeeded and result.error
    assert factory.clients[0].closed


def test_cwd_symlink_escape_is_rejected_before_provider_start(
    candidate: Candidate, tmp_path: Path
) -> None:
    (Path(candidate.repo_root) / "escape").symlink_to(tmp_path)
    factory = Factory()
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_command(
            CheckSpec(name="check", argv=["test"], cwd="escape"),
            candidate,
            Policy(),
        )
    )
    assert not result.succeeded and not factory.clients


def test_command_timeout_terminates_owned_process(candidate: Candidate) -> None:
    factory = Factory(block_command=True)
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_command(
            CheckSpec(name="check", argv=["wait"], timeout_seconds=1),
            candidate,
            Policy(),
            invocation_id="slow-command",
        )
    )
    client = factory.clients[0]
    assert result.timed_out and not result.succeeded and client.closed
    assert ("command/exec/terminate", {"processId": "slow-command"}) in client.requests


def test_review_has_strict_schema_isolated_tools_and_observed_ids(candidate: Candidate) -> None:
    factory = Factory()
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_review(
            "qa_engineer",
            candidate,
            {"evidence": []},
            Policy(),
            invocation_id="review-assigned",
        )
    )
    assert result.succeeded and result.thread_id and result.turn_id
    assert result.role == "qa_engineer" and result.invocation_id == "review-assigned"
    assert result.candidate_digest == candidate.candidate_digest
    client = factory.clients[0]
    start = next(payload for method, payload in client.requests if method == "thread/start")
    assert start["sandbox"] == "read-only" and start["approvalPolicy"] == "never"
    assert start["model"] == "gpt-5.6-terra"
    assert start["config"]["model_reasoning_effort"] == "high"
    assert start["config"]["mcp_servers"]["devgod"]["enabled"] is False
    assert start["config"]["plugins"]["other@local"]["enabled"] is False
    assert start["config"]["apps"]["configured"]["enabled"] is False
    assert start["config"]["features"] == {"apps": False, "multi_agent": False}
    assert "hooks" not in start["config"]["features"]
    assert client.config.env == {"DEVGOD_MANAGED_REVIEW": "1"}
    schema = _schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert set(schema["$defs"]["Finding"]["required"]) == set(
        schema["$defs"]["Finding"]["properties"]
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"review_output": "not JSON"},
        {"review_status": "interrupted"},
        {"review_status": "failed"},
        {
            "review_output": json.dumps(
                {"decision": "approve", "summary": "missing required fields"}
            )
        },
        {"review_output": json.dumps({"decision": "approve", "summary": "ok", "forged": True})},
        {
            "review_events": [
                event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})
            ]
        },
        {"review_events": [event("item/completed", {"item": {"type": "mcpToolCall"}})]},
    ],
)
def test_incomplete_or_invalid_reviews_never_pass(candidate: Candidate, settings: Any) -> None:
    factory = Factory(**settings)
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_review(
            "reviewer",
            candidate,
            {},
            Policy(),
            invocation_id="review-invalid",
        )
    )
    assert not result.succeeded and result.error and result.payload is None
    assert factory.clients[0].closed


@pytest.mark.parametrize(
    "settings",
    [
        {"tools": {"write_other_repo": {}}},
        {"inventory_error": "not available"},
        {"inventory_status": "starting"},
        {"inventory_status": "connected"},
        {"authenticated": False},
    ],
)
def test_unsafe_or_unavailable_reviewer_does_not_start_turn(
    candidate: Candidate, settings: Any
) -> None:
    factory = Factory(**settings)
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_review(
            "security_reviewer",
            candidate,
            {},
            Policy(),
        )
    )
    assert not result.succeeded
    assert all(method != "turn/start" for method, _ in factory.clients[0].requests)


def test_review_timeout_interrupts_provider_turn(candidate: Candidate) -> None:
    factory = Factory(review_events=[])
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_review(
            "reviewer",
            candidate,
            {},
            Policy(review_timeout_seconds=1),
        )
    )
    assert not result.succeeded and "time limit" in result.error
    assert any(method == "turn/interrupt" for method, _ in factory.clients[0].requests)
    assert factory.clients[0].closed


@pytest.mark.parametrize("role", ["reviewer", "qa_engineer", "security_reviewer"])
def test_review_defaults_to_terra_high_without_leaking_source_route(
    candidate: Candidate, role: str
) -> None:
    factory = Factory(source_model="gpt-5.5", source_reasoning_effort="low")
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_review(
            role,  # type: ignore[arg-type]
            candidate,
            {},
            Policy(),
        )
    )
    assert result.succeeded
    client = factory.clients[0]
    start = next(payload for method, payload in client.requests if method == "thread/start")
    assert start["model"] == "gpt-5.6-terra"
    assert start["config"]["model_reasoning_effort"] == "high"
    assert client.config.cwd == candidate.snapshot_path
    assert start["cwd"] == candidate.snapshot_path


@pytest.mark.parametrize("role", ["reviewer", "qa_engineer", "security_reviewer"])
def test_legacy_review_model_remains_the_all_role_high_fallback(
    candidate: Candidate, role: str
) -> None:
    factory = Factory(source_model="gpt-5.5", source_reasoning_effort="low")
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_review(
            role,  # type: ignore[arg-type]
            candidate,
            {},
            Policy(review_model="legacy-reviewer"),
        )
    )
    assert result.succeeded
    start = next(
        payload for method, payload in factory.clients[0].requests if method == "thread/start"
    )
    assert start["model"] == "legacy-reviewer"
    assert start["config"]["model_reasoning_effort"] == "high"


def test_explicit_review_route_overrides_legacy_model(candidate: Candidate) -> None:
    factory = Factory(source_model="gpt-5.5", source_reasoning_effort="low")
    result = asyncio.run(
        CodexAdapter(client_factory=factory).run_review(
            "security_reviewer",
            candidate,
            {},
            Policy(
                review_model="legacy-reviewer",
                review_routes={
                    "security_reviewer": ModelRoute(
                        model="gpt-5.6-sol", reasoning_effort="max"
                    )
                },
            ),
        )
    )
    assert result.succeeded
    start = next(
        payload for method, payload in factory.clients[0].requests if method == "thread/start"
    )
    assert start["model"] == "gpt-5.6-sol"
    assert start["config"]["model_reasoning_effort"] == "max"


def test_cancellation_during_startup_cannot_leak_late_transport(candidate: Candidate) -> None:
    factory = Factory(block_startup=True)

    async def scenario() -> None:
        adapter = CodexAdapter(client_factory=factory)
        task = asyncio.create_task(
            adapter.run_review(
                "reviewer",
                candidate,
                {},
                Policy(),
                invocation_id="startup-cancel",
            )
        )
        while not factory.clients:
            await asyncio.sleep(0)
        assert await asyncio.to_thread(factory.clients[0].startup_entered.wait, 1)
        await adapter.cancel("startup-cancel")
        assert not adapter.termination_confirmed("not-owned")
        assert not adapter.termination_confirmed("startup-cancel")
        factory.clients[0].startup_release.set()
        result = await task
        assert not result.succeeded
        assert factory.clients[0].closed
        assert not factory.clients[0].requests
        assert adapter.termination_confirmed("startup-cancel")
        await adapter.close()

    asyncio.run(scenario())


def test_concurrent_reviews_have_separate_clients(candidate: Candidate) -> None:
    factory = Factory()

    async def scenario() -> Any:
        adapter = CodexAdapter(client_factory=factory)
        try:
            return await asyncio.gather(
                *(
                    adapter.run_review(
                        role, candidate, {}, Policy(), invocation_id=f"review-{index}"
                    )
                    for index, role in enumerate(("reviewer", "qa_engineer", "security_reviewer"))
                )
            )
        finally:
            await adapter.close()

    results = asyncio.run(scenario())
    assert all(result.succeeded for result in results)
    assert len({result.thread_id for result in results}) == 3
    assert len(factory.clients) == 3 and all(client.closed for client in factory.clients)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper ownership contract")
@pytest.mark.parametrize("kill_supervisor", [False, True])
@pytest.mark.parametrize("force_libc", [False, True])
def test_subreaper_receipt_covers_detached_children(
    tmp_path: Path, kill_supervisor: bool, force_libc: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_bindings = ""
    if force_libc:
        monkeypatch.delattr(os, "pidfd_open", raising=False)
        monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
        missing_bindings = (
            "import os,signal\n"
            "if hasattr(os,'pidfd_open'): del os.pidfd_open\n"
            "if hasattr(signal,'pidfd_send_signal'): del signal.pidfd_send_signal\n"
        )
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import json,os,subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-I','-c',{child_code!r}],start_new_session=True); "
        "print(json.dumps({'primary':os.getpid(),'child':child.pid}),flush=True); time.sleep(30)"
    )
    receipt_dir = tmp_path / "control"
    receipt_dir.mkdir(mode=0o700)
    nonce = "c" * 32
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(
        missing_bindings
        + "import sys\nfrom pathlib import Path\nfrom devgod.launcher import supervise\n"
        f"raise SystemExit(supervise([sys.executable,'-I','-c',{parent_code!r}],Path({str(receipt_dir)!r}),{nonce!r}))\n"
    )
    owner = subprocess.Popen(
        [sys.executable, "-I", str(supervisor)],
        stdout=subprocess.PIPE,
        text=True,
    )
    children: dict[str, int] = {}
    child_handles: list[int] = []
    try:
        assert owner.stdout is not None
        children = json.loads(owner.stdout.readline())
        for pid in children.values():
            child_handles.append(pidfd_open(pid))
        identity = _read_process(owner.pid)
        assert identity is not None
        identity.pop("state")
        identity["boot_id"] = _boot_id()
        identity["nonce"] = nonce
        identity["receipt_path"] = str(receipt_dir / "stopped.json")
        assert identity["pid"] == identity["sid"] == identity["pgid"]
        if kill_supervisor:
            owner.kill()
            owner.wait(timeout=3)
            assert not asyncio.run(CodexAdapter().recover_termination(identity))
            assert not Path(identity["receipt_path"]).exists()
            return
        assert asyncio.run(CodexAdapter().recover_termination(identity))
        owner.wait(timeout=3)
        for pid in children.values():
            child = _read_process(pid)
            assert child is None or child["state"] in ("Z", "X")
        receipt = json.loads(Path(identity["receipt_path"]).read_text())
        assert receipt["descendants_reaped"] is True
        assert receipt["nonce"] == nonce
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=3)
        for descriptor in child_handles:
            try:
                pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(descriptor)


def test_recovery_rejects_pid_reuse_without_signalling(monkeypatch: pytest.MonkeyPatch) -> None:
    import devgod.codex_adapter as module

    identity = {"pid": 987654, "pgid": 987654, "sid": 987654, "start_ticks": 10, "boot_id": "boot"}
    identity.update(receipt_path="/tmp/absent-devgod-receipt/stopped.json", nonce="c" * 32)
    monkeypatch.setattr(module, "_boot_id", lambda: "boot")
    monkeypatch.setattr(
        module, "_read_process", lambda pid: {**identity, "start_ticks": 11, "state": "S"}
    )

    def forbidden(*args: Any) -> Any:
        raise AssertionError("must not signal a recycled PID")

    monkeypatch.setattr(module, "_signal_process", forbidden)
    assert not asyncio.run(CodexAdapter().recover_termination(identity))


def test_recovery_does_not_infer_parent_death_means_children_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devgod.codex_adapter as module

    identity = {"pid": 987654, "pgid": 987654, "sid": 987654, "start_ticks": 10, "boot_id": "boot"}
    identity.update(receipt_path="/tmp/absent-devgod-receipt/stopped.json", nonce="c" * 32)
    monkeypatch.setattr(module, "_boot_id", lambda: "boot")
    monkeypatch.setattr(module, "_read_process", lambda pid: None)
    monkeypatch.setattr(
        module, "_session_members", lambda sid: [{"pid": 987655, "sid": sid, "state": "S"}]
    )
    assert not asyncio.run(CodexAdapter().recover_termination(identity))


def test_recovery_requires_receipt_even_for_empty_session_except_prior_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devgod.codex_adapter as module

    identity = {"pid": 987654, "pgid": 987654, "sid": 987654, "start_ticks": 10, "boot_id": "boot"}
    identity.update(receipt_path="/tmp/absent-devgod-receipt/stopped.json", nonce="c" * 32)
    monkeypatch.setattr(module, "_boot_id", lambda: "boot")
    monkeypatch.setattr(module, "_read_process", lambda pid: None)
    monkeypatch.setattr(module, "_session_members", lambda sid: [])
    assert not asyncio.run(CodexAdapter().recover_termination(identity))
    monkeypatch.setattr(module, "_boot_id", lambda: "new-boot")
    assert asyncio.run(CodexAdapter().recover_termination(identity))
    assert not asyncio.run(CodexAdapter().recover_termination({"pid": 1}))


def test_receipt_root_rejects_repo_snapshot_and_symlink(
    candidate: Candidate, tmp_path: Path
) -> None:
    for root in (Path(candidate.repo_root) / "state", Path(candidate.snapshot_path) / "state"):
        adapter = CodexAdapter(client_factory=Factory(), receipt_root=root)
        with pytest.raises(AdapterError, match="outside"):
            adapter._new_invocation(
                "test", "review", Path(candidate.snapshot_path), repo_root=Path(candidate.repo_root)
            )
        assert not root.exists()
    target = tmp_path / "actual"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    adapter = CodexAdapter(client_factory=Factory(), receipt_root=link / "state")
    with pytest.raises(AdapterError, match="symlinks"):
        adapter._new_invocation("test", "command", Path(candidate.repo_root))


def test_production_runtime_inside_consumer_cannot_dispatch(
    candidate: Candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    import devgod.codex_adapter as module

    monkeypatch.setattr(
        module, "_runtime_paths", lambda: [Path(candidate.repo_root) / ".venv/sdk.py"]
    )
    adapter = CodexAdapter()
    with pytest.raises(AdapterError, match="external tool installation"):
        adapter._new_invocation("test", "command", Path(candidate.repo_root))
    assert not adapter._active


def test_unresolved_supervisor_releases_output_waiters_without_false_termination(
    candidate: Candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = Factory()
    adapter = CodexAdapter(client_factory=factory)
    original_start = adapter._start

    async def start(job: Any) -> None:
        await original_start(job)
        job.process_identity = {"observed": True}
        job.client._router = SimpleNamespace(
            fail_all=lambda error: job.client.global_events.put(error)
        )

    async def unresolved(identity: Any) -> bool:
        return False

    monkeypatch.setattr(adapter, "_start", start)
    monkeypatch.setattr(adapter, "recover_termination", unresolved)

    async def scenario() -> Any:
        async with asyncio.timeout(2):
            return await adapter.run_command(
                CheckSpec(name="check", argv=["true"]),
                candidate,
                Policy(),
                invocation_id="unresolved",
            )

    result = asyncio.run(scenario())
    assert not result.succeeded and result.error
    assert not adapter.termination_confirmed("unresolved")
    assert not factory.clients[0].closed


@pytest.mark.parametrize(
    "mutation", ["nonce", "reaped", "oversize", "symlink", "directory_mode", "fifo"]
)
def test_receipt_requires_exact_private_supervisor_evidence(tmp_path: Path, mutation: str) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    receipt_path = control / "stopped.json"
    identity = {
        "pid": 987654,
        "pgid": 987654,
        "sid": 987654,
        "start_ticks": 10,
        "boot_id": _boot_id(),
        "nonce": "c" * 32,
        "receipt_path": str(receipt_path),
    }
    receipt = {key: identity[key] for key in ("nonce", "pid", "start_ticks", "boot_id")}
    receipt["descendants_reaped"] = True
    receipt_path.write_text(json.dumps(receipt))
    assert CodexAdapter._receipt_valid(identity)
    if mutation == "nonce":
        receipt["nonce"] = "d" * 32
        receipt_path.write_text(json.dumps(receipt))
    elif mutation == "reaped":
        receipt["descendants_reaped"] = False
        receipt_path.write_text(json.dumps(receipt))
    elif mutation == "oversize":
        receipt_path.write_text(" " * 4097)
    elif mutation == "symlink":
        other = control / "other.json"
        receipt_path.rename(other)
        receipt_path.symlink_to(other)
    elif mutation == "directory_mode":
        control.chmod(0o755)
    else:
        receipt_path.unlink()
        os.mkfifo(receipt_path, mode=0o600)
    assert not CodexAdapter._receipt_valid(identity)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent death supervision contract")
def test_supervisor_reaps_detached_children_after_its_parent_dies(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    nonce = "e" * 32
    payload = (
        "import json,os,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(30)'],start_new_session=True); "
        "print(json.dumps({'primary':os.getpid(),'child':child.pid}),flush=True);time.sleep(30)"
    )
    script = tmp_path / "supervisor.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\nfrom devgod.launcher import supervise\n"
        f"raise SystemExit(supervise([sys.executable,'-I','-c',{payload!r}],Path({str(control)!r}),{nonce!r}))\n"
    )
    parent_code = (
        "import json,subprocess,sys,time;"
        f"supervisor=subprocess.Popen([sys.executable,'-I',{str(script)!r}]);"
        "print(json.dumps({'supervisor':supervisor.pid}),flush=True);time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-I", "-c", parent_code], stdout=subprocess.PIPE, text=True
    )
    processes: dict[str, int] = {}
    process_handles: list[int] = []
    try:
        assert parent.stdout is not None
        processes.update(json.loads(parent.stdout.readline()))
        processes.update(json.loads(parent.stdout.readline()))
        for pid in processes.values():
            process_handles.append(pidfd_open(pid))
        identity = _read_process(processes["supervisor"])
        assert identity is not None
        identity.pop("state")
        identity.update(boot_id=_boot_id(), nonce=nonce, receipt_path=str(control / "stopped.json"))
        parent.kill()
        parent.wait(timeout=3)

        async def observe() -> None:
            async with asyncio.timeout(3):
                while not CodexAdapter._receipt_valid(identity):
                    await asyncio.sleep(0.02)

        asyncio.run(observe())
        for pid in processes.values():
            process = _read_process(pid)
            if pid != processes["supervisor"]:
                assert process is None or process["state"] in ("Z", "X")
        assert asyncio.run(CodexAdapter().recover_termination(identity))
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        for descriptor in process_handles:
            try:
                pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(descriptor)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel PID handles")
@pytest.mark.parametrize("missing", [(), ("open",), ("open", "signal")])
def test_pid_handles_remain_available_without_optional_python_bindings(
    monkeypatch: pytest.MonkeyPatch, missing: tuple[str, ...]
) -> None:
    from devgod.launcher import pidfd_open, pidfd_send_signal, pidfd_supported

    if "open" in missing:
        monkeypatch.delattr(os, "pidfd_open", raising=False)
    if "signal" in missing:
        monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    assert pidfd_supported()
    descriptor = pidfd_open(os.getpid())
    try:
        assert not os.get_inheritable(descriptor)
        pidfd_send_signal(descriptor, 0)
    finally:
        os.close(descriptor)
    with pytest.raises(OSError) as raised:
        pidfd_send_signal(descriptor, 0)
    assert raised.value.errno == errno.EBADF
    capabilities = asyncio.run(CodexAdapter().capabilities())
    assert capabilities["available"]
    assert capabilities["crash_recovery"] == "linux-subreaper-receipt"


def test_pid_handle_native_bindings_take_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    import devgod.launcher as module

    calls: list[tuple[Any, ...]] = []

    def native_open(pid: int, flags: int) -> int:
        calls.append(("open", pid, flags))
        return 91

    def fallback_forbidden(*args: Any) -> Any:
        raise AssertionError("native binding must not try libc fallback")

    monkeypatch.setattr(os, "pidfd_open", native_open, raising=False)
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda fd, sig: calls.append(("signal", fd, sig)),
        raising=False,
    )
    monkeypatch.setattr(module, "_libc_function", fallback_forbidden)
    assert module.pidfd_open(123) == 91
    module.pidfd_send_signal(91, 0)
    assert calls == [("open", 123, 0), ("signal", 91, 0)]


@pytest.mark.parametrize("error", [errno.ENOSYS, errno.EPERM, errno.ESRCH])
@pytest.mark.parametrize("operation", ["open", "signal"])
def test_pid_handle_libc_preserves_kernel_errors(
    monkeypatch: pytest.MonkeyPatch, error: int, operation: str
) -> None:
    import devgod.launcher as module

    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)

    def fail(*args: Any) -> int:
        ctypes.set_errno(error)
        return -1

    monkeypatch.setattr(module, "_libc_function", lambda *args: fail)
    with pytest.raises(OSError) as raised:
        if operation == "open":
            module.pidfd_open(123)
        else:
            module.pidfd_send_signal(91, 0)
    assert raised.value.errno == error


def test_pid_handle_probe_fails_closed_when_libc_symbols_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devgod.launcher as module

    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: object())
    assert not module.pidfd_supported()


def test_pid_handle_probe_does_not_retry_denied_native_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devgod.launcher as module

    def denied(*args: Any) -> Any:
        raise PermissionError(errno.EPERM, "probe denied")

    def fallback_forbidden(*args: Any) -> Any:
        raise AssertionError("a denied syscall must not try an alternative")

    monkeypatch.setattr(os, "pidfd_open", denied, raising=False)
    monkeypatch.setattr(module, "_libc_function", fallback_forbidden)
    assert not module.pidfd_supported()


def test_missing_kernel_handles_prevent_managed_child_start(
    candidate: Candidate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import devgod.codex_adapter as adapter_module
    import devgod.launcher as launcher_module

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not reach runtime or subprocess launch")

    monkeypatch.setattr(adapter_module, "pidfd_supported", lambda: False)
    monkeypatch.setattr(adapter_module, "_runtime_paths", forbidden)
    monkeypatch.setattr(launcher_module, "pidfd_supported", lambda: False)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(AdapterError, match="kernel PID handles"):
        CodexAdapter()._new_invocation("test", "command", Path(candidate.repo_root))
    with pytest.raises(OSError, match="Kernel PID handles"):
        launcher_module.supervise(["unused"], tmp_path, "a" * 32)
    capabilities = asyncio.run(CodexAdapter().capabilities())
    assert not capabilities["available"]
    assert capabilities["crash_recovery"] == "unsupported"
