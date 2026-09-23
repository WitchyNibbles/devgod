"""Exercise installed DevGod through an actual native Codex manager thread.

Run this with the clean wheel venv produced by package_smoke.py. The test client
explicitly supplies only the generated DevGod MCP connection in thread config.
Codex's native workspace-write thread startup records project trust in a private
temporary CODEX_HOME. Shared configuration and credentials are hash checked.
This establishes native tools/delegation + live MCP delivery, not first-use UI
trust or lifecycle-hook behavior. A private copy of the existing login is removed
after the exercise.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def plain(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value


FINAL_HANDOFF_SECTIONS = ("Outcome", "Changes", "Verification", "Agents", "Limitations")
FINAL_HANDOFF_FAILURE_LIMIT = 500
FINAL_HANDOFF_REPORT_LIMIT = 16_384


def final_handoff_report(message: str) -> dict[str, Any]:
    """Return bounded report evidence while preserving full-message identity."""
    return {
        "final_message": message[:FINAL_HANDOFF_REPORT_LIMIT],
        "final_message_truncated": len(message) > FINAL_HANDOFF_REPORT_LIMIT,
        "final_message_sha256": hashlib.sha256(message.encode()).hexdigest(),
    }


def validate_turn_completion(payload: Any, expected_turn_id: str) -> str | None:
    """Return a bounded failure reason for an invalid root-turn completion."""
    if not isinstance(payload, dict):
        return "Native manager completion payload was not an object."
    completed = payload.get("turn")
    if not isinstance(completed, dict) or completed.get("id") != expected_turn_id:
        return "Native manager completion identity did not match the requested turn."
    if completed.get("status") != "completed" or completed.get("error"):
        return "Native manager turn failed or was interrupted."
    return None


def validate_final_handoff(message: str) -> tuple[bool, str | None]:
    """Validate the manager's user-facing terminal handoff without runtime state."""
    if not isinstance(message, str) or not message.strip():
        return False, "No completed root agentMessage was captured."

    heading_pattern = re.compile(
        r"(?im)^#{1,6}\s+(Outcome|Changes|Verification|Agents|Limitations)\s*:?\s*$"
    )
    matches = list(heading_pattern.finditer(message))
    found = [match.group(1) for match in matches]
    failures: list[str] = []
    if found != list(FINAL_HANDOFF_SECTIONS):
        failures.append(
            "Expected terminal sections in order: " + ", ".join(FINAL_HANDOFF_SECTIONS) + "."
        )

    bodies: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(message)
        bodies[match.group(1)] = message[match.end():end].strip()
    for section in FINAL_HANDOFF_SECTIONS:
        if not bodies.get(section):
            failures.append(f"Section {section} must contain concrete content.")

    changes = bodies.get("Changes", "")
    missing_fixture_files = [
        filename for filename in ("greetings.py", "welcome.py") if filename not in changes
    ]
    if missing_fixture_files:
        failures.append("Changes must name changed fixture files: " + ", ".join(missing_fixture_files) + ".")

    verification = bodies.get("Verification", "")
    accepted_check = "/usr/bin/python3 -m unittest discover -v"
    if accepted_check not in verification:
        failures.append("Verification must name the accepted unittest command.")
    positive_result = re.search(
        r"(?i)\b(?:OK|passed|exit(?:ed)?(?:\s+code)?\s*[:=]?\s*0|success(?:ful)?)\b",
        verification,
    )
    negative_result = re.search(
        r"(?i)\b(?:not\s+(?:passed|successful|ok|succeeded)|"
        r"did(?:\s+not|n't)\s+(?:pass|succeed)|unsuccessful|failed|failure|nonzero|"
        r"exit(?:ed)?(?:\s+code)?\s*[:=]?\s*[1-9]\d*)\b",
        verification,
    )
    if not positive_result or negative_result:
        failures.append("Verification must state the accepted check result.")
    if not re.search(r"(?i)\bbranch\b", verification) or not re.search(
        r"(?i)\bverified\b", verification
    ):
        failures.append("Verification must state the branch and verified kernel status.")

    agents = bodies.get("Agents", "")
    planning_entry = re.search(
        r"(?im)^\s*[-*]?\s*(?:native\s+)?(?:architect|planner|planning\s+lead|sol\s+lead)\s*:\s*"
        r".*\b(?:completed|concluded|planned|recommended|approved|done)\b.*$",
        agents,
    )
    implementation_entry = re.search(
        r"(?im)^\s*[-*]?\s*(?:native\s+)?(?:implementer|implementation\s+specialist|worker|"
        r"developer|luna\s+worker)\s*:\s*"
        r".*\b(?:completed|concluded|implemented|changed|passed|done)\b.*$",
        agents,
    )
    if not re.search(r"(?i)\bnative\b", agents) or not planning_entry or not implementation_entry:
        failures.append(
            "Agents must give separate native Planner and Implementer entries with conclusions."
        )

    if failures:
        return False, " ".join(failures)[:FINAL_HANDOFF_FAILURE_LIMIT]
    return True, None


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or isinstance(value, float) and math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{json.dumps(key)} = {_toml_value(item)}" for key, item in value.items()
        ) + " }"
    raise ValueError("Unsupported model-provider setting type")


def _file_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _process_role(pid: int, root_pid: int, argv: list[bytes]) -> str:
    if pid == root_pid:
        return "native_app_server"
    if b"devgod" in argv and b"mcp" in argv:
        return "devgod_mcp"
    if any(arg.endswith(b"/launcher.py") for arg in argv):
        return "verification_supervisor"
    if b"app-server" in argv:
        return "verification_app_server"
    return "other"


class PrivateCodexHome:
    """Confine test-runtime trust and credential writes to an expendable directory."""

    def __init__(self, shared_home: Path | None = None, excluded_root: Path | None = None):
        self.shared_home = shared_home or Path(
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        )
        self.path: Path | None = None
        self.excluded_root = excluded_root.resolve() if excluded_root is not None else None
        self.settings: dict[str, Any] = {}
        self.before: dict[str, str | None] = {}
        self.evidence: dict[str, Any] = {}

    def hashes(self) -> dict[str, str | None]:
        return {name: _file_hash(self.shared_home / name) for name in ("config.toml", "auth.json")}

    def __enter__(self) -> PrivateCodexHome:
        for name in ("config.toml", "auth.json"):
            if (self.shared_home / name).is_symlink():
                raise ValueError("Smoke configuration and credential sources must be regular files")
        self.before = self.hashes()
        self.path = Path(tempfile.mkdtemp(prefix="devgod-smoke-codex-", dir=Path("/tmp").resolve()))
        self.path.chmod(0o700)
        try:
            if self.excluded_root is not None and self.path.is_relative_to(self.excluded_root):
                raise ValueError("Private credentials must remain outside the smoke report directory")
            source = self.shared_home / "config.toml"
            parsed = tomllib.loads(source.read_text()) if source.is_file() else {}
            self.settings = {
                key: parsed[key] for key in ("model", "model_reasoning_effort", "model_provider")
                if key in parsed
            }
            provider = parsed.get("model_provider", "openai")
            providers = parsed.get("model_providers", {})
            if provider in providers:
                self.settings["model_providers"] = {provider: providers[provider]}
            # A copied file cache must not fall back to a shared system keyring.
            self.settings["cli_auth_credentials_store"] = "file"
            encoded = "\n".join(
                f"{json.dumps(key)} = {_toml_value(value)}" for key, value in self.settings.items()
            ) + "\n"
            tomllib.loads(encoded)
            self._write("config.toml", encoded.encode())
            auth = self.shared_home / "auth.json"
            if auth.is_file():
                self._write("auth.json", auth.read_bytes())
            return self
        except BaseException:
            self.cleanup()
            raise

    def _write(self, name: str, data: bytes) -> None:
        assert self.path is not None
        descriptor = os.open(self.path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)

    def capture(self, repo: Path) -> None:
        assert self.path is not None
        config = self.path / "config.toml"
        parsed = tomllib.loads(config.read_text()) if config.is_file() else {}
        registration = parsed.get("projects", {}).get(str(repo), {})
        self.evidence.update(
            private_home=str(self.path),
            copied_setting_names=sorted(self.settings),
            shared_hashes_before=self.before,
            shared_hashes_after=self.hashes(),
            private_config_sha256=_file_hash(config),
            native_private_project_registered=registration.get("trust_level") == "trusted",
            private_home_outside_consumer=not self.path.is_relative_to(repo),
            private_home_outside_report=not self.path.is_relative_to(repo.parent),
        )

    def cleanup(self) -> None:
        if self.path is not None:
            try:
                shutil.rmtree(self.path)
            finally:
                self.evidence["private_home_removed"] = not self.path.exists()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.cleanup()


async def exercise(output: Path, timeout: int) -> dict[str, Any]:
    isolated = PrivateCodexHome(excluded_root=output)
    report: dict[str, Any] = {"status": "failed", "delivery_status": "failed"}
    try:
        with isolated:
            try:
                report = await _exercise(output, timeout, isolated)
            finally:
                isolated.capture(output / "consumer")
    finally:
        report["configuration_isolation"] = isolated.evidence
        after = isolated.hashes()
        report["global_config_hashes"] = {
            "before": isolated.before.get("config.toml"), "after": after["config.toml"],
        }
        report["global_auth_hashes"] = {
            "before": isolated.before.get("auth.json"), "after": after["auth.json"],
        }
        report["global_config_modified"] = after["config.toml"] != isolated.before.get("config.toml")
        report["global_auth_modified"] = after["auth.json"] != isolated.before.get("auth.json")
        if report["global_config_modified"] or report["global_auth_modified"]:
            report.update(status="failed", error="Shared Codex configuration or credentials changed")
        if not all(isolated.evidence.get(key) for key in (
            "private_home_removed", "private_home_outside_consumer", "private_home_outside_report",
        )):
            report.update(status="failed", error="Private credential isolation or cleanup was not verified")
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


async def _exercise(output: Path, timeout: int, isolated: PrivateCodexHome) -> dict[str, Any]:
    from openai_codex import CodexConfig
    from openai_codex.client import CodexClient

    import devgod
    from devgod.codex_adapter import deny_approval

    repo = output / "consumer"
    repo.mkdir()
    state = output / "state"
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    env.update(
        CODEX_HOME=str(isolated.path), XDG_STATE_HOME=str(state),
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="DevGod Native Smoke", GIT_AUTHOR_EMAIL="smoke@example.invalid",
        GIT_COMMITTER_NAME="DevGod Native Smoke", GIT_COMMITTER_EMAIL="smoke@example.invalid",
    )

    def run(argv: list[str]) -> str:
        result = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(f"Fixture command failed: {argv!r}: {result.stderr[-4000:]}")
        return result.stdout

    (repo / "README.md").write_text("# Native DevGod fixture\n\nA Python standard-library exercise.\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    git = ["git", "-c", f"core.hooksPath={os.devnull}"]
    run([*git, "init", "--initial-branch=main"])
    run([*git, "add", "."])
    run([*git, "commit", "-m", "Native smoke baseline"])
    cli = [sys.executable, "-I", "-m", "devgod", "--repo", str(repo), "--state-home", str(state), "--json"]
    setup = json.loads(run([*cli, "init"]))
    config = tomllib.loads((repo / ".codex/config.toml").read_text())
    generated_servers = config["mcp_servers"]
    for server in generated_servers.values():
        server.setdefault("env", {})["CODEX_HOME"] = str(isolated.path)
    server_name = setup["server"]
    assert set(generated_servers) == {server_name}
    skill = repo / setup["skill_path"]
    request = (
        "$devgod-manager\n"
        "This repository is an intentionally installed DevGod integration test. "
        "Implement and verify this fully specified goal without further product questions: "
        "Task one creates greetings.py with greeting(name: str) -> str. Strip surrounding "
        "whitespace, use World for an empty/whitespace-only name, and return Hello, NAME! "
        "Task two depends on task one and creates welcome.py with welcome(names: list[str]) "
        "-> str, joining greeting(name) for each input using newline characters; empty input "
        "returns an empty string. Add standard-library unittest regression tests for both "
        "functions including all stated edge cases. These are acceptance IDs A1 and A2 "
        "respectively. No dependencies, publication, commits, or production changes. "
        "Use the installed DevGod manager skill and real connected MCP tools to record "
        "the run, two dependent tasks, checkpoints, and verification; delegate actual "
        "implementation to native Codex specialists. Read actual MCP tool schemas. "
        "Use /usr/bin/python3 -m unittest discover -v as the accepted deterministic check. "
        "Use the MCP wait/status tools while verification runs, repair any findings, and "
        "finish only after the kernel reports the current local branch verified. "
        "Work autonomously through completion in this single turn; do not stop for "
        "step-by-step next-step updates. End with Markdown sections in this exact order: "
        "Outcome, Changes, Verification, Agents, Limitations. Name greetings.py and "
        "welcome.py under Changes; under Verification give the accepted command, its "
        "result, and the verified branch/status; under Agents give each native agent "
        "role and conclusion as separate `Planner:` and `Implementer:` entries. "
        "Do not edit DevGod state/evidence or fabricate receipts. Do not grant hook trust. "
        "If the native host cannot provide a needed capability, describe the concrete limit."
    )
    (output / "request.txt").write_text(request + "\n")
    approvals: list[str] = []

    def deny(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        approvals.append(method)
        return deny_approval(method, params)

    client = CodexClient(
        CodexConfig(cwd=str(repo), client_name="devgod_native_smoke", env={
            "CODEX_HOME": str(isolated.path), "XDG_STATE_HOME": str(state),
        }),
        approval_handler=deny,
    )
    installed_artifact = json.loads(
        importlib.metadata.distribution("devgod").read_text("direct_url.json") or "null"
    )
    installed_wheel_sha256 = None
    if isinstance(installed_artifact, dict):
        location = urlparse(installed_artifact.get("url", ""))
        if location.scheme == "file":
            wheel_path = Path(unquote(location.path))
            if wheel_path.suffix == ".whl" and wheel_path.is_file():
                installed_wheel_sha256 = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    report: dict[str, Any] = {
        "status": "incomplete", "scope": "native manager with explicit test-client MCP config",
        "repo": str(repo), "state": str(state), "package_import": str(devgod.__file__),
        "installed_artifact": installed_artifact,
        "installed_wheel_sha256": installed_wheel_sha256,
        "setup": setup, "hook_trust_granted": False,
        "client_mcp_override": True, "native_skill": str(skill), "approval_requests": approvals,
        "mcp_private_codex_home_explicit": True,
        "hook_lifecycle_and_first_use_ui_trust_tested": False,
    }
    observed_items: dict[str, dict[str, Any]] = {}
    completed_root_messages: list[str] = []
    process_evidence: dict[tuple[int, str], dict[str, Any]] = {}
    monitor: asyncio.Task[None] | None = None

    async def monitor_processes(root_pid: int) -> None:
        while True:
            entries: dict[int, tuple[int, str]] = {}
            for path in Path("/proc").iterdir():
                if not path.name.isdigit():
                    continue
                try:
                    fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                    entries[int(path.name)] = (int(fields[1]), fields[19])
                except (OSError, ValueError, IndexError):
                    continue
            descendants = {root_pid}
            while True:
                expanded = descendants | {pid for pid, (ppid, _) in entries.items() if ppid in descendants}
                if expanded == descendants:
                    break
                descendants = expanded
            for pid in descendants:
                if pid not in entries:
                    continue
                path = Path("/proc") / str(pid)
                try:
                    argv = (path / "cmdline").read_bytes().split(b"\0")
                    role = _process_role(pid, root_pid, argv)
                    if role == "other":
                        continue
                    home = next((entry.removeprefix(b"CODEX_HOME=") for entry in (path / "environ").read_bytes().split(b"\0") if entry.startswith(b"CODEX_HOME=")), None)
                    process_evidence[(pid, entries[pid][1])] = {
                        "pid": pid, "parent_pid": entries[pid][0], "start_ticks": entries[pid][1],
                        "role": role, "private_codex_home_matches": home == os.fsencode(isolated.path),
                    }
                except OSError:
                    continue
            await asyncio.sleep(0.5)

    started = time.monotonic()
    try:
        await asyncio.to_thread(client.start)
        process = client._proc
        if process is not None:
            report["native_app_server_pid"] = process.pid
            executable = Path(f"/proc/{process.pid}/exe").resolve()
            report["native_app_server_executable"] = str(executable)
            report["native_app_server_executable_sha256"] = _file_hash(executable)
            monitor = asyncio.create_task(monitor_processes(process.pid))
        initialized = await asyncio.to_thread(client.initialize)
        report["server"] = plain(initialized)
        thread = await asyncio.to_thread(client.thread_start, {
            "cwd": str(repo), "sandbox": "workspace-write", "approvalPolicy": "never",
            "config": {"mcp_servers": generated_servers, "features": {"multi_agent": True}},
            "developerInstructions": (
                "This is a native manager integration exercise, not a managed reviewer. "
                "Follow the user's complete goal using native subagents and the installed "
                "DevGod MCP server. Keep work within the fixture repository and existing "
                "permission boundary. Do not modify global settings or grant trust."
            ),
        })
        report.update(thread_id=thread.thread.id, model=thread.model)
        turn = await asyncio.to_thread(client.turn_start, thread.thread.id, [
            {"type": "skill", "name": "devgod-manager", "path": str(skill)},
            {"type": "text", "text": request},
        ])
        report["turn_id"] = turn.turn.id
        with (output / "native-events.jsonl").open("w") as log:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("Native manager exceeded the smoke time budget")
                event = await asyncio.wait_for(
                    asyncio.to_thread(client.next_turn_notification, turn.turn.id), remaining
                )
                payload = plain(event.payload)
                log.write(json.dumps({"method": event.method, "payload": payload}) + "\n")
                log.flush()
                item = payload.get("item") if isinstance(payload, dict) else None
                if isinstance(item, dict) and item.get("id"):
                    observed_items[item["id"]] = item
                if event.method == "item/completed":
                    if isinstance(item, dict) and item.get("type") == "agentMessage":
                        text = item.get("text")
                        if isinstance(text, str):
                            # Notifications belong to the root turn; child work is
                            # represented separately by subAgentActivity items.
                            completed_root_messages.append(text)
                    print(json.dumps({"method": event.method, "type": item.get("type"),
                                          "tool": item.get("tool"), "name": item.get("name")}), flush=True)
                if event.method == "turn/completed":
                    completion_failure = validate_turn_completion(payload, turn.turn.id)
                    if completion_failure:
                        raise RuntimeError(completion_failure)
                    report["turn_result"] = payload
                    break
        status = json.loads(run([*cli, "status"]))
        report["kernel_status"] = status
        native_jobs = [
            item for item in observed_items.values()
            if "collab" in item.get("type", "").lower() or item.get("type") == "subAgentActivity"
        ]
        mcp_calls = [item for item in observed_items.values() if "mcp" in item.get("type", "").lower()]
        report["native_delegation_items"] = native_jobs
        native_child_ids = sorted({
            item["agentThreadId"] for item in native_jobs
            if item.get("agentThreadId") and item.get("kind") == "started"
        })
        report["native_child_thread_ids"] = native_child_ids
        final_message = completed_root_messages[-1] if completed_root_messages else ""
        final_handoff_valid, final_handoff_failure = validate_final_handoff(final_message)
        report.update(final_handoff_report(final_message))
        report["final_handoff_valid"] = final_handoff_valid
        report["final_handoff_validation_failure"] = final_handoff_failure
        report["mcp_call_count"] = len(mcp_calls)
        report["mcp_calls"] = [{key: item.get(key) for key in ("id", "type", "server", "tool", "status")} for item in mcp_calls]
        approval_errors = [
            {"tool": item.get("tool"), "error": item.get("error")}
            for item in mcp_calls
            if "approval" in json.dumps(item.get("error", {})).lower()
        ]
        report["mcp_approval_errors"] = approval_errors
        report["files"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in repo.glob("*.py")}
        tasks = status.get("tasks", [])
        run_record = status.get("run") or {}
        dependencies = [task for task in tasks if (task.get("spec", task)).get("depends_on")]
        if (
            run_record.get("state") == "verified"
            and len(tasks) >= 2
            and dependencies
            and len(native_child_ids) >= 3
            and mcp_calls
            and final_handoff_valid
            and not approvals
            and not approval_errors
        ):
            report["status"] = "passed"
        else:
            report["limitation"] = "The single native manager turn did not satisfy every delegation, dependency, MCP, verified-state, and final-handoff condition."
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        await asyncio.to_thread(client.close)
        if monitor is not None:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
        report["process_codex_home_evidence"] = list(process_evidence.values())
        report["delivery_status"] = report["status"]
        required_roles = {"native_app_server", "devgod_mcp", "verification_supervisor", "verification_app_server"}
        report["private_codex_home_propagation_verified"] = (
            required_roles <= {entry["role"] for entry in process_evidence.values()}
            and all(entry["private_codex_home_matches"] for entry in process_evidence.values())
        )
        if not report["private_codex_home_propagation_verified"]:
            report.update(status="failed", error="Private CODEX_HOME was not observed across every runtime role")
        report["duration_seconds"] = round(time.monotonic() - started, 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="devgod-native-"))
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Native smoke output directory must be empty")
    report = asyncio.run(exercise(output, args.timeout))
    print(json.dumps({"status": report["status"], "report": str(output / "report.json"),
                      "error": report.get("error"), "duration_seconds": report.get("duration_seconds")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
