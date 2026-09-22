from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from devgod import install


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "repo with spaces"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


def test_install_is_idempotent_and_removable(repo):
    first = install.init(repo)
    initial = snapshot(repo)
    times = {p: (repo / p).stat().st_mtime_ns for p in initial}
    second = install.init(repo)
    assert first["installed"] and not second["changed"]
    assert second["hook_trust"] == "host_managed_unknown"
    assert snapshot(repo) == initial
    assert {p: (repo / p).stat().st_mtime_ns for p in initial} == times
    assert install.doctor(repo)["ok"]
    install.uninstall(repo)
    assert snapshot(repo) == {}
    assert not install.uninstall(repo)["removed"]


def test_role_agents_are_managed_and_preserve_user_edits(repo):
    install.init(repo)
    agents = [repo / ".codex/agents" / name for name in install.ROLE_AGENT_FILES]
    assert all(path.exists() for path in agents)
    manifest = json.loads((repo / install.MANIFEST).read_text())
    assert {str(path.relative_to(repo)) for path in agents} <= set(manifest["files"])

    edited = agents[0]
    edited.write_text(edited.read_text() + "\n# local customization\n")
    result = install.init(repo)
    assert str(edited.relative_to(repo)) in result["preserved_edits"]

    result = install.uninstall(repo)
    assert str(edited.relative_to(repo)) in result["preserved"]
    assert edited.exists()
    assert all(not path.exists() for path in agents[1:])


def test_role_agents_are_added_to_existing_version_two_manifest(repo):
    install.init(repo)
    manifest_path = repo / install.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    for name in install.ROLE_AGENT_FILES:
        del manifest["files"][f".codex/agents/{name}"]
    manifest_path.write_text(json.dumps(manifest))

    install.init(repo)
    upgraded = json.loads(manifest_path.read_text())
    assert upgraded["version"] == 2
    assert all(f".codex/agents/{name}" in upgraded["files"] for name in install.ROLE_AGENT_FILES)


def test_existing_settings_comments_and_instructions_survive(repo):
    agents = "# Local standards\n\nAlways run the project's own gate.\n"
    config = '# User notes\nmodel = "custom-model"\napproval_policy = "on-request"\n\n[features]\nhooks = false\n\n[mcp_servers.other]\ncommand = "other" # keep this\n'
    (repo / "AGENTS.md").write_text(agents)
    (repo / ".codex").mkdir()
    (repo / ".codex/config.toml").write_text(config)
    install.init(repo)
    actual = (repo / ".codex/config.toml").read_text()
    assert actual.startswith(config)
    data = tomllib.loads(actual)
    assert data["approval_policy"] == "on-request"
    assert data["model"] == "custom-model"
    assert data["features"]["hooks"] is False
    assert data["mcp_servers"]["devgod"]["default_tools_approval_mode"] == "auto"
    assert data["mcp_servers"]["devgod"]["enabled_tools"] == list(install.APPROVED_TOOLS)
    assert data["mcp_servers"]["devgod"]["tools"] == {
        name: {"approval_mode": "approve"} for name in install.APPROVED_TOOLS
    }
    assert set(data) == {"model", "approval_policy", "features", "mcp_servers"}
    install.uninstall(repo)
    assert (repo / "AGENTS.md").read_text() == agents
    assert (repo / ".codex/config.toml").read_text() == config


def test_hooks_merge_preserves_user_handlers_and_root_format(repo):
    (repo / ".codex").mkdir()
    user_hook = {"hooks": [{"type": "command", "command": "trusted-tool", "timeout": 9}]}
    text = '{\n  "description" : "keep this format",\n  "hooks": ' + json.dumps({"Stop": [user_hook], "PermissionRequest": [user_hook]}) + '\n}\n'
    (repo / ".codex/hooks.json").write_text(text)
    install.init(repo)
    output = (repo / ".codex/hooks.json").read_text()
    assert '"description" : "keep this format"' in output
    hooks = json.loads(output)["hooks"]
    assert hooks["Stop"][0] == user_hook
    assert hooks["PermissionRequest"] == [user_hook]
    install.uninstall(repo)
    hooks = json.loads((repo / ".codex/hooks.json").read_text())["hooks"]
    assert hooks["Stop"] == hooks["PermissionRequest"] == [user_hook]


def test_existing_names_do_not_get_overwritten(repo):
    (repo / ".codex").mkdir()
    (repo / ".codex/config.toml").write_text('[mcp_servers.devgod]\ncommand = "my-server"\n')
    skill = repo / ".agents/skills/devgod-manager"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("user skill")
    result = install.init(repo)
    assert result["server"] == "devgod_workflow"
    assert result["skill_path"] == ".agents/skills/devgod-manager-2/SKILL.md"
    install.uninstall(repo)
    assert (skill / "SKILL.md").read_text() == "user skill"
    assert tomllib.loads((repo / ".codex/config.toml").read_text())["mcp_servers"]["devgod"]["command"] == "my-server"


def test_user_edits_to_skill_and_managed_settings_preserved(repo):
    result = install.init(repo)
    skill = repo / result["skill_path"]
    skill.write_text(skill.read_text() + "\nUser-specific instruction.\n")
    config = repo / ".codex/config.toml"
    config.write_text(config.read_text().replace('default_tools_approval_mode = "auto"', 'default_tools_approval_mode = "prompt"'))
    upgraded = install.init(repo)
    assert upgraded["preserved_edits"] == [".codex/config.toml", result["skill_path"]]
    assert "User-specific instruction" in skill.read_text()
    assert tomllib.loads(config.read_text())["mcp_servers"]["devgod"]["default_tools_approval_mode"] == "prompt"
    assert upgraded["server"] == "devgod"
    assert upgraded["backups"]
    assert all(not Path(p).is_relative_to(repo) for p in upgraded["backups"])
    install.uninstall(repo)
    assert skill.exists()
    assert tomllib.loads(config.read_text())["mcp_servers"]["devgod"]["default_tools_approval_mode"] == "prompt"


def test_edited_verify_approval_is_not_bypassed_by_a_new_server(repo):
    install.init(repo)
    config = repo / ".codex/config.toml"
    original = config.read_text()
    modified = original.replace(
        '[mcp_servers.devgod.tools.verify]\napproval_mode = "approve"',
        '[mcp_servers.devgod.tools.verify]\napproval_mode = "prompt"',
    )
    assert modified != original
    config.write_text(modified)
    for _ in range(2):
        result = install.init(repo)
        assert result["server"] == "devgod"
        assert ".codex/config.toml" in result["preserved_edits"]
        servers = tomllib.loads(config.read_text())["mcp_servers"]
        assert set(servers) == {"devgod"}
        assert servers["devgod"]["tools"]["verify"]["approval_mode"] == "prompt"
        assert config.read_text() == modified
    assert not install.doctor(repo)["ok"]
    install.uninstall(repo)
    assert config.read_text() == modified


def test_edited_managed_agents_content_remains_active(repo):
    install.init(repo)
    path = repo / "AGENTS.md"
    path.write_text(path.read_text().replace(install.AGENTS_END, "Run the custom gate.\n" + install.AGENTS_END))
    install.init(repo)
    install.uninstall(repo)
    assert "Run the custom gate." in path.read_text()


@pytest.mark.parametrize("target", [".codex", "AGENTS.md", ".agents"])
def test_symlinks_rejected_without_outside_writes(repo, tmp_path, target):
    outside = tmp_path / "outside"
    if target == "AGENTS.md":
        outside.write_text("user content")
    else:
        outside.mkdir()
    (repo / target).symlink_to(outside)
    with pytest.raises(install.InstallError, match="symlink"):
        install.init(repo)
    assert snapshot(repo) == ({"AGENTS.md": b"user content"} if target == "AGENTS.md" else {})


def test_manifest_traversal_and_arbitrary_section_deletion_rejected(repo, tmp_path):
    install.init(repo)
    manifest = repo / install.MANIFEST
    data = json.loads(manifest.read_text())
    outside = tmp_path / "important"
    outside.write_text("keep")
    data["files"]["../important"] = install._digest("keep")
    manifest.write_text(json.dumps(data))
    before = snapshot(repo)
    with pytest.raises(install.InstallError):
        install.uninstall(repo)
    assert outside.read_text() == "keep" and snapshot(repo) == before
    del data["files"]["../important"]
    data["sections"]["AGENTS.md"] = "arbitrary user text"
    manifest.write_text(json.dumps(data))
    with pytest.raises(install.InstallError):
        install.uninstall(repo)


def test_forged_manifest_cannot_remove_unowned_permission_hooks(repo):
    install.init(repo)
    manifest_path = repo / install.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    path = repo / ".codex/hooks.json"
    config = json.loads(path.read_text())
    safety_hook = {"hooks": [{"type": "command", "command": "user-permission-gate"}]}
    config["hooks"]["PermissionRequest"] = [safety_hook]
    path.write_text(json.dumps(config))
    manifest["hooks"]["PermissionRequest"] = [safety_hook]
    manifest_path.write_text(json.dumps(manifest))
    before = snapshot(repo)
    with pytest.raises(install.InstallError, match="ownership"):
        install.uninstall(repo)
    assert snapshot(repo) == before
    with pytest.raises(install.InstallError, match="ownership"):
        install.init(repo)
    assert snapshot(repo) == before


def test_manifest_server_name_cannot_inject_configuration_tables(repo):
    install.init(repo)
    path = repo / install.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["server"] = 'evil]\ncommand="x"\n[features'
    path.write_text(json.dumps(manifest))
    before = snapshot(repo)
    with pytest.raises(install.InstallError, match="server name"):
        install.init(repo)
    assert snapshot(repo) == before


def test_invalid_existing_config_does_not_partially_install(repo):
    (repo / ".codex").mkdir()
    (repo / ".codex/config.toml").write_text("invalid TOML")
    before = snapshot(repo)
    with pytest.raises(install.InstallError):
        install.init(repo)
    assert snapshot(repo) == before


def test_generated_command_quotes_paths_and_never_grants_host_permissions(repo):
    result = install.init(repo, [sys.executable, "-m", "devgod"])
    data = tomllib.loads((repo / ".codex/config.toml").read_text())
    assert set(data) == {"mcp_servers"}
    assert set(data["mcp_servers"]) == {result["server"]}
    assert set(data["mcp_servers"][result["server"]]["tools"]) == set(install.APPROVED_TOOLS)
    hooks = json.loads((repo / ".codex/hooks.json").read_text())["hooks"]
    assert set(hooks) == set(install.EVENTS)
    for groups in hooks.values():
        handler = groups[0]["hooks"][0]
        assert shlex.split(handler["command"]) == [sys.executable, "-m", "devgod", "--repo", str(repo), "hook"]
        assert handler["timeout"] == 5
    assert "hook_trust" in result and not any("trust" in k for k in data)


@pytest.mark.parametrize("surface", ["mcp", "hook"])
def test_packaged_plugin_launcher_ignores_ambient_python_modules(repo, tmp_path, surface):
    injected = tmp_path / "injected"
    injected.mkdir()
    marker = tmp_path / "plugin-imported-untrusted-code"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    (injected / "devgod.py").write_text(source)
    (repo / "devgod.py").write_text(source)
    assets = install.ASSETS / "devgod"
    if surface == "mcp":
        settings = json.loads((assets / ".mcp.json").read_text())["mcpServers"]["devgod"]
        argv = [settings["command"], *settings["args"], "--help"]
    else:
        settings = json.loads((assets / "hooks/hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]
        argv = [*shlex.split(settings["command"]), "--help"]
    environment = dict(os.environ, PYTHONPATH=str(injected), PYTHONHOME=str(injected))
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    result = subprocess.run(argv, cwd=repo, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_explicit_private_state_location_is_shared_by_mcp_and_hooks(repo, tmp_path):
    state = tmp_path / "custom state"
    install.init(repo, state_home=state)
    data = tomllib.loads((repo / ".codex/config.toml").read_text())
    assert data["mcp_servers"]["devgod"]["args"][-3:] == ["--state-home", str(state), "mcp"]
    hooks = json.loads((repo / ".codex/hooks.json").read_text())["hooks"]
    assert shlex.split(hooks["Stop"][0]["hooks"][0]["command"])[-3:] == ["--state-home", str(state), "hook"]
    before = snapshot(repo)
    install.init(repo)
    assert snapshot(repo) == before


def test_backups_use_explicit_and_persisted_state_without_default_writes(repo, tmp_path):
    state = tmp_path / "selected-state"
    (repo / "AGENTS.md").write_text("<!-- BEGIN DEVGOD MANAGED -->\nOld limits\n<!-- END DEVGOD MANAGED -->\n")
    migrated = install.init(repo, migrate=True, state_home=state)
    assert migrated["backups"] and all(Path(p).is_relative_to(state) for p in migrated["backups"])
    agents = repo / "AGENTS.md"
    agents.write_text(agents.read_text().replace(install.AGENTS_END, "Custom rule\n" + install.AGENTS_END))
    upgraded = install.init(repo)
    assert upgraded["backups"] and all(Path(p).is_relative_to(state) for p in upgraded["backups"])
    install.uninstall(repo)
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("parent", ["base", "repos", "install-backups", "worktree"])
def test_backup_parent_symlinks_cannot_redirect_writes(repo, tmp_path, parent):
    from devgod.workspace import Workspace

    state = tmp_path / "selected-state"
    outside = tmp_path / "outside-state"
    outside.mkdir()
    if parent == "base":
        state.symlink_to(outside)
    elif parent == "repos":
        state.mkdir()
        (state / "repos").symlink_to(outside)
    else:
        workspace = Workspace(repo, state_root=state)
        base = workspace.state_dir / "install-backups"
        if parent == "worktree":
            base.mkdir()
            base = base / workspace.worktree_id
        base.symlink_to(outside)
    (repo / "AGENTS.md").write_text("<!-- BEGIN DEVGOD MANAGED -->\nOld limits\n<!-- END DEVGOD MANAGED -->\n")
    before = snapshot(repo)
    with pytest.raises(install.InstallError, match="symlink|unsafe"):
        install.init(repo, migrate=True, state_home=state)
    assert list(outside.iterdir()) == []
    assert snapshot(repo) == before


def test_legacy_migration_removes_only_recognized_old_handlers(repo):
    (repo / ".codex").mkdir()
    (repo / "AGENTS.md").write_text("User prefix\n<!-- BEGIN DEVGOD MANAGED -->\nOld limits\n<!-- END DEVGOD MANAGED -->\nUser suffix\n")
    (repo / ".agents.md").write_text("<!-- BEGIN DEVGOD KERNEL -->\nOld limits\n<!-- END DEVGOD KERNEL -->\nKeep memory.\n")
    old = {"type": "command", "command": 'node "$(git rev-parse --show-toplevel)/plugins/devgod/scripts/stop.mjs"'}
    custom = {"type": "command", "command": "my-review-hook"}
    (repo / ".codex/hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [old, custom]}]}}))
    result = install.init(repo, migrate=True)
    assert len(result["backups"]) == 3
    assert "Old limits" not in (repo / "AGENTS.md").read_text()
    assert "User prefix" in (repo / "AGENTS.md").read_text()
    assert (repo / ".agents.md").read_text().endswith("Keep memory.\n")
    hooks = json.loads((repo / ".codex/hooks.json").read_text())["hooks"]["Stop"]
    assert any(g["hooks"] == [custom] for g in hooks)
    assert all(old not in g["hooks"] for g in hooks)
    install.uninstall(repo)
    assert "User suffix" in (repo / "AGENTS.md").read_text()


def test_migration_archives_legacy_postgres_mcp_and_unchanged_skills(repo):
    (repo / ".codex").mkdir()
    config = '# Preserve model\nmodel = "user-model"\n[mcp_servers.devgod]\ncommand = "node"\nargs = ["--experimental-strip-types", "/old/devgod/src/admin.ts", "mcp"]\n[mcp_servers.other]\ncommand = "other"\n'
    (repo / ".codex/config.toml").write_text(config)
    (repo / ".devgod").mkdir()
    records = []
    for name in ("devgod-autopilot", "devgod-execution", "devgod-custom"):
        path = f".agents/skills/{name}/SKILL.md"
        full = repo / path
        full.parent.mkdir(parents=True)
        content = "Old DevGod council and database workflow"
        full.write_text(content + (" user edit" if name.endswith("custom") else ""))
        records.append({"target": path, "strategy": "replace", "contentHash": install._digest(content)})
    records.append({"target": "../../outside", "strategy": "replace", "contentHash": install._digest("outside")})
    (repo / ".devgod/install-manifest.json").write_text(json.dumps({"version": 1, "files": records}))
    (repo / ".devgod/memory.md").write_text("Keep durable history")
    result = install.init(repo, migrate=True)
    actual = tomllib.loads((repo / ".codex/config.toml").read_text())
    assert actual["mcp_servers"]["devgod"]["args"][-1] == "mcp"
    assert "admin.ts" not in str(actual)
    assert actual["mcp_servers"]["other"]["command"] == "other"
    assert actual["model"] == "user-model"
    assert len(result["migrated"]) == 2
    assert not (repo / ".agents/skills/devgod-autopilot/SKILL.md").exists()
    assert (repo / ".agents/skills/devgod-custom/SKILL.md").exists()
    assert (repo / ".devgod/memory.md").read_text() == "Keep durable history"
