"""Build and exercise a wheel in a clean venv and consuming repository.

No global installation, model call, or trust grant is performed. Dependencies
are installed from the local uv cache unless --online is explicitly supplied.
The retained report/venv can be used by native_smoke.py for a separate live run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

REQUIRED_ASSETS = (
    "devgod/assets/devgod/.codex-plugin/plugin.json",
    "devgod/assets/devgod/.mcp.json",
    "devgod/assets/devgod/hooks/hooks.json",
    "devgod/assets/devgod/agents/devgod-luna-worker.toml",
    "devgod/assets/devgod/agents/devgod-sol-lead.toml",
    "devgod/assets/devgod/agents/devgod-sol-expert.toml",
    "devgod/assets/devgod/skills/devgod-manager/SKILL.md",
    "devgod/assets/devgod/skills/devgod-manager/agents/openai.yaml",
    "devgod/assets/agents-block.md",
)
RETIRED_ASSETS = ("devgod/assets/devgod/agents/devgod-terra-lead.toml",)


def command(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 180) -> str:
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {argv!r}\n"
            f"{result.stdout[-8000:]}\n{result.stderr[-8000:]}"
        )
    return result.stdout


def files(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp/devgod-uv-cache"))
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    project = args.project.resolve()
    output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="devgod-package-"))
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Smoke output directory must be empty")
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    env.update(
        XDG_STATE_HOME=str(output / "state"),
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="DevGod Package Smoke", GIT_AUTHOR_EMAIL="smoke@example.invalid",
        GIT_COMMITTER_NAME="DevGod Package Smoke", GIT_COMMITTER_EMAIL="smoke@example.invalid",
    )
    cache = ["--cache-dir", str(args.cache_dir)]
    network = [] if args.online else ["--offline"]
    report: dict[str, Any] = {"scope": "built distribution and installation, no live model execution"}
    try:
        command(["uv", "build", *network, *cache, "--out-dir", str(output / "dist")], cwd=project, env=env)
        wheel = next((output / "dist").glob("*.whl"))
        source = next((output / "dist").glob("*.tar.gz"))
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            assert all(name in names for name in REQUIRED_ASSETS), "Wheel omitted plugin assets"
            assert all(name not in names for name in RETIRED_ASSETS), (
                "Wheel retained retired plugin assets"
            )
            metadata = archive.read(next(name for name in names if name.endswith(".dist-info/METADATA"))).decode()
            assert "Description-Content-Type: text/markdown" in metadata, "Wheel lacks README metadata"
        with tarfile.open(source) as archive:
            names = archive.getnames()
            assert all(
                any(name.endswith("/src/" + asset) for name in names) for asset in REQUIRED_ASSETS
            ), "Source distribution omitted plugin assets"
            assert all(
                not any(name.endswith("/src/" + asset) for name in names)
                for asset in RETIRED_ASSETS
            ), "Source distribution retained retired plugin assets"
        report["artifacts"] = {
            "wheel": str(wheel), "sdist": str(source),
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "required_assets": list(REQUIRED_ASSETS),
        }
        environment = output / "venv"
        command(["uv", "venv", *cache, "--python", sys.executable, str(environment)], cwd=output, env=env)
        python = environment / "bin/python"
        command(["uv", "pip", "install", *network, *cache, "--python", str(python), str(wheel)], cwd=output, env=env)
        consumer = output / "consumer"
        consumer.mkdir()
        original = {
            "AGENTS.md": "# Existing project instructions\n\nUse small, tested changes.\n",
            ".codex/config.toml": '# Preserve this comment.\nmodel_reasoning_effort = "medium"\n',
            ".gitignore": "*.tmp\n",
            "user-note.txt": "Preexisting user content stays intact.\n",
        }
        for relative, text in original.items():
            path = consumer / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git = ["git", "-c", f"core.hooksPath={os.devnull}"]
        command([*git, "init", "--initial-branch=main"], cwd=consumer, env=env)
        command([*git, "add", "."], cwd=consumer, env=env)
        command([*git, "commit", "-m", "Package smoke baseline"], cwd=consumer, env=env)
        location = command([str(python), "-I", "-c", "import devgod; print(devgod.__file__)"], cwd=consumer, env=env).strip()
        assert str(environment) in location and str(project) not in location, location
        report["import_location"] = location
        cli = [str(python), "-I", "-m", "devgod", "--repo", str(consumer), "--json"]
        first = json.loads(command([*cli, "init"], cwd=consumer, env=env))
        generated = tomllib.loads((consumer / ".codex/config.toml").read_text())
        server = generated["mcp_servers"][first["server"]]
        assert server["command"].startswith(str(environment)), "MCP did not use wheel interpreter"
        assert "-I" in server["args"], "MCP Python entrypoint lacks module-shadow isolation"
        assert "verify" in server["enabled_tools"] and "recover" in server["enabled_tools"]
        assert all(
            server["tools"][name]["approval_mode"] == "approve"
            for name in server["enabled_tools"]
        ), "Authorized workflow tools still require extra approval"
        before_upgrade = files(consumer)
        second = json.loads(command([*cli, "init"], cwd=consumer, env=env))
        assert first["installed"] and second["installed"]
        assert not second["changed"], second
        assert files(consumer) == before_upgrade, "Repeat setup changed installed bytes"
        doctor = json.loads(command([*cli, "doctor"], cwd=consumer, env=env))
        assert doctor["ok"], doctor
        removed = json.loads(command([*cli, "uninstall"], cwd=consumer, env=env))
        assert not removed["installed"], removed
        for relative, text in original.items():
            assert (consumer / relative).read_text(encoding="utf-8") == text, relative
        assert set(files(consumer)) == set(original), "Uninstall left unowned new files"
        report.update(
            status="passed", venv=str(environment), consumer=str(consumer),
            initialization=first, repeated_setup=second, doctor=doctor, uninstall=removed,
            original_files_preserved=True, hook_trust_granted=False,
        )
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": "failed", "report": str(output / "report.json"), "error": str(exc)}))
        return 1
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": "passed", "report": str(output / "report.json"), "venv": str(environment)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
