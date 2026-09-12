"""Administrative command line and local MCP entry point.

Normal engineering work stays in Codex. These commands expose the same kernel
operations for setup, inspection and automated diagnostics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 1_048_576


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devgod", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Consuming Git worktree")
    parser.add_argument("--state-home", type=Path, help="Private state location (default: XDG_STATE_HOME)")
    parser.add_argument("--json", action="store_true", help="Print structured JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Install repository-local native Codex integration")
    init.add_argument("--migrate", action="store_true", help="Migrate recognized legacy DevGod integration")
    commands.add_parser("doctor", help="Inspect installation and local runtime capabilities")
    commands.add_parser("uninstall", help="Remove only DevGod-owned repository integration")
    commands.add_parser("mcp", help="Run the local stdio MCP server")
    commands.add_parser("hook", help="Handle one native Codex lifecycle event from stdin")

    start = commands.add_parser("start", help="Start an accepted goal on a local delivery branch")
    start.add_argument("--goal", required=True)
    start.add_argument("--acceptance", action="append", required=True, metavar="ID:DESCRIPTION")
    start.add_argument("--branch", help="Delivery branch name; automatically generated when omitted")
    start.add_argument("--checks", type=Path, help="JSON array of accepted check specifications")
    start.add_argument("--decisions", type=Path, help="JSON object of accepted design decisions")

    plan = commands.add_parser("plan", help="Add or amend task scopes and accepted verification checks")
    plan.add_argument("--run", dest="run_id")
    plan.add_argument("--tasks", type=Path, help="JSON array of task specifications to add or amend")
    plan.add_argument("--checks", type=Path, help="JSON array replacing accepted check specifications")

    for name in ("status", "next", "resume", "cancel"):
        command = commands.add_parser(name, help=f"{name.capitalize()} the selected or current run")
        command.add_argument("run_id", nargs="?")

    task = commands.add_parser("task", help="Record task plans and implementation claims")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    add = task_commands.add_parser("add", help="Add or replace one planned task")
    add.add_argument("--run", dest="run_id")
    add.add_argument("--id", required=True, dest="task_id")
    add.add_argument("--title", required=True)
    add.add_argument("--role", required=True)
    add.add_argument("--acceptance", action="append", required=True)
    add.add_argument("--depends-on", action="append", default=[])
    add.add_argument("--path", action="append", required=True)
    update = task_commands.add_parser("update", help="Record implementation progress; cannot grant verification")
    update.add_argument("task_id")
    update.add_argument("--run", dest="run_id")
    update.add_argument("--state", choices=("planned", "implementing", "repair", "blocked"))
    update.add_argument("--complete", action="store_true", help="Claim implementation complete for verification")
    update.add_argument("--summary", default="")
    listing = task_commands.add_parser("list", help="Show tasks in the run")
    listing.add_argument("--run", dest="run_id")

    checkpoint = commands.add_parser("checkpoint", help="Save or inspect continuation context")
    checkpoint_commands = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    save = checkpoint_commands.add_parser("save")
    save.add_argument("--run", dest="run_id")
    save.add_argument("--summary", required=True)
    save.add_argument("--decisions", type=Path)
    save.add_argument("--next-action", action="append", default=[])
    show = checkpoint_commands.add_parser("show")
    show.add_argument("--run", dest="run_id")

    verify = commands.add_parser("verify", help="Execute checks and independent reviews; wait for the result")
    verify.add_argument("run_id", nargs="?")
    recover = commands.add_parser("recover", help="Record inspection of interrupted effects and safely rerun verification")
    recover.add_argument("job_id")
    recover.add_argument("--attempt", type=int, required=True)
    recover.add_argument("--candidate-digest", required=True)
    recover.add_argument("--checks-digest", required=True)
    recover.add_argument("--observations", required=True, help="Manager's inspection of possible effects; never a passing receipt")
    wait = commands.add_parser("wait", help="Wait for a job owned by a running MCP service")
    wait.add_argument("job_id")
    wait.add_argument("--timeout", type=float, default=30.0, help="Maximum wait in seconds (0–60)")
    return parser


def _read_json(path: Path) -> Any:
    with path.open("rb") as stream:
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("JSON input exceeds the 1 MiB limit")
    return json.loads(data)


def _criteria(values: list[str]) -> list[dict[str, str]]:
    result = []
    for value in values:
        key, separator, description = value.partition(":")
        if not separator or not key.strip() or not description.strip():
            raise ValueError("Acceptance must use ID:DESCRIPTION")
        result.append({"acceptance_id": key.strip(), "description": description.strip()})
    return result


def _human(value: Any) -> str:
    """Keep inspectable structured detail without leaking protocol chatter."""
    if isinstance(value, dict):
        if "error" in value:
            error = value["error"]
            if isinstance(error, dict):
                return f"{error.get('code', 'error')}: {error.get('message', error)}"
            return str(error)
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{key}: {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
            elif item is not None:
                lines.append(f"{key}: {item}")
        return "\n".join(lines) or "No active DevGod run."
    if isinstance(value, list):
        return "\n".join(_human(item) for item in value) or "No records."
    return str(value)


def _print(value: Any, *, as_json: bool, error: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True) if as_json else _human(value),
          file=sys.stderr if error and not as_json else sys.stdout)


async def _dispatch(args: argparse.Namespace) -> tuple[Any, int]:
    from .mcp_server import jsonable, open_runtime, selected_run, wait_for_job
    from .models import AcceptanceCriterion, CheckSpec, TaskSpec

    runtime = open_runtime(args.repo, args.state_home)
    try:
        service = runtime.service
        command = args.command
        if command == "start":
            result = service.start(
                args.goal,
                [AcceptanceCriterion.model_validate(item) for item in _criteria(args.acceptance)],
                checks=[CheckSpec.model_validate(item) for item in _read_json(args.checks)] if args.checks else [],
                decisions=_read_json(args.decisions) if args.decisions else {},
                branch=args.branch,
            )
        elif command == "status":
            result = service.status(args.run_id)
        elif command == "next":
            result = service.next_action(args.run_id)
        elif command == "resume":
            result = service.resume(args.run_id)
        elif command == "cancel":
            result = await service.cancel(args.run_id)
        elif command == "plan":
            if args.tasks is None and args.checks is None:
                raise ValueError("Plan requires --tasks or --checks")
            tasks = [TaskSpec.model_validate(item) for item in _read_json(args.tasks)] if args.tasks else []
            checks = [CheckSpec.model_validate(item) for item in _read_json(args.checks)] if args.checks else None
            result = service.plan(selected_run(service, args.run_id), tasks, checks=checks)
        elif command == "task":
            run_id = selected_run(service, args.run_id)
            if args.task_command == "add":
                spec = TaskSpec(task_id=args.task_id, title=args.title, owner_role=args.role,
                                acceptance=args.acceptance, depends_on=args.depends_on, allowed_paths=args.path)
                result = service.plan(run_id, [spec])
            elif args.task_command == "list":
                result = {"run_id": run_id, "tasks": service.status(run_id)["tasks"]}
            else:
                if args.complete and args.state:
                    raise ValueError("Use either --complete or --state for a task update")
                if not args.complete and not args.state:
                    raise ValueError("Task update requires --state or --complete")
                result = service.task_update(run_id, args.task_id,
                                             "verifying" if args.complete else args.state, summary=args.summary)
        elif command == "checkpoint":
            run_id = selected_run(service, args.run_id)
            if args.checkpoint_command == "save":
                result = service.checkpoint(run_id, {
                    "summary": args.summary,
                    "decisions": _read_json(args.decisions) if args.decisions else {},
                    "next_actions": args.next_action,
                })
            else:
                result = {"run_id": run_id, "checkpoint": service.status(run_id)["run"].get("checkpoint")}
        elif command in {"verify", "recover"}:
            if command == "recover":
                started = jsonable(await service.recover(
                    args.job_id, args.attempt, args.candidate_digest, args.checks_digest,
                    args.observations,
                ))
            else:
                started = jsonable(await service.verify(args.run_id))
            job = started.get("job", started)
            job_id = job["job_id"]
            # CLI has no resident loop after exit. Own execution until completion;
            # only MCP can return promptly while keeping its worker alive.
            await runtime.runner.wait(job_id)
            result = jsonable(service.verification_status(job_id))
            job = result.get("job", result)
            state = job.get("state", job.get("status"))
            verified = result.get("run", {}).get("state") == "verified"
            return result, 0 if state == "succeeded" and verified else 1
        elif command == "wait":
            result = await wait_for_job(service, args.job_id, args.timeout)
            job = result.get("job", result)
            state = job.get("state", job.get("status"))
            return result, 0 if state == "succeeded" else (1 if state in {"failed", "cancelled", "interrupted"} else 3)
        else:
            raise ValueError(f"Unsupported command: {command}")
        return jsonable(result), 0
    finally:
        await runtime.close()


async def _doctor(repo: Path) -> dict[str, Any]:
    from importlib.metadata import version

    from . import install
    from .codex_adapter import CodexAdapter

    result = install.doctor(repo)
    adapter = CodexAdapter()
    try:
        capabilities = await adapter.capabilities()
    finally:
        await adapter.close()
    result["runtime"] = {"python": sys.version.split()[0], "mcp": version("mcp"), **capabilities}
    result["ok"] = bool(result.get("ok", True) and capabilities.get("available"))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "mcp":
            from .mcp_server import create_server

            create_server(args.repo, args.state_home).run(transport="stdio")
            return 0
        if args.command == "hook":
            from .hooks import main as hook_main

            hook_args = ["--repo", str(args.repo)]
            if args.state_home is not None:
                hook_args.extend(["--state-home", str(args.state_home)])
            return hook_main(hook_args)
        if args.command in {"init", "doctor", "uninstall"}:
            from . import install

            if args.command == "init":
                result = install.init(args.repo, migrate=args.migrate, state_home=args.state_home)
            elif args.command == "doctor":
                result = asyncio.run(_doctor(args.repo))
            else:
                result = getattr(install, args.command)(args.repo)
            _print(result, as_json=args.json)
            return 0 if result.get("ok", True) else 1
        result, code = asyncio.run(_dispatch(args))
        _print(result, as_json=args.json)
        return code
    except KeyboardInterrupt:
        _print({"error": {"code": "interrupted", "message": "Execution interrupted; recorded work is preserved."}},
               as_json=args.json and args.command != "mcp", error=True)
        return 130
    except Exception as exc:
        from .mcp_server import error_payload

        _print(error_payload(exc), as_json=args.json and args.command != "mcp", error=True)
        return 2 if isinstance(exc, (ValueError, KeyError)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
