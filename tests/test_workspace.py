from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from devgod.models import CheckSpec, Policy
from devgod.workspace import SourceChangedError, Workspace, WorkspaceError


def git(root: Path, *args: str) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    return subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(root), *args],
        env=env,
        check=True,
        capture_output=True,
    ).stdout


@pytest.fixture
def workspace(git_repo: Path, tmp_path: Path) -> Workspace:
    return Workspace(git_repo, tmp_path / "state")


def test_branch_preserves_index_and_dirty_work(workspace: Workspace) -> None:
    root = workspace.root
    (root / "README.md").write_text("staged version\n")
    git(root, "add", "README.md")
    (root / "README.md").write_text("unstaged version\n")
    (root / "new\nfile.py").write_text("untracked\n")
    before = workspace.provenance()
    manifest = workspace.manifest()
    created = workspace.ensure_branch("devgod/delivery")
    after = workspace.provenance()
    assert created["branch"] == "devgod/delivery"
    assert created["original_branch"] == "main"
    assert after["baseline_status"] == before["baseline_status"]
    assert after["baseline_index_digest"] == before["baseline_index_digest"]
    assert workspace.manifest() == manifest
    assert created["baseline_dirty_paths"] == ["README.md", "new\nfile.py"]


def test_same_branch_and_collision_are_automatic(workspace: Workspace) -> None:
    git(workspace.root, "branch", "devgod/existing")
    branch = workspace.ensure_branch("devgod/existing")["branch"]
    assert branch.startswith("devgod/existing-")
    assert workspace.ensure_branch(branch)["branch"] == branch
    assert workspace.ensure_branch()["branch"] == branch


def test_git_hooks_fsmonitor_and_inherited_overrides_never_execute(
    workspace: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = workspace.root
    marker = tmp_path / "unexpected-hook"
    hooks = root / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-checkout", "reference-transaction"):
        script = hooks / name
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        script.chmod(0o755)
    monitor = tmp_path / "monitor"
    monitor.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    monitor.chmod(0o755)
    git(root, "config", "core.fsmonitor", str(monitor))
    git(root, "config", "diff.external", str(monitor))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-git-dir"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    workspace.ensure_branch("devgod/safe")
    workspace.snapshot()
    assert not marker.exists()


def test_ignored_secrets_absent_but_tracked_ignored_present(workspace: Workspace) -> None:
    root = workspace.root
    (root / ".gitignore").write_text("*.secret\n.env\n")
    (root / "tracked.secret").write_text("intended tracked source")
    git(root, "add", "-f", "tracked.secret")
    (root / "private.secret").write_text("ambient secret")
    (root / ".env").write_text("secret")
    (root / "line\nname.py").write_text("source")
    manifest = workspace.manifest()
    assert "tracked.secret" in manifest
    assert "line\nname.py" in manifest
    assert "private.secret" not in manifest
    assert ".env" not in manifest
    snapshot = Path(workspace.snapshot().snapshot_path)
    assert (snapshot / "tracked.secret").read_text() == "intended tracked source"
    assert not (snapshot / "private.secret").exists()
    assert not (snapshot / ".git").exists()


def test_fingerprint_binds_bytes_mode_deletion_paths_and_plan(workspace: Workspace) -> None:
    source = workspace.root / "README.md"
    baseline = workspace.manifest()
    original = workspace.fingerprint()
    source.chmod(0o755)
    executable = workspace.fingerprint()
    assert executable.candidate_digest != original.candidate_digest
    source.chmod(0o644)
    assert workspace.fingerprint().candidate_digest == original.candidate_digest
    source.write_text("changed")
    assert workspace.fingerprint().candidate_digest != original.candidate_digest
    assert workspace.changed_paths(baseline) == ("README.md",)
    source.unlink()
    assert workspace.manifest()["README.md"] == {"kind": "missing"}
    assert workspace.fingerprint().candidate_digest not in (
        original.candidate_digest,
        executable.candidate_digest,
    )
    check = CheckSpec(name="tests", argv=["python", "-m", "pytest"])
    checked = workspace.fingerprint([check], Policy(), plan={"acceptance": ["a"]})
    edited_plan = workspace.fingerprint([check], Policy(), plan={"acceptance": ["b"]})
    assert checked.checks_digest != edited_plan.checks_digest
    assert checked.candidate_digest != edited_plan.candidate_digest


def test_snapshot_is_readonly_and_bytes_remain_frozen(workspace: Workspace) -> None:
    (workspace.root / "run.sh").write_text("#!/bin/sh\ntrue\n")
    (workspace.root / "run.sh").chmod(0o755)
    candidate = workspace.snapshot()
    snapshot = Path(candidate.snapshot_path)
    assert not stat.S_IMODE(snapshot.stat().st_mode) & 0o222
    assert stat.S_IMODE((snapshot / "run.sh").stat().st_mode) == 0o555
    assert stat.S_IMODE((snapshot / "README.md").stat().st_mode) == 0o444
    assert candidate.candidate_digest == workspace.fingerprint().candidate_digest
    (workspace.root / "README.md").write_text("changed after snapshot")
    assert (snapshot / "README.md").read_text() == "# Fixture project\n"
    assert workspace.fingerprint().candidate_digest != candidate.candidate_digest


def test_internal_symlink_is_copied_without_dereferencing(workspace: Workspace) -> None:
    (workspace.root / "link").symlink_to("README.md")
    manifest = workspace.manifest()
    assert manifest["link"] == {"kind": "symlink", "mode": "120000", "target": "README.md"}
    snapshot = Path(workspace.snapshot().snapshot_path)
    assert (snapshot / "link").is_symlink()
    assert os.readlink(snapshot / "link") == "README.md"


@pytest.mark.parametrize("target", ["../private", "/etc/passwd"])
def test_snapshot_rejects_symlink_escape(workspace: Workspace, target: str) -> None:
    (workspace.root / "escape").symlink_to(target)
    assert workspace.manifest()["escape"]["target"] == target
    with pytest.raises(WorkspaceError, match="symlink escapes"):
        workspace.snapshot()
    assert not list((workspace.state_dir / "snapshots").iterdir())


def test_no_follow_parent_directory_replaced_by_symlink(
    workspace: Workspace, tmp_path: Path
) -> None:
    directory = workspace.root / "directory"
    directory.mkdir()
    (directory / "source").write_text("safe")
    git(workspace.root, "add", "directory/source")
    (directory / "source").unlink()
    directory.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source").write_text("secret")
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="Unsafe or unreadable"):
        workspace.manifest()


def test_snapshot_rejects_concurrent_changes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan = workspace._scan

    def mutate(destination: Path | None = None, relocated: dict | None = None) -> dict:
        result = scan(destination, relocated)
        if destination is not None:
            (workspace.root / "README.md").write_text("concurrent edit")
        return result

    monkeypatch.setattr(workspace, "_scan", mutate)
    with pytest.raises(SourceChangedError, match="Source changed"):
        workspace.snapshot()
    assert not list((workspace.state_dir / "snapshots").iterdir())


def test_linked_worktrees_share_repo_state_but_have_distinct_identity(
    workspace: Workspace,
    tmp_path: Path,
) -> None:
    linked = tmp_path / "linked"
    git(workspace.root, "worktree", "add", "-b", "linked", str(linked))
    other = Workspace(linked, tmp_path / "state")
    assert workspace.repo_id == other.repo_id
    assert workspace.state_dir == other.state_dir
    assert workspace.worktree_id != other.worktree_id
    assert workspace.fingerprint().candidate_digest != other.fingerprint().candidate_digest
    with pytest.raises(WorkspaceError, match="outside every implementation worktree"):
        Workspace(linked, workspace.root / ".state")


def test_state_is_private_external_and_xdg_aware(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    workspace = Workspace(git_repo)
    assert workspace.state_dir == tmp_path / "xdg-state" / "devgod" / "repos" / workspace.repo_id
    assert stat.S_IMODE(workspace.state_dir.stat().st_mode) == 0o700
    with pytest.raises(WorkspaceError, match="outside every implementation worktree"):
        Workspace(git_repo, git_repo / "private-state")


def test_only_explicit_generated_paths_are_excluded(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / ".devgod").mkdir()
    (git_repo / ".devgod" / "source.py").write_text("source")
    (git_repo / ".devgod" / "status.json").write_text("generated")
    workspace = Workspace(git_repo, tmp_path / "state", generated_paths=(".devgod/status.json",))
    assert ".devgod/source.py" in workspace.manifest()
    assert ".devgod/status.json" not in workspace.manifest()


def test_special_files_and_limits_are_rejected(workspace: Workspace, tmp_path: Path) -> None:
    fifo = workspace.root / "pipe"
    os.mkfifo(fifo)
    # Git ignores FIFOs in its own listing; simulate an index path replaced by one.
    fifo.unlink()
    fifo.write_text("ordinary source")
    git(workspace.root, "add", "pipe")
    fifo.unlink()
    os.mkfifo(fifo)
    with pytest.raises(WorkspaceError, match="special file"):
        workspace.snapshot()
    fifo.unlink()
    tiny = Workspace(workspace.root, tmp_path / "state", max_file_bytes=1)
    with pytest.raises(WorkspaceError, match="regular-file limit"):
        tiny.manifest()


def test_non_git_directory_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="needs a working Git repository"):
        Workspace(tmp_path, tmp_path / "state")


def test_submodule_source_is_included(workspace: Workspace, tmp_path: Path) -> None:
    subrepo = tmp_path / "subrepo"
    subrepo.mkdir()
    git(subrepo, "init", "--initial-branch=main")
    (subrepo / "module.py").write_text("value = 1\n")
    git(subrepo, "add", "module.py")
    git(subrepo, "commit", "-m", "submodule fixture")
    git(
        workspace.root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(subrepo),
        "module",
    )
    (workspace.root / "module" / "new.py").write_text("new = True\n")
    manifest = workspace.manifest()
    assert manifest["module"]["kind"] == "gitlink"
    assert "module/module.py" in manifest
    assert "module/new.py" in manifest
    snapshot = Path(workspace.snapshot().snapshot_path)
    assert (snapshot / "module" / "module.py").read_text() == "value = 1\n"
    assert not (snapshot / "module" / ".git").exists()


def test_snapshot_policies_are_inert_but_reviewable(workspace: Workspace) -> None:
    config = workspace.root / ".codex"
    config.mkdir()
    (config / "config.toml").write_text('[mcp_servers.untrusted]\ncommand = "unsafe"\n')
    (config / "hooks.json").write_text('{"hook":"untrusted"}')
    (config / "rules").mkdir()
    (config / "rules" / "untrusted.md").write_text("untrusted instructions")
    candidate = workspace.snapshot()
    snapshot = Path(candidate.snapshot_path)
    assert not (snapshot / ".codex" / "config.toml").exists()
    assert not (snapshot / ".codex" / "hooks.json").exists()
    assert not (snapshot / ".codex").exists()
    mapping = workspace.snapshot_context(candidate)["relocated_paths"]
    assert (snapshot / mapping[".codex/config.toml"]).read_text() == (
        config / "config.toml"
    ).read_text()
    assert (snapshot / mapping[".codex/rules/untrusted.md"]).read_text() == "untrusted instructions"
    assert workspace.fingerprint().candidate_digest == candidate.candidate_digest
    workspace.verify_snapshot(candidate)


@pytest.mark.parametrize("mutation", ["edit", "new", "delete", "mode"])
def test_snapshot_integrity_rechecked(workspace: Workspace, mutation: str) -> None:
    candidate = workspace.snapshot()
    snapshot = Path(candidate.snapshot_path)
    snapshot.chmod(0o700)
    source = snapshot / "README.md"
    if mutation == "edit":
        source.chmod(0o600)
        source.write_text("altered")
    elif mutation == "new":
        (snapshot / "injected.py").write_text("injected")
    elif mutation == "delete":
        source.unlink()
    else:
        source.chmod(0o555)
    with pytest.raises(SourceChangedError, match="Review snapshot"):
        workspace.verify_snapshot(candidate)


def test_detached_head_gets_delivery_branch_without_checkout(workspace: Workspace) -> None:
    git(workspace.root, "checkout", "--detach")
    (workspace.root / "README.md").write_text("dirty detached source")
    assert workspace.fingerprint().branch == "HEAD"
    info = workspace.ensure_branch()
    assert info["original_branch"] == "HEAD"
    assert info["branch"].startswith("devgod/")
    assert (workspace.root / "README.md").read_text() == "dirty detached source"


def test_nested_untracked_repository_is_captured(workspace: Workspace) -> None:
    nested = workspace.root / "nested"
    nested.mkdir()
    git(nested, "init", "--initial-branch=main")
    (nested / "nested.py").write_text("nested source")
    git(nested, "add", "nested.py")
    git(nested, "commit", "-m", "nested source")
    (nested / "new.py").write_text("new source")
    assert workspace.manifest()["nested"]["kind"] == "gitlink"
    snapshot = Path(workspace.snapshot().snapshot_path)
    assert (snapshot / "nested" / "new.py").read_text() == "new source"
    assert (snapshot / "nested" / "nested.py").read_text() == "nested source"
    assert not (snapshot / "nested" / ".git").exists()


def test_global_ignores_are_respected(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    ignores = user_home / "ignore-patterns"
    ignores.write_text("private-secret\n")
    (user_home / ".gitconfig").write_text(f"[core]\nexcludesFile = {ignores}\n")
    monkeypatch.setenv("HOME", str(user_home))
    (workspace.root / "private-secret").write_text("secret")
    assert "private-secret" not in workspace.manifest()


def test_codex_directory_symlink_is_inert(workspace: Workspace) -> None:
    (workspace.root / "settings").mkdir()
    (workspace.root / "settings" / "config.toml").write_text("# untrusted config")
    (workspace.root / ".codex").symlink_to("settings")
    candidate = workspace.snapshot()
    assert not (Path(candidate.snapshot_path) / ".codex").exists()
    stored = workspace.snapshot_context(candidate)["relocated_paths"][".codex"]
    assert (Path(candidate.snapshot_path) / stored).is_symlink()


@pytest.mark.parametrize("links", [
    {"cycle": "cycle"},
    {"first": "second", "second": "first"},
    {"cycle": "nested/back/leaf", "nested/back": "../cycle"},
])
def test_snapshot_rejects_symlink_cycle(workspace: Workspace, links: dict[str, str]) -> None:
    for path, target in links.items():
        location = workspace.root / path
        location.parent.mkdir(parents=True, exist_ok=True)
        location.symlink_to(target)
    with pytest.raises(WorkspaceError, match="symlink cycle"):
        workspace.snapshot()
    assert not list((workspace.state_dir / "snapshots").iterdir())


@pytest.mark.parametrize("links", [
    {"dangling": "missing"},
    {"dangling": "missing/parent/file"},
    {"first": "second", "second": "missing/file"},
])
def test_snapshot_preserves_internal_dangling_links(
    workspace: Workspace, links: dict[str, str],
) -> None:
    for path, target in links.items():
        (workspace.root / path).symlink_to(target)
    candidate = workspace.snapshot()
    snapshot = Path(candidate.snapshot_path)
    for path, target in links.items():
        assert (snapshot / path).is_symlink()
        assert os.readlink(snapshot / path) == target
        assert not (snapshot / path).exists()
    workspace.verify_snapshot(candidate)


def test_snapshot_rejects_dangling_link_chain_escaping_root(workspace: Workspace) -> None:
    (workspace.root / "first").symlink_to("second")
    (workspace.root / "second").symlink_to("../missing/private")
    with pytest.raises(WorkspaceError, match="symlink escapes"):
        workspace.snapshot()
    assert not list((workspace.state_dir / "snapshots").iterdir())


def test_snapshot_rejects_manifest_forgery(workspace: Workspace) -> None:
    candidate = workspace.snapshot()
    metadata = Path(candidate.snapshot_path).with_suffix(".json")
    metadata.chmod(0o600)
    metadata.write_text('{"candidate_digest":"' + candidate.candidate_digest + '","files":{}}')
    with pytest.raises(WorkspaceError, match="manifest is missing or invalid"):
        workspace.verify_snapshot(candidate)
