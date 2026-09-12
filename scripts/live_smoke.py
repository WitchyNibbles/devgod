"""Opt-in authenticated production verification smoke in a fresh local repository.

This exercises real sandboxed checks and independent Codex review sessions. The
small implementation fixture is scripted, so this does not claim to exercise
native-manager conversation or native implementation-subagent delegation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import devgod
from devgod.codex_adapter import CodexAdapter
from devgod.service import DevGodService
from devgod.store import Store
from devgod.verification import VerificationRunner
from devgod.workspace import Workspace


def fixture_git(root: Path, *args: str) -> None:
    """Administrative Git setup only; repository checks use CodexAdapter."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_AUTHOR_NAME": "DevGod live smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.invalid",
        "GIT_COMMITTER_NAME": "DevGod live smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(root), *args],
        env=environment, check=True, capture_output=True,
    )


def emit(message: str) -> None:
    print(message, flush=True)


async def run_smoke(root: Path, output: Path, timeout: int) -> dict:
    repository = root / "consumer"
    repository.mkdir()
    (repository / ".gitignore").write_text("__pycache__/\n")
    (repository / "calculator.py").write_text(
        '"""Integer summation for the smoke fixture."""\n\n'
        "def subtotal(values):\n    return sum(values) + 1\n"
    )
    (repository / "verify.py").write_text(
        "from calculator import subtotal\n"
        "from invoice import invoice_total\n"
        "for values, expected in [([], 0), ([2, 3], 5), ([-2, 3], 1)]:\n"
        "    assert subtotal(values) == expected\n"
        "    assert invoice_total(values) == expected\n"
        "print('SMOKE_CHECKS_OK')\n"
    )
    fixture_git(repository, "init", "--initial-branch=main")
    fixture_git(repository, "add", ".gitignore", "calculator.py", "verify.py")
    fixture_git(repository, "commit", "-m", "Create isolated live smoke fixture")
    workspace = Workspace(repository, state_root=root / "private-state")
    store = Store(workspace.state_dir)
    adapter = CodexAdapter(receipt_root=workspace.state_dir / "supervisors")
    runner = VerificationRunner(workspace, store, adapter)
    service = DevGodService(workspace, store, runner)
    report = {
        "kind": "authenticated-production-verification",
        "started_at": datetime.now(UTC).isoformat(),
        "fixture_root": str(root),
        "implementation_mode": "scripted fixture; native implementation delegation is separate evidence",
        "success": False,
        "runtime": {
            "devgod": version("devgod"),
            "openai_codex": version("openai-codex"),
            "python": sys.version.split()[0],
            "source_sha256": {
                str(path.relative_to(Path(devgod.__file__).parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(Path(devgod.__file__).parent.rglob("*.py"))
            },
        },
    }
    try:
        result = service.start(
            "Provide integer subtotal and an invoice wrapper that delegates to it.",
            acceptance=[
                {"acceptance_id": "sum", "description": "For finite lists of integers, subtotal returns their sum, including empty and negative inputs."},
                {"acceptance_id": "wrapper", "description": "invoice_total delegates to subtotal and returns the same result."},
            ],
            tasks=[
                {"task_id": "calculator", "title": "Implement integer subtotal", "acceptance": ["sum"], "allowed_paths": ["calculator.py"]},
                {"task_id": "invoice", "title": "Add the invoice wrapper", "acceptance": ["wrapper"], "depends_on": ["calculator"], "allowed_paths": ["invoice.py"]},
            ],
            checks=[{"name": "fixture", "argv": [sys.executable, "-B", "verify.py"], "acceptance_ids": ["sum", "wrapper"], "timeout_seconds": 30}],
            policy={"review_timeout_seconds": min(timeout, 600), "max_parallel_reviews": 3},
        )
        run_id = result["run"]["run_id"]
        report["run_id"] = run_id
        report["branch"] = result["run"]["spec"]["branch"]
        service.task_update(run_id, "calculator", "implementing")
        service.task_update(run_id, "calculator", "verifying", "Initial subtotal implementation prepared.")
        service.task_update(run_id, "invoice", "implementing")
        (repository / "invoice.py").write_text(
            '"""Invoice wrapper for integer amounts."""\n\n'
            "from calculator import subtotal\n\n\n"
            "def invoice_total(values):\n    return subtotal(values)\n"
        )
        service.task_update(run_id, "invoice", "verifying", "Invoice delegates to subtotal.")
        emit(f"Live run {run_id}: executing the intentionally failing fixture check.")
        first = await service.verify(run_id)
        await asyncio.wait_for(runner.wait(first["job_id"]), timeout=timeout)
        failed = service.verification_status(first["job_id"])
        if failed["run"]["state"] != "repair":
            raise RuntimeError(f"Failing check did not enter repair: {failed['run']['state']}")
        first_evidence = store.list_evidence(run_id)
        if not any(item["kind"] == "check" and item["payload"]["result"]["exit_code"] != 0 for item in first_evidence):
            raise RuntimeError("No actual failed command evidence was recorded.")
        report["failed_check_job"] = first["job_id"]
        service.task_update(run_id, "calculator", "implementing")
        (repository / "calculator.py").write_text(
            '"""Integer summation for the smoke fixture."""\n\n'
            "def subtotal(values):\n    return sum(values)\n"
        )
        service.task_update(run_id, "calculator", "verifying", "Removed the incorrect offset after the actual check failure.")
        service.task_update(run_id, "invoice", "implementing")
        service.task_update(run_id, "invoice", "verifying", "Rechecked wrapper integration after repairing its dependency.")
        emit("Source repaired: dispatching a fresh real check and independent reviewer, QA, and security sessions.")
        second = await service.verify(run_id)
        wait = asyncio.create_task(runner.wait(second["job_id"]))
        deadline = asyncio.get_running_loop().time() + timeout
        while not wait.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Live verification exceeded the smoke budget.")
            await asyncio.wait({wait}, timeout=min(20, remaining))
            if not wait.done():
                states = [f"{item['kind']}:{item.get('role') or '-'}={item['state']}" for item in store.list_jobs(run_id)]
                emit("Live progress: " + ", ".join(states))
        await wait
        final = service.verification_status(second["job_id"])
        report["final_job"] = second["job_id"]
        report["final_status"] = final
        report["evidence"] = store.list_evidence(run_id)
        if final["run"]["state"] != "verified":
            raise RuntimeError("Real verification did not pass; inspect recorded reviewer or command diagnostics.")
        reviews = [item for item in report["evidence"] if item["kind"] == "review"]
        if len(reviews) != 3 or len({item["payload"]["thread_id"] for item in reviews}) != 3:
            raise RuntimeError("Live proof lacks three independent reviewer sessions.")
        report["verified_gate"] = final["run"]["gate"]
        (repository / "calculator.py").write_text((repository / "calculator.py").read_text() + "\n# Subsequent unverified edit.\n")
        stale = service.status(run_id)
        report["stale_candidate_state"] = stale["run"]["state"]
        if stale["run"]["state"] != "repair":
            raise RuntimeError("Post-verification source mutation did not invalidate the verified state.")
        report["success"] = True
        emit("Actual failed-check repair, sandboxed success, three independent reviews, and stale-candidate invalidation passed.")
        return report
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if report.get("run_id"):
            report["diagnostic_status"] = service.status(report["run_id"])
            report["evidence"] = store.list_evidence(report["run_id"])
        raise
    finally:
        await runner.close()
        await adapter.close()
        report["finished_at"] = datetime.now(UTC).isoformat()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, default=str) + "\n")
        output.chmod(0o600)
        store.close()
        emit(f"Live evidence: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true", help="Explicitly run authenticated Codex sessions.")
    parser.add_argument("--output", type=Path, help="Write the live evidence report to this path.")
    parser.add_argument("--timeout", type=int, default=600, help="Bound each verification round in seconds.")
    args = parser.parse_args()
    if not args.allow_live:
        parser.error("--allow-live is required: this smoke invokes authenticated Codex sessions.")
    if not 30 <= args.timeout <= 1800:
        parser.error("--timeout must be between 30 and 1800 seconds.")
    root = Path(tempfile.mkdtemp(prefix="devgod-live-"))
    root.chmod(0o700)
    output = args.output.resolve() if args.output else root / "report.json"
    emit(f"Fresh live fixture: {root}")
    try:
        asyncio.run(run_smoke(root, output, args.timeout))
    except Exception as exc:
        emit(f"Live smoke failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
