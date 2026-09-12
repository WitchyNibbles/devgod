"""MCP protocol and lifecycle tests; fake dispatch is labeled and never live proof."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return environment


def _data(result) -> dict:
    content = result.structured_content
    if content is not None:
        return content
    return json.loads(result.content[0].text)


def test_actual_stdio_initialize_schema_and_kernel_calls(tmp_path: Path) -> None:
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "baseline"], check=True)

    async def exercise() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "devgod", "--repo", str(repo), "--state-home", str(tmp_path / "state"), "mcp"],
            env=_environment(),
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams, read_timeout_seconds=10) as client:
                initialized = await client.initialize()
                assert initialized.server_info.name == "DevGod"
                listing = await client.list_tools()
                tools = {tool.name: tool for tool in listing.tools}
                assert {"run_start", "plan", "task_update", "checkpoint", "status", "next", "verify",
                        "verification_status", "wait", "resume", "recover", "cancel"} == set(tools)
                # Status may revoke stale evidence or persist a newly observed
                # gate, so its annotation declares the small internal mutation.
                assert tools["status"].annotations.read_only_hint is False
                assert tools["run_start"].annotations.read_only_hint is False
                assert tools["verify"].annotations.open_world_hint is True
                assert tools["recover"].annotations.open_world_hint is True
                invalid_recovery = await client.call_tool("recover", {
                    "job_id": "missing", "attempt": 0, "candidate_digest": "not-a-digest",
                    "checks_digest": "0" * 64, "observations": "Inspected effects",
                })
                assert invalid_recovery.is_error
                assert "verified" not in json.dumps(tools["task_update"].input_schema)
                schema = tools["run_start"].input_schema
                assert "AcceptanceCriterion" in schema["$defs"]
                assert schema["$defs"]["AcceptanceCriterion"]["additionalProperties"] is False
                empty = await client.call_tool("status", {})
                assert not empty.is_error
                assert isinstance(_data(empty), dict)
                started = await client.call_tool("run_start", {
                    "goal": "Implement a greeting", "acceptance": [
                        {"acceptance_id": "AC-1", "description": "A greeting is available"}],
                    "tasks": [{"task_id": "greeting", "title": "Greeting", "owner_role": "implementer",
                               "acceptance": ["AC-1"], "allowed_paths": ["hello.py"]}],
                })
                assert not started.is_error, started
                status = _data(await client.call_tool("status", {}))
                assert status["run"]["spec"]["goal"] == "Implement a greeting"
                assert status["tasks"][0]["task_id"] == "greeting"
                rejected = await client.call_tool("task_update", {
                    "task_id": "greeting", "state": "verified"})
                assert rejected.is_error
                traversal = await client.call_tool("plan", {"tasks": [{
                    "task_id": "escape", "title": "Escape", "acceptance": ["AC-1"],
                    "allowed_paths": ["../outside.py"]}]})
                assert traversal.is_error

    asyncio.run(exercise())


def test_stdio_lifespan_preserves_background_job_and_closes_it(tmp_path: Path) -> None:
    """Synthetic timer proves MCP task lifetime only, not actual check/review execution."""
    marker = tmp_path / "closed"
    script = tmp_path / "synthetic_mcp.py"
    script.write_text(
        """import asyncio
from pathlib import Path
from types import SimpleNamespace
from devgod.mcp_server import create_server

class SyntheticService:
    def __init__(self):
        self.job = {'job_id': 'job_demo', 'state': 'queued'}
        self.task = None
    async def verify(self, run_id=None):
        async def work():
            self.job['state'] = 'running'
            await asyncio.sleep(0.3)
            self.job['state'] = 'succeeded'
        self.task = asyncio.create_task(work())
        return dict(self.job)
    def verification_status(self, job_id):
        return dict(self.job)
    def status(self, run_id=None):
        return {'alive': True}

class SyntheticRuntime:
    def __init__(self):
        self.service = SyntheticService()
    async def close(self):
        if self.service.task is not None and not self.service.task.done():
            self.service.task.cancel()
            await asyncio.gather(self.service.task, return_exceptions=True)
        Path(MARKER).write_text('closed')

create_server('.', runtime_factory=lambda *args: SyntheticRuntime()).run()
""".replace("MARKER", repr(str(marker)))
    )

    async def exercise() -> None:
        parameters = StdioServerParameters(command=sys.executable, args=[str(script)], env=_environment())
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams, read_timeout_seconds=10) as client:
                await client.initialize()
                started = _data(await client.call_tool("verify", {}))
                assert started["state"] == "queued"
                immediate = _data(await client.call_tool("wait", {"job_id": "job_demo", "timeout_seconds": 0}))
                assert immediate["state"] == "running"
                # A second unrelated request while verification runs proves the
                # first request is no longer holding up the tool interface.
                status = _data(await client.call_tool("status", {}))
                assert status["alive"]
                completed = _data(await client.call_tool("wait", {"job_id": "job_demo", "timeout_seconds": 2}))
                assert completed["state"] == "succeeded"
    asyncio.run(exercise())
    assert marker.read_text() == "closed"
