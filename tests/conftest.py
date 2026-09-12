"""Shared fixtures keep repository state and secrets outside real user projects."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="DevGod Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="DevGod Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(root), *args],
            env=env,
            check=True,
            capture_output=True,
        )

    git("init", "--initial-branch=main")
    (root / "README.md").write_text("# Fixture project\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "Initial fixture")
    return root
