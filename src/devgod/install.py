"""Repository-only installation, preserving unowned instructions and configuration.

The manifest describes ownership, never authorizes arbitrary paths. Codex itself
owns project and exact-definition hook trust; this module never writes trust.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

ASSETS = Path(__file__).parent / "assets"
MANIFEST = ".devgod/native-install.json"
AGENTS_BEGIN = "<!-- BEGIN DEVGOD NATIVE -->"
AGENTS_END = "<!-- END DEVGOD NATIVE -->"
CONFIG_BEGIN = "# BEGIN DEVGOD NATIVE"
CONFIG_END = "# END DEVGOD NATIVE"
EVENTS = ("SessionStart", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "Stop")
APPROVED_TOOLS = (
    "run_start", "plan", "task_update", "checkpoint", "status", "next",
    "verify", "verification_status", "wait", "resume", "recover", "cancel",
)
TRUST_NOTE = (
    "Codex must trust this project's .codex layer and each exact hook definition. "
    "Review new or changed hooks through /hooks in Codex CLI. Existing trust is "
    "reused for unchanged definitions; installation cannot grant it. The manager "
    "skill and MCP workflow remain usable when lifecycle hooks are unavailable."
)


class InstallError(ValueError):
    """An unsafe or malformed installation input was rejected before writing."""


def _digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _root(repo: Path | str) -> Path:
    root = Path(repo).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise InstallError("Repository must be an existing directory")
    # A linked worktree has a .git file. Never execute hooks just to install.
    if not (root / ".git").exists():
        raise InstallError("Run installation at a Git repository or worktree root")
    return root


def _safe(root: Path, relative: str) -> Path:
    p = Path(relative)
    if p.is_absolute() or not p.parts or any(x in {"..", "."} for x in p.parts):
        raise InstallError("Installation path must stay inside the repository")
    target = root
    for part in p.parts:
        target /= part
        if target.is_symlink():
            raise InstallError(f"Installation refuses symlink path: {relative}")
    if target.exists() and not target.is_file() and target == root / p:
        raise InstallError(f"Installation target is not a regular file: {relative}")
    return target


def _read(root: Path, relative: str) -> str:
    p = _safe(root, relative)
    if not p.exists():
        return ""
    if p.stat().st_size > 2_000_000:
        raise InstallError(f"Installation file is too large: {relative}")
    return p.read_text(encoding="utf-8")


def _write(root: Path, relative: str, content: str, *, private: bool = False) -> None:
    target = _safe(root, relative)
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else (0o600 if private else 0o644)
    fd, temporary = tempfile.mkstemp(prefix=".devgod-write-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        _safe(root, relative)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _check_private_parents(path: Path) -> None:
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise InstallError("Private installation state refuses symlink parents")


def _private_base(state_home: Path | str | None) -> Path:
    path = Path(state_home).expanduser().absolute() if state_home is not None else Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "devgod"
    if not path.is_absolute():
        raise InstallError("Private installation state must use an absolute path")
    _check_private_parents(path / "repos")
    return path


def _backup(root: Path, relative: str, content: str, state_home: Path | str | None = None) -> str:
    from .workspace import Workspace

    workspace = Workspace(root, state_root=_private_base(state_home))
    directory = workspace.state_dir / "install-backups" / workspace.worktree_id
    name = directory / f"{relative.replace('/', '__')}.{_digest(content)[:16]}.bak"
    # Traverse with open directory handles: a replaced parent cannot redirect a
    # later backup write. Each component, including existing parents, is no-follow.
    parent_fd = os.open(directory.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in directory.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        try:
            fd = os.open(name.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            fd = os.open(name.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read(2_000_001) != content:
                    raise InstallError("Installation backup collision")
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError as exc:
        raise InstallError("Installation backup path is unavailable or unsafe") from exc
    finally:
        os.close(parent_fd)
    return str(name)


def _load(root: Path) -> dict[str, Any]:
    raw = _read(root, MANIFEST)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InstallError("Invalid DevGod install manifest; preserved for recovery") from exc
    if not isinstance(data, dict) or data.get("version") != 2:
        raise InstallError("Unsupported DevGod native installation manifest")
    if not isinstance(data.get("server"), str) or not re.fullmatch(r"devgod(?:_workflow(?:_\d+)?)?", data["server"]):
        raise InstallError("Invalid managed MCP server name")
    for field in ("files", "sections"):
        if not isinstance(data.get(field, {}), dict):
            raise InstallError("Malformed DevGod installation ownership records")
    for path, section in data.get("sections", {}).items():
        pair = {"AGENTS.md": (AGENTS_BEGIN, AGENTS_END), ".codex/config.toml": (CONFIG_BEGIN, CONFIG_END)}.get(path)
        if not pair or not isinstance(section, str):
            raise InstallError("Invalid managed section ownership")
        span = _block(section, *pair)
        if not span or section[:span[0]].strip() or section[span[1]:].strip():
            raise InstallError("Managed section ownership extends outside its markers")
    executable = data.get("executable")
    state_home = data.get("state_home")
    if (
        not isinstance(executable, list) or not executable
        or not all(isinstance(part, str) and part and not any(c in part for c in "\x00\r\n") for part in executable)
        or not Path(executable[0]).is_absolute()
        or (state_home is not None and (not isinstance(state_home, str) or not Path(state_home).is_absolute()))
        or data.get("hooks") != _hook_groups(executable, root, state_home)
    ):
        raise InstallError("Manifest hook ownership does not match the narrow DevGod integration")
    return data


def _block(text: str, begin: str, end: str) -> tuple[int, int] | None:
    # Require complete lines, a single pair, and ordered markers.
    starts = [m.start() for m in re.finditer(rf"(?m)^{re.escape(begin)}\r?$", text)]
    ends = [m.end() for m in re.finditer(rf"(?m)^{re.escape(end)}\r?$", text)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise InstallError("Ambiguous managed markers; no content was removed")
    return starts[0], ends[0]


def _section(text: str, begin: str, end: str, body: str) -> tuple[str, str]:
    replacement = f"{begin}\n{body.rstrip()}\n{end}"
    span = _block(text, begin, end)
    if span:
        return text[:span[0]] + replacement + text[span[1]:], replacement
    addition = ("\n\n" if text and not text.endswith("\n") else "\n" if text else "") + replacement + "\n"
    return text + addition, addition


def _argv(executable: str | Sequence[str] | None) -> list[str]:
    argv = [sys.executable, "-I", "-m", "devgod"] if executable is None else ([executable] if isinstance(executable, str) else list(executable))
    if not argv or not all(isinstance(s, str) and s and not any(c in s for c in "\x00\r\n") for s in argv):
        raise InstallError("Executable must be a nonempty argv vector without control characters")
    executable_path = shutil.which(argv[0]) if not Path(argv[0]).is_absolute() else argv[0]
    if not executable_path or not Path(executable_path).is_file():
        raise InstallError("DevGod executable was not found")
    argv[0] = str(Path(executable_path).absolute())
    return argv


def _hook_groups(argv: list[str], root: Path, state_home: str | None = None) -> dict[str, list[dict[str, Any]]]:
    options = ["--repo", str(root), *(["--state-home", state_home] if state_home else [])]
    command = shlex.join([*argv, *options, "hook"])
    result: dict[str, list[dict[str, Any]]] = {}
    for event in EVENTS:
        handler: dict[str, Any] = {"type": "command", "command": command, "timeout": 5}
        if event in {"SessionStart", "SubagentStart"}:
            handler["additionalContextLimit"] = 1200
        result[event] = [{"hooks": [handler]}]
    return result


def _json_members(text: str, start: int = 0) -> dict[str, tuple[int, int]]:
    """Find object value spans using the JSON decoder, preserving other bytes."""
    decoder = json.JSONDecoder()
    index = start
    while text[index].isspace():
        index += 1
    if text[index] != "{":
        raise InstallError("Expected JSON object")
    index += 1
    spans: dict[str, tuple[int, int]] = {}
    while True:
        while text[index].isspace() or text[index] == ",":
            index += 1
        if text[index] == "}":
            return spans
        key, index = decoder.raw_decode(text, index)
        while text[index].isspace() or text[index] == ":":
            index += 1
        value_start = index
        _, index = decoder.raw_decode(text, index)
        spans[key] = (value_start, index)


def _set_json(text: str, key: str, value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    members = _json_members(text)
    if key in members:
        start, end = members[key]
        return text[:start] + rendered + text[end:]
    closing = text.rfind("}")
    return text[:closing] + ("," if members else "") + "\n" + json.dumps(key) + ": " + rendered + "\n" + text[closing:]


def _merge_hooks(text: str, old: dict[str, Any], new: dict[str, Any]) -> str:
    if not text:
        text = '{\n  "hooks": {}\n}\n'
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise InstallError("Existing hooks.json is invalid; preserved") from exc
    if not isinstance(document, dict) or not isinstance(document.get("hooks", {}), dict):
        raise InstallError("Existing hooks.json must contain a hooks object")
    members = _json_members(text)
    hooks_text = text[slice(*members["hooks"])] if "hooks" in members else "{}"
    hooks = document.get("hooks", {})
    for event in dict.fromkeys([*old, *new]):
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise InstallError(f"Existing {event} hooks must be an array")
        entries = entries.copy()
        for owned in old.get(event, []):
            if owned in entries:
                entries.remove(owned)
        for entry in new.get(event, []):
            if entry not in entries:
                entries.append(entry)
        hooks_text = _set_json(hooks_text, event, entries)
    if "hooks" in members:
        start, end = members["hooks"]
        return text[:start] + hooks_text + text[end:]
    return _set_json(text, "hooks", json.loads(hooks_text))


_LEGACY_COMMANDS = {
    'bash "$(git rev-parse --show-toplevel)/scripts/devgod-session-start.sh"',
    *(f'node "$(git rev-parse --show-toplevel)/plugins/devgod/scripts/{name}.mjs"' for name in (
        "user-prompt-submit", "pre-tool-use", "permission-request", "post-tool-use", "stop",
    )),
}


def _without_toml_table(text: str, keys: tuple[str, ...]) -> str:
    """Remove only a recognized table, validating the exact semantic change."""
    before = tomllib.loads(text)
    expected = deepcopy(before)
    node = expected
    for key in keys[:-1]:
        node = node[key]
    del node[keys[-1]]
    headings = list(re.finditer(r"(?m)^\s*\[[^\n]+\][ \t]*(?:#[^\n]*)?\r?$", text))
    spans = []
    for index, match in enumerate(headings):
        try:
            parsed = tomllib.loads(match.group() + "\nx = true\n")
        except ValueError:
            continue
        node = parsed
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                break
            node = node[key]
        else:
            spans.append((match.start(), headings[index + 1].start() if index + 1 < len(headings) else len(text)))
    result = text
    for start, end in reversed(spans):
        result = result[:start] + result[end:]
    if tomllib.loads(result) != expected:
        raise InstallError("Legacy table is not safely separable; no configuration was removed")
    return result


def _legacy_changes(root: Path, state_home: Path | str | None = None) -> tuple[dict[str, str], list[str], list[str]]:
    changes: dict[str, str] = {}
    archived = []
    remove = []
    for path, begin, end in (
        ("AGENTS.md", "<!-- BEGIN DEVGOD MANAGED -->", "<!-- END DEVGOD MANAGED -->"),
        (".agents.md", "<!-- BEGIN DEVGOD KERNEL -->", "<!-- END DEVGOD KERNEL -->"),
    ):
        original = _read(root, path)
        span = _block(original, begin, end)
        if span:
            archived.append(_backup(root, path, original, state_home))
            changes[path] = original[:span[0]] + original[span[1]:]
    original = _read(root, ".codex/hooks.json")
    if original:
        document = json.loads(original)
        old: dict[str, list[dict[str, Any]]] = {}
        replacements: dict[str, list[dict[str, Any]]] = {}
        for event, entries in document.get("hooks", {}).items():
            for group in entries:
                handlers = group.get("hooks", [])
                retained = [h for h in handlers if h.get("command") not in _LEGACY_COMMANDS]
                if len(retained) != len(handlers):
                    old.setdefault(event, []).append(group)
                    if retained:
                        replacements.setdefault(event, []).append({**group, "hooks": retained})
        if old:
            archived.append(_backup(root, ".codex/hooks.json", original, state_home))
            changes[".codex/hooks.json"] = _merge_hooks(original, old, replacements)
    config = _read(root, ".codex/config.toml")
    parsed = tomllib.loads(config)
    server = parsed.get("mcp_servers", {}).get("devgod", {})
    arguments = server.get("args", []) if isinstance(server, dict) else []
    if (
        isinstance(server, dict)
        and Path(server.get("command", "")).name in {"node", "node.exe"}
        and isinstance(arguments, list) and len(arguments) >= 2
        and arguments[-1] == "mcp" and isinstance(arguments[-2], str)
        and re.search(r"(?:^|/)devgod/(?:src/admin\.ts|dist/admin\.js)$", arguments[-2].replace("\\", "/"))
        and all(x == "--experimental-strip-types" for x in arguments[:-2])
    ):
        archived.append(_backup(root, ".codex/config.toml", config, state_home))
        config = _without_toml_table(config, ("mcp_servers", "devgod"))
        changes[".codex/config.toml"] = config
    legacy_manifest = _read(root, ".devgod/install-manifest.json")
    if legacy_manifest:
        manifest = json.loads(legacy_manifest)
        if isinstance(manifest, dict) and manifest.get("version") == 1:
            for entry in manifest.get("files", []):
                if not isinstance(entry, dict) or not isinstance(entry.get("target"), str):
                    continue
                path = entry["target"]
                is_skill = re.fullmatch(r"\.agents/skills/devgod-[a-z0-9-]+/SKILL\.md", path)
                is_agent = re.fullmatch(r"\.codex/agents/[a-z0-9-]+\.toml", path)
                if not (is_skill or is_agent) or entry.get("strategy") != "replace":
                    continue
                content = _read(root, path)
                if not content or _digest(content) != entry.get("contentHash"):
                    continue
                if is_agent and "devgod" not in content.lower():
                    continue
                archived.append(_backup(root, path, content, state_home))
                remove.append(path)
    return changes, archived, remove


def init(repo: Path | str, executable: str | Sequence[str] | None = None, *, migrate: bool = False, state_home: Path | str | None = None) -> dict[str, Any]:
    """Apply intentional local setup. Repeated unchanged setup performs no writes."""
    root = _root(repo)
    argv = _argv(executable)
    previous = _load(root)
    selected_state = str(_private_base(state_home)) if state_home is not None else previous.get("state_home")
    if selected_state is not None:
        from .workspace import Workspace

        Workspace(root, state_root=_private_base(selected_state))
    pending, backups, legacy_remove = _legacy_changes(root, selected_state) if migrate else ({}, [], [])
    config = pending.get(".codex/config.toml", _read(root, ".codex/config.toml"))
    agents = pending.get("AGENTS.md", _read(root, "AGENTS.md"))
    hook_text = pending.get(".codex/hooks.json", _read(root, ".codex/hooks.json"))
    # Validate all existing input before applying the integration.
    try:
        config_data = tomllib.loads(config)
        if hook_text:
            json.loads(hook_text)
    except ValueError as exc:
        raise InstallError("Existing Codex configuration is invalid; preserved") from exc
    old_section = previous.get("sections", {}).get(".codex/config.toml", "")
    config_span = _block(config, CONFIG_BEGIN, CONFIG_END)
    preserve_config = False
    retained = []
    if config_span:
        actual = config[slice(*config_span)]
        expected = old_section.strip()
        if not previous or actual != expected:
            backups.append(_backup(root, ".codex/config.toml", config, selected_state))
            if previous:
                # An edited known server remains the effective server. Adding an
                # alias would evade deliberate per-tool approvals or restrictions.
                preserve_config = True
                retained.append(".codex/config.toml")
            else:
                config = config.replace(CONFIG_BEGIN, "# Preserved previous DevGod settings", 1).replace(CONFIG_END, "# End preserved previous DevGod settings", 1)
                config_data = tomllib.loads(config)
    server = previous.get("server", "devgod")
    existing_servers = config_data.get("mcp_servers", {})
    if not isinstance(existing_servers, dict):
        raise InstallError("mcp_servers must be a TOML table")
    if not _block(config, CONFIG_BEGIN, CONFIG_END):
        if not isinstance(server, str) or not re.fullmatch(r"devgod(?:_workflow(?:_\d+)?)?", server):
            server = "devgod"
        if server in existing_servers:
            server = "devgod_workflow"
            suffix = 2
            while server in existing_servers:
                server = f"devgod_workflow_{suffix}"
                suffix += 1
    skill_dir = previous.get("skill_dir", ".agents/skills/devgod-manager")
    if not re.fullmatch(r"\.agents/skills/devgod-manager(?:-\d+)?", str(skill_dir)):
        raise InstallError("Invalid managed skill location")
    if not previous:
        suffix = 2
        while (root / skill_dir).exists():
            skill_dir = f".agents/skills/devgod-manager-{suffix}"
            suffix += 1
    files: dict[str, str] = {}
    source = ASSETS / "devgod" / "skills" / "devgod-manager"
    for relative in ("SKILL.md", "agents/openai.yaml"):
        target = f"{skill_dir}/{relative}"
        desired = (source / relative).read_text(encoding="utf-8")
        if relative == "SKILL.md":
            desired = desired.replace("name: devgod-manager\n", f"name: {Path(skill_dir).name}\n", 1)
        current = _read(root, target)
        old_hash = previous.get("files", {}).get(target)
        if current and old_hash and _digest(current) != old_hash and current != desired:
            # User edits stay active. Preserve the upgrade variant for inspection.
            backups.append(_backup(root, target + ".new", desired, selected_state))
            retained.append(target)
            files[target] = old_hash
        else:
            pending[target] = desired
            files[target] = _digest(desired)
    body = (ASSETS / "agents-block.md").read_text().format(skill_path=f"{skill_dir}/SKILL.md")
    old_agents = previous.get("sections", {}).get("AGENTS.md", "")
    span = _block(agents, AGENTS_BEGIN, AGENTS_END)
    if span and old_agents and agents[slice(*span)] != old_agents.strip():
        # Keep user-authored additions active while replacing only our markers.
        backups.append(_backup(root, "AGENTS.md", agents, selected_state))
        agents = agents[:span[0]] + agents[slice(*span)].replace(AGENTS_BEGIN, "<!-- Preserved DevGod custom instructions -->", 1).replace(AGENTS_END, "<!-- End preserved DevGod custom instructions -->", 1) + agents[span[1]:]
    agents, agents_section = _section(agents, AGENTS_BEGIN, AGENTS_END, body)
    mcp_body = (
        f"[mcp_servers.{server}]\ncommand = {json.dumps(argv[0])}\n"
        f"args = {json.dumps([*argv[1:], '--repo', str(root), *(['--state-home', selected_state] if selected_state else []), 'mcp'])}\n"
        f"enabled_tools = {json.dumps(list(APPROVED_TOOLS))}\n"
        'default_tools_approval_mode = "auto"\nstartup_timeout_sec = 20\ntool_timeout_sec = 60\n'
    )
    mcp_body += "\n".join(
        f'[mcp_servers.{server}.tools.{name}]\napproval_mode = "approve"\n'
        for name in APPROVED_TOOLS
    )
    if preserve_config:
        # Keep the original owned text so uninstall preserves user modifications.
        config_section = old_section or f"{CONFIG_BEGIN}\n{mcp_body.rstrip()}\n{CONFIG_END}"
    else:
        config, config_section = _section(config, CONFIG_BEGIN, CONFIG_END, mcp_body)
    tomllib.loads(config)
    groups = _hook_groups(argv, root, selected_state)
    hook_text = _merge_hooks(hook_text, previous.get("hooks", {}), groups)
    pending.update({"AGENTS.md": agents, ".codex/config.toml": config, ".codex/hooks.json": hook_text})
    # Preserve the initial insertion whitespace in the ownership record on no-op upgrades.
    sections = {"AGENTS.md": agents_section, ".codex/config.toml": config_section}
    for path in sections:
        old = previous.get("sections", {}).get(path, "")
        if old and old in pending[path] and old.strip() == sections[path].strip():
            sections[path] = old
    manifest = {
        "version": 2, "server": server, "skill_dir": skill_dir, "executable": argv, "state_home": selected_state,
        "files": files, "sections": sections, "hooks": groups,
        "created": previous.get("created", {p: not _safe(root, p).exists() for p in ("AGENTS.md", ".codex/config.toml", ".codex/hooks.json")}),
    }
    for path in [*pending, MANIFEST]:
        _safe(root, path)
    changed = [p for p, value in pending.items() if _read(root, p) != value]
    for path, content in pending.items():
        _write(root, path, content)
    _write(root, MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n", private=True)
    for path in legacy_remove:
        _safe(root, path).unlink()
    return {"installed": True, "repo": str(root), "changed": changed, "server": server, "skill_path": f"{skill_dir}/SKILL.md", "backups": backups, "preserved_edits": retained, "migrated": legacy_remove, "hook_trust": "host_managed_unknown", "notes": [TRUST_NOTE]}


def uninstall(repo: Path | str) -> dict[str, Any]:
    root = _root(repo)
    manifest = _load(root)
    if not manifest:
        return {"installed": False, "removed": [], "preserved": []}
    skill_dir = manifest.get("skill_dir", "")
    if not re.fullmatch(r"\.agents/skills/devgod-manager(?:-\d+)?", str(skill_dir)):
        raise InstallError("Invalid managed skill location")
    allowed = {f"{skill_dir}/SKILL.md", f"{skill_dir}/agents/openai.yaml"}
    if set(manifest.get("files", {})) - allowed or set(manifest.get("sections", {})) - {"AGENTS.md", ".codex/config.toml"}:
        raise InstallError("Manifest contains unowned installation paths")
    for path in [*allowed, "AGENTS.md", ".codex/config.toml", ".codex/hooks.json", MANIFEST]:
        _safe(root, path)
    # Malformed user configuration must not leave a partially removed install.
    tomllib.loads(_read(root, ".codex/config.toml"))
    if _read(root, ".codex/hooks.json"):
        _merge_hooks(_read(root, ".codex/hooks.json"), manifest.get("hooks", {}), {})
    removed, preserved = [], []
    for path, digest in manifest.get("files", {}).items():
        current = _read(root, path)
        if current and _digest(current) == digest:
            _safe(root, path).unlink()
            removed.append(path)
        elif current:
            preserved.append(path)
    for path, section in manifest.get("sections", {}).items():
        current = _read(root, path)
        if not isinstance(section, str) or not section.strip():
            raise InstallError("Invalid section ownership")
        pair = (AGENTS_BEGIN, AGENTS_END) if path == "AGENTS.md" else (CONFIG_BEGIN, CONFIG_END)
        span = _block(current, *pair)
        if section in current or (span and current[slice(*span)] == section.strip()):
            if section in current:
                content = current.replace(section, "", 1)
            else:
                assert span is not None
                content = current[:span[0]] + current[span[1]:]
            if manifest.get("created", {}).get(path) and not content:
                _safe(root, path).unlink()
            else:
                if path.endswith(".toml"):
                    tomllib.loads(content)
                _write(root, path, content)
            removed.append(path + " managed section")
        elif current:
            preserved.append(path)
    current = _read(root, ".codex/hooks.json")
    if current:
        content = _merge_hooks(current, manifest.get("hooks", {}), {})
        document = json.loads(content)
        if manifest.get("created", {}).get(".codex/hooks.json") and set(document) <= {"hooks"} and all(not v for v in document.get("hooks", {}).values()):
            _safe(root, ".codex/hooks.json").unlink()
        else:
            _write(root, ".codex/hooks.json", content)
        removed.append("DevGod hook entries")
    _safe(root, MANIFEST).unlink()
    return {"installed": False, "removed": removed, "preserved": preserved, "notes": ["Private run history and installation backups are preserved."]}


def doctor(repo: Path | str) -> dict[str, Any]:
    root = _root(repo)
    problems = []
    try:
        manifest = _load(root)
        config = tomllib.loads(_read(root, ".codex/config.toml"))
        hooks = json.loads(_read(root, ".codex/hooks.json") or "{}")
    except (InstallError, ValueError) as exc:
        return {"installed": False, "ok": False, "problems": [str(exc)], "notes": [TRUST_NOTE]}
    if not manifest:
        problems.append("DevGod native integration is not installed")
    else:
        server = config.get("mcp_servers", {}).get(manifest.get("server"), {})
        if not server or server.get("default_tools_approval_mode") != "auto":
            problems.append("DevGod MCP configuration differs from the installed integration")
        if server.get("enabled_tools") != list(APPROVED_TOOLS) or any(
            server.get("tools", {}).get(name, {}).get("approval_mode") != "approve"
            for name in APPROVED_TOOLS
        ):
            problems.append("DevGod's explicit repository tool approvals are missing or edited")
        for event, groups in manifest.get("hooks", {}).items():
            if any(group not in hooks.get("hooks", {}).get(event, []) for group in groups):
                problems.append(f"DevGod {event} hook is missing or edited")
        for path, digest in manifest.get("files", {}).items():
            if _digest(_read(root, path)) != digest:
                problems.append(f"Managed skill file is missing or edited: {path}")
    notes = [TRUST_NOTE]
    if config.get("features", {}).get("hooks") is False:
        notes.append("Project configuration disables hooks; existing project controls were preserved")
    if config.get("hooks"):
        notes.append("This project also has inline hooks; Codex merges both sources")
    legacy = _read(root, ".devgod/install-manifest.json") or "<!-- BEGIN DEVGOD MANAGED -->" in _read(root, "AGENTS.md")
    if legacy:
        notes.append("Legacy DevGod artifacts detected; init --migrate archives recognized old instructions and hook handlers automatically")
    return {"installed": bool(manifest), "ok": not problems, "problems": problems, "hook_trust": "not_observable", "notes": notes}
