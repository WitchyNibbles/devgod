"""Native MCP tools backed by one durable kernel and one asynchronous lifespan."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .models import (
    AcceptanceCriterion,
    CheckSpec,
    Digest,
    Identifier,
    Policy,
    ShortText,
    TaskSpec,
    Text,
)

_LOG = logging.getLogger(__name__)
_TERMINAL_JOBS = {"succeeded", "failed", "interrupted", "cancelled"}


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PermissionError):
        code = "permission_denied"
        action = "Report the concrete denied action through the existing host permission boundary."
    elif isinstance(exc, (ValueError, KeyError)):
        code = "invalid_request"
        action = "Inspect status, correct the request using the recorded plan, and retry automatically."
    elif isinstance(exc, OSError):
        code = "runtime_unavailable"
        action = "Inspect doctor and repair the local runtime; preserve existing run evidence."
    else:
        code = "internal_error"
        action = "Inspect status and the runtime diagnostic, repair the failure, then resume the run."
    return {"error": {"code": code, "message": str(exc)[:4096], "next_action": action}}


@dataclass
class Runtime:
    workspace: Any
    store: Any
    service: Any
    runner: Any
    adapter: Any

    async def close(self) -> None:
        # The owning event loop remains alive until every managed task has stopped.
        try:
            await self.runner.close()
        finally:
            try:
                await self.adapter.close()
            finally:
                self.store.close()


def open_runtime(repo: str | Path, state_home: str | Path | None = None) -> Runtime:
    """Compose a runtime on the caller's thread; call close on its event loop."""
    from .codex_adapter import CodexAdapter
    from .service import DevGodService
    from .store import Store
    from .verification import VerificationRunner
    from .workspace import Workspace

    workspace = Workspace(repo, state_root=state_home)
    store = Store(workspace.state_dir)
    try:
        adapter = CodexAdapter(receipt_root=workspace.state_dir / "supervisors")
        runner = VerificationRunner(workspace, store, adapter)
        service = DevGodService(workspace, store=store, verification=runner)
        return Runtime(workspace, store, service, runner, adapter)
    except BaseException:
        store.close()
        raise


def selected_run(service: Any, run_id: str | None) -> str:
    if run_id is not None:
        return run_id
    status = service.status()
    run = status.get("run")
    if not run:
        raise ValueError("No active run. Start the accepted goal with run_start before recording work.")
    return run["run_id"]


async def wait_for_job(service: Any, job_id: str, timeout_seconds: float = 30) -> dict[str, Any]:
    """A bounded observation only: timeout does not cancel the owned execution."""
    if not 0 <= timeout_seconds <= 60:
        raise ValueError("Wait timeout must be between 0 and 60 seconds")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        result = jsonable(service.verification_status(job_id))
        job = result.get("job", result)
        remaining = deadline - asyncio.get_running_loop().time()
        if job.get("state", job.get("status")) in _TERMINAL_JOBS or remaining <= 0:
            return result
        await asyncio.sleep(min(0.2, remaining))


def create_server(
    repo: str | Path,
    state_home: str | Path | None = None,
    *,
    runtime_factory: Any = open_runtime,
) -> MCPServer:
    """Create a stdio server; verification tasks outlive individual tool calls."""
    current: Runtime | None = None

    @asynccontextmanager
    async def lifespan(server: MCPServer):
        nonlocal current
        current = runtime_factory(repo, state_home)
        try:
            yield current
        finally:
            await current.close()
            current = None

    server = MCPServer(
        "DevGod",
        instructions=(
            "Manage accepted engineering work in the existing Codex conversation. "
            "Internal bookkeeping is automatic. Use native specialists for implementation, "
            "then verify to dispatch sandboxed checks and independent review. Only a current "
            "kernel verified result establishes the review-ready local delivery branch."
        ),
        lifespan=lifespan,
    )

    def runtime() -> Runtime:
        if current is None:
            raise RuntimeError("DevGod MCP lifespan has not started")
        return current

    def tool(*, read_only: bool = False, idempotent: bool = False,
             destructive: bool = False, open_world: bool = False):
        def decorate(function):
            @functools.wraps(function)
            async def guarded(*args, **kwargs):
                try:
                    return jsonable(await function(*args, **kwargs))
                except Exception as exc:
                    if not isinstance(exc, (ValueError, KeyError, OSError)):
                        _LOG.exception("DevGod tool failed: %s", function.__name__)
                    raise ToolError(json.dumps(error_payload(exc), ensure_ascii=False)) from exc

            return server.tool(annotations=ToolAnnotations(
                read_only_hint=read_only,
                destructive_hint=destructive,
                idempotent_hint=idempotent,
                open_world_hint=open_world,
            ))(guarded)
        return decorate

    @tool()
    async def run_start(
        goal: Text,
        acceptance: Annotated[list[AcceptanceCriterion], Field(min_length=1, max_length=256)],
        tasks: Annotated[list[TaskSpec], Field(max_length=4096)] = [],
        checks: Annotated[list[CheckSpec], Field(max_length=256)] = [],
        decisions: Annotated[dict[ShortText, Text], Field(max_length=256)] = {},
        branch: Annotated[str, Field(max_length=256)] | None = None,
        policy: Policy | None = None,
    ) -> dict[str, Any]:
        """Record the accepted design and create a delivery branch preserving existing work."""
        return runtime().service.start(goal, acceptance, tasks, checks,
                                       decisions=decisions, branch=branch, policy=policy)

    @tool()
    async def plan(
        tasks: Annotated[list[TaskSpec], Field(max_length=4096)] | None = None,
        checks: Annotated[list[CheckSpec], Field(max_length=256)] | None = None,
        run_id: Identifier | None = None,
    ) -> dict[str, Any]:
        """Add or amend tasks and accepted checks; changed configuration requires fresh verification."""
        service = runtime().service
        return service.plan(selected_run(service, run_id), tasks or [], checks=checks)

    @tool()
    async def task_update(
        task_id: Identifier,
        state: Literal["planned", "implementing", "verifying", "repair", "blocked"],
        summary: Annotated[str, Field(max_length=16_384)] = "",
        run_id: Identifier | None = None,
    ) -> dict[str, Any]:
        """Record native implementation progress. Verifying claims readiness; it cannot grant verification."""
        service = runtime().service
        return service.task_update(selected_run(service, run_id), task_id, state, summary=summary)

    @tool()
    async def checkpoint(
        summary: Text,
        decisions: Annotated[dict[ShortText, Text], Field(max_length=256)] = {},
        next_actions: Annotated[list[Text], Field(max_length=256)] = [],
        run_id: Identifier | None = None,
    ) -> dict[str, Any]:
        """Persist concise continuation context at task/review boundaries and before compaction."""
        service = runtime().service
        return service.checkpoint(selected_run(service, run_id),
                                  {"summary": summary, "decisions": decisions, "next_actions": next_actions})

    @tool(idempotent=True)
    async def status(run_id: Identifier | None = None) -> dict[str, Any]:
        """Refresh current evidence freshness and return run, tasks, jobs, and the concrete next action."""
        return runtime().service.status(run_id)

    @tool(idempotent=True)
    async def next(run_id: Identifier | None = None) -> dict[str, Any]:
        """Read the deterministic next action for continued native manager work."""
        return runtime().service.next_action(run_id)

    @tool(idempotent=True, open_world=True)
    async def verify(run_id: Identifier | None = None) -> dict[str, Any]:
        """Start bounded sandboxed checks and three independent reviewers; return a durable job ID promptly."""
        return await runtime().service.verify(run_id)

    @tool(idempotent=True)
    async def verification_status(job_id: Identifier) -> dict[str, Any]:
        """Read an existing verification attempt without dispatching work."""
        return runtime().service.verification_status(job_id)

    @tool(idempotent=True)
    async def wait(
        job_id: Identifier,
        timeout_seconds: Annotated[float, Field(ge=0, le=60)] = 30,
    ) -> dict[str, Any]:
        """Wait at most 60 seconds for a job; timeout leaves the verification running."""
        return await wait_for_job(runtime().service, job_id, timeout_seconds)

    @tool()
    async def resume(run_id: Identifier | None = None) -> dict[str, Any]:
        """Reconcile interrupted work and return the next automatic recovery action."""
        return runtime().service.resume(run_id)

    @tool(open_world=True)
    async def recover(
        job_id: Identifier,
        attempt: Annotated[int, Field(ge=1, strict=True)],
        candidate_digest: Digest,
        checks_digest: Digest,
        observations: Annotated[str, Field(min_length=1, max_length=16_384)],
    ) -> dict[str, Any]:
        """After inspecting interrupted effects, retry a stopped current attempt with fresh evidence; cannot approve work."""
        return await runtime().service.recover(
            job_id, attempt, candidate_digest, checks_digest, observations,
        )

    @tool(destructive=True)
    async def cancel(run_id: Identifier | None = None) -> dict[str, Any]:
        """Stop owned jobs for this run and preserve its implementation and recorded evidence."""
        return await runtime().service.cancel(run_id)

    return server
