"""Git worktree boundaries and content-addressed, read-only review candidates.

Controller Git operations never check out files, run hooks, or use external diff
drivers. Candidate reads use descriptor-relative, no-follow traversal; symlinks
are represented by their link text, never by the contents of their targets.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .models import Candidate


class WorkspaceError(RuntimeError):
    """An actionable repository or candidate-boundary failure."""


class SourceChangedError(WorkspaceError):
    """The candidate changed while evidence was being captured."""


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _digest(value: Any) -> str:
    data = json.dumps(
        _jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


class Workspace:
    """A worktree plus private state shared only by its canonical Git repository.

    ``state_root`` overrides the DevGod state base (before ``repos/<repo_id>``).
    Exclusions must be exact, explicitly declared generated paths, not globs.
    Repository policy, including AGENTS and .codex configuration, is included.
    """

    def __init__(
        self,
        repo_root: str | Path,
        state_root: str | Path | None = None,
        *,
        generated_paths: tuple[str, ...] = (),
        max_file_bytes: int = 64 * 1024 * 1024,
        max_total_bytes: int = 512 * 1024 * 1024,
        max_files: int = 100_000,
    ) -> None:
        requested = Path(repo_root).expanduser().resolve()
        self.root = requested
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_files = max_files
        if min(max_file_bytes, max_total_bytes, max_files) <= 0:
            raise WorkspaceError("Candidate size limits must be positive.")
        self.generated_paths = frozenset(self._safe_path(p) for p in generated_paths)
        try:
            top = self._git("rev-parse", "--show-toplevel").strip()
            discovered_root = Path(os.fsdecode(top)).resolve()
            common = self._git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
            if not _inside(requested, discovered_root):
                raise WorkspaceError("Git configuration redirected the requested repository outside its authorized directory.")
            if discovered_root != requested:
                discovered_common = self._git(
                    "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=discovered_root,
                ).strip()
                if Path(os.fsdecode(discovered_common)).resolve() != Path(os.fsdecode(common)).resolve():
                    raise WorkspaceError("Git configuration redirected the requested directory into a different repository.")
            self.root = discovered_root
        except WorkspaceError as exc:
            raise WorkspaceError(
                f"DevGod needs a working Git repository at {requested}. "
                "Select the consuming repository's working directory; bare repositories "
                "cannot provide an implementation candidate. " + str(exc)
            ) from exc
        self.git_common_dir = Path(os.fsdecode(common)).resolve()
        self.repo_id = _digest({"git_common_dir": str(self.git_common_dir)})
        self.worktree_id = _digest({"repo_id": self.repo_id, "root": str(self.root)})
        default = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "devgod"
        base = Path(state_root).expanduser() if state_root is not None else default
        if not base.is_absolute():
            raise WorkspaceError("DevGod state_root / XDG_STATE_HOME must be an absolute path.")
        base = base.resolve()
        self.state_dir = base / "repos" / self.repo_id
        worktrees = [self.root]
        for record in self._git("worktree", "list", "--porcelain", "-z").split(b"\0"):
            if record.startswith(b"worktree "):
                worktrees.append(Path(os.fsdecode(record[9:])).resolve())
        if any(_inside(self.state_dir.resolve(), tree) for tree in worktrees):
            raise WorkspaceError(
                "Private DevGod state must be outside every implementation worktree; "
                "use the default XDG state directory or another external state_root."
            )
        if self.state_dir.is_symlink():
            raise WorkspaceError("Private DevGod state cannot be a symlink.")
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.state_dir.stat().st_uid != os.getuid():
            raise WorkspaceError("Private DevGod state must belong to the current user.")
        self.state_dir.chmod(0o700)

    def _git(self, *args: str, cwd: Path | None = None, ok: tuple[int, ...] = (0,)) -> bytes:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        # Keep ordinary configured ignore files and safe.directory settings.
        # Unsafe command-capable settings are disabled explicitly below.
        env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        command_prefix = [
            "git",
            "--no-pager",
            "--no-optional-locks",
            "--literal-pathspecs",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "diff.external=",
            "-c",
            "submodule.recurse=false",
        ]
        try:
            # Even read-only `git status` may run attribute-selected clean or
            # long-running process filters. Read only their configuration names
            # (which does not execute filters) and disable each command for this
            # controller invocation. Environment config keys preserve arbitrary
            # subsection names without shell or `-c key=value` parsing ambiguity.
            configured = subprocess.run(
                [*command_prefix, "config", "--null", "--name-only", "--get-regexp",
                 r"^filter\..*\.(clean|smudge|process|required)$"],
                cwd=cwd or self.root, env=env, capture_output=True, timeout=30, check=False,
            )
            if configured.returncode not in (0, 1):
                raise WorkspaceError("Git filter configuration could not be inspected safely.")
            if len(configured.stdout) > 262_144:
                raise WorkspaceError("Git filter configuration exceeds the controller safety limit.")
            filters = sorted(set(configured.stdout.split(b"\0")) - {b""})
            if filters:
                env["GIT_CONFIG_COUNT"] = str(len(filters))
                for index, raw_key in enumerate(filters):
                    key = os.fsdecode(raw_key)
                    env[f"GIT_CONFIG_KEY_{index}"] = key
                    env[f"GIT_CONFIG_VALUE_{index}"] = "false" if key.endswith(".required") else ""
            result = subprocess.run(
                [*command_prefix, *args], cwd=cwd or self.root, env=env,
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"Git repository inspection failed: {exc}") from exc
        if result.returncode not in ok:
            error = os.fsdecode(result.stderr[:4096]).strip()
            raise WorkspaceError(f"Git {args[0]} failed: {error}")
        return result.stdout

    def identity(self) -> dict[str, str]:
        return {
            "repo_id": self.repo_id,
            "worktree_id": self.worktree_id,
            "repo_root": str(self.root),
            "git_common_dir": str(self.git_common_dir),
        }

    def _revision(self) -> tuple[str, str]:
        try:
            head = self._git("rev-parse", "--verify", "HEAD").decode("ascii").strip()
        except WorkspaceError as exc:
            raise WorkspaceError(
                "This repository has no initial commit to anchor a delivery branch. "
                "Run design against an existing Git history before starting delivery."
            ) from exc
        branch = os.fsdecode(
            self._git("symbolic-ref", "--quiet", "--short", "HEAD", ok=(0, 1))
        ).strip()
        return branch or "HEAD", head

    def provenance(self) -> dict[str, Any]:
        branch, head = self._revision()
        status = self._git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"
        )
        records = status.split(b"\0")
        paths: set[str] = set()
        entries: list[dict[str, str]] = []
        i = 0
        while i < len(records):
            record = records[i]
            i += 1
            if not record:
                continue
            code, path = os.fsdecode(record[:2]), os.fsdecode(record[3:])
            paths.add(path)
            entry = {"status": code, "path": path}
            if "R" in code or "C" in code:
                if i >= len(records) or not records[i]:
                    raise WorkspaceError("Git returned an incomplete rename record.")
                entry["original_path"] = os.fsdecode(records[i])
                paths.add(entry["original_path"])
                i += 1
            entries.append(entry)
        return {
            **self.identity(),
            "branch": branch,
            "base_revision": head,
            "head_revision": head,
            "baseline_dirty_paths": sorted(paths),
            "baseline_status": entries,
            "baseline_index_digest": hashlib.sha256(
                self._git("ls-files", "--stage", "-z")
            ).hexdigest(),
        }

    def ensure_branch(self, name: str | None = None) -> dict[str, Any]:
        baseline = self.provenance()
        current = baseline["branch"]
        if name is None and current.startswith("devgod/"):
            return baseline
        name = name or f"devgod/work-{secrets.token_hex(6)}"
        if name.startswith("-"):
            raise WorkspaceError("Delivery branch names cannot begin with '-'.")
        self._git("check-ref-format", "--branch", name)
        if name == current:
            return baseline
        # Never checkout/reset files. A new reference at HEAD plus symbolic-ref
        # changes branch ownership while leaving every index/worktree byte intact.
        if self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", ok=(0, 1)):
            name = f"{name}-{secrets.token_hex(4)}"
        self._git("branch", "--no-track", name, baseline["head_revision"])
        if self._revision() != (current, baseline["head_revision"]):
            raise SourceChangedError(
                "The active branch moved during delivery setup; re-read repository state and retry."
            )
        self._git(
            "symbolic-ref", "-m", "devgod: select delivery branch", "HEAD", f"refs/heads/{name}"
        )
        return {**baseline, "original_branch": current, "branch": name}

    @staticmethod
    def _safe_path(value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or "\0" in value
            or path.is_absolute()
            or any(p in ("..", ".git") for p in path.parts)
        ):
            raise WorkspaceError(f"Unsafe candidate path: {value!r}")
        if path.as_posix() != value or value == ".":
            raise WorkspaceError(f"Candidate paths must be normalized relative paths: {value!r}")
        return value

    def _parent_fd(self, relative: str, root: Path | None = None) -> tuple[int, str]:
        parts = PurePosixPath(self._safe_path(relative)).parts
        fd = os.open(root or self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def _enumerate(
        self, cwd: Path | None = None, prefix: str = "", depth: int = 0
    ) -> dict[str, dict[str, Any] | None]:
        if depth > 16:
            raise WorkspaceError("Submodule nesting exceeds the candidate safety limit (16).")
        cwd = cwd or self.root
        staged = self._git("ls-files", "--stage", "-z", cwd=cwd)
        entries: dict[str, dict[str, Any] | None] = {}
        for record in staged.split(b"\0"):
            if not record:
                continue
            metadata, raw = record.split(b"\t", 1)
            mode, revision, stage = metadata.split()
            path = self._safe_path(prefix + os.fsdecode(raw))
            if stage != b"0":
                raise WorkspaceError(
                    "Resolve the repository's merge conflicts before freezing verification evidence."
                )
            if mode == b"160000":
                entry: dict[str, Any] = {
                    "kind": "gitlink",
                    "mode": "160000",
                    "revision": revision.decode("ascii"),
                }
                fd = None
                try:
                    fd, leaf = self._parent_fd(path)
                    info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        raise WorkspaceError(f"Submodule path must be a real directory: {path!r}")
                    subroot = self.root / path
                    top = self._git("rev-parse", "--show-toplevel", cwd=subroot).strip()
                    discovered = Path(os.fsdecode(top)).resolve()
                    if discovered == subroot:
                        entry["revision"] = (
                            self._git("rev-parse", "HEAD", cwd=subroot).decode("ascii").strip()
                        )
                        entries.update(self._enumerate(subroot, path + "/", depth + 1))
                    elif discovered != cwd:
                        raise WorkspaceError(
                            f"Submodule working directory escapes the candidate: {path!r}"
                        )
                except FileNotFoundError:
                    entry["missing"] = True
                finally:
                    if fd is not None:
                        os.close(fd)
                entries[path] = entry
            else:
                entries[path] = None
        for raw in self._git("ls-files", "--others", "--exclude-standard", "-z", cwd=cwd).split(
            b"\0"
        ):
            if raw:
                if raw.endswith(b"/"):
                    # Git reports an untracked nested repository as a directory.
                    # Retain its source using its own ignore rules and metadata.
                    path = self._safe_path(prefix + os.fsdecode(raw[:-1]))
                    fd, leaf = self._parent_fd(path)
                    try:
                        if not stat.S_ISDIR(
                            os.stat(leaf, dir_fd=fd, follow_symlinks=False).st_mode
                        ):
                            raise WorkspaceError(
                                f"Nested repository path must be a real directory: {path!r}"
                            )
                    finally:
                        os.close(fd)
                    nested = self.root / path
                    if (
                        Path(
                            os.fsdecode(
                                self._git("rev-parse", "--show-toplevel", cwd=nested)
                            ).strip()
                        ).resolve()
                        != nested
                    ):
                        raise WorkspaceError(
                            f"Git returned an unexpected untracked directory: {path!r}"
                        )
                    nested_revision = (
                        self._git("rev-parse", "--verify", "--quiet", "HEAD", cwd=nested, ok=(0, 1))
                        .decode("ascii")
                        .strip()
                    )
                    entries[path] = {
                        "kind": "gitlink",
                        "mode": "160000",
                        "revision": nested_revision,
                    }
                    entries.update(self._enumerate(nested, path + "/", depth + 1))
                else:
                    entries[self._safe_path(prefix + os.fsdecode(raw))] = None
        return entries

    def _read_entry(
        self, relative: str, root: Path | None = None
    ) -> tuple[dict[str, Any], bytes | str | None]:
        fd = None
        try:
            fd, leaf = self._parent_fd(relative, root)
            before = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                target = os.readlink(leaf, dir_fd=fd)
                return {"kind": "symlink", "mode": "120000", "target": target}, target
            if not stat.S_ISREG(before.st_mode):
                raise WorkspaceError(
                    f"Candidate contains an unsupported special file: {relative!r}"
                )
            file_fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_size > self.max_file_bytes:
                    raise WorkspaceError(
                        f"Candidate file exceeds the regular-file limit: {relative!r}"
                    )
                data = stream.read(self.max_file_bytes + 1)
                after = os.fstat(stream.fileno())

            def key(s: os.stat_result) -> tuple[int, ...]:
                return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

            if key(before) != key(opened) or key(opened) != key(after):
                raise SourceChangedError(
                    f"Source changed while reading {relative!r}; recapture the candidate."
                )
            if len(data) > self.max_file_bytes:
                raise WorkspaceError(f"Candidate file exceeds max_file_bytes: {relative!r}")
            return {
                "kind": "file",
                "mode": "100755" if before.st_mode & 0o111 else "100644",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }, data
        except FileNotFoundError:
            return {"kind": "missing"}, None
        except OSError as exc:
            raise WorkspaceError(
                f"Unsafe or unreadable candidate path {relative!r}: {exc}"
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)

    def _scan(
        self, destination: Path | None = None, relocated: dict[str, str] | None = None
    ) -> dict[str, dict[str, Any]]:
        entries = self._enumerate()
        if len(entries) > self.max_files:
            raise WorkspaceError(
                f"Candidate exceeds max_files ({self.max_files}); adjust the explicit workspace limit."
            )
        manifest: dict[str, dict[str, Any]] = {}
        total = 0
        for path in sorted(entries):
            if path in self.generated_paths:
                continue
            entry, data = (
                (entries[path], None) if entries[path] is not None else self._read_entry(path)
            )
            assert entry is not None
            manifest[path] = entry
            total += entry.get("size", 0) + len(os.fsencode(entry.get("target", "")))
            if total > self.max_total_bytes:
                raise WorkspaceError(
                    f"Candidate exceeds max_total_bytes ({self.max_total_bytes}); adjust the explicit workspace limit."
                )
            if destination is not None and entry["kind"] != "missing":
                target = destination / (relocated or {}).get(path, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if entry["kind"] == "gitlink":
                    target.mkdir(exist_ok=True)
                elif entry["kind"] == "symlink":
                    # Strict validation also runs after all links are present.
                    link = Path(str(data))
                    logical = Path(os.path.abspath(self.root / Path(path).parent / link))
                    try:
                        resolved = (target.parent / link).resolve()
                    except RuntimeError as exc:
                        raise WorkspaceError(
                            f"Review snapshot contains a symlink cycle: {path!r}"
                        ) from exc
                    if (
                        link.is_absolute()
                        or not _inside(logical, self.root)
                        or not _inside(resolved, destination)
                    ):
                        raise WorkspaceError(f"Review snapshot symlink escapes its root: {path!r}")
                    target.symlink_to(str(data))
                else:
                    with target.open("xb") as stream:
                        stream.write(data)  # type: ignore[arg-type]
                    target.chmod(0o555 if entry["mode"] == "100755" else 0o444)
                    if hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
                        raise SourceChangedError(f"Snapshot bytes changed while copying {path!r}.")
        return manifest

    def manifest(self) -> dict[str, dict[str, Any]]:
        return self._scan()

    def changed_paths(self, baseline: dict[str, Any]) -> tuple[str, ...]:
        current = self.manifest()
        return tuple(
            p for p in sorted(baseline.keys() | current.keys()) if baseline.get(p) != current.get(p)
        )

    def _candidate(
        self,
        manifest: dict[str, Any],
        checks: Any,
        policy: Any,
        base_revision: str | None,
        plan: Any,
        snapshot: Path | None = None,
    ) -> Candidate:
        branch, head = self._revision()
        checks_digest = _digest({"checks": checks, "policy": policy, "plan": plan})
        fields = {
            "repo_id": self.repo_id,
            "worktree_id": self.worktree_id,
            "repo_root": str(self.root),
            "branch": branch,
            "head_revision": head,
            "base_revision": base_revision or head,
            "checks_digest": checks_digest,
        }
        return Candidate(
            **fields,
            candidate_digest=_digest({**fields, "files": manifest}),
            snapshot_path=str(snapshot) if snapshot else None,
        )

    def fingerprint(
        self,
        checks: Any = (),
        policy: Any = None,
        *,
        base_revision: str | None = None,
        plan: Any = None,
    ) -> Candidate:
        before = self._revision()
        candidate = self._candidate(self.manifest(), checks, policy, base_revision, plan)
        if before != (candidate.branch, candidate.head_revision):
            raise SourceChangedError(
                "Repository revision changed while fingerprinting; recapture the candidate."
            )
        return candidate

    def snapshot(
        self,
        checks: Any = (),
        policy: Any = None,
        *,
        base_revision: str | None = None,
        plan: Any = None,
    ) -> Candidate:
        before_revision = self._revision()
        before = self.manifest()
        parent = self.state_dir / "snapshots"
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink():
            raise WorkspaceError("Snapshot storage cannot be a symlink.")
        destination = parent / secrets.token_hex(16)
        destination.mkdir(mode=0o700)
        sidecar = destination.with_suffix(".json")
        inert = f"devgod-review-inputs-{destination.name}"
        relocated = {
            path: f"{inert}/{hashlib.sha256(os.fsencode(path)).hexdigest()}.review-data"
            for path in before
            if ".codex" in PurePosixPath(path).parts
        }
        try:
            copied = self._scan(destination, relocated)
            after = self.manifest()
            if before != copied or copied != after or before_revision != self._revision():
                raise SourceChangedError(
                    "Source changed while freezing the review snapshot; recapture and rerun verification."
                )
            for path, entry in copied.items():
                if entry["kind"] == "symlink":
                    try:
                        resolved = (destination / relocated.get(path, path)).resolve()
                    except RuntimeError as exc:
                        raise WorkspaceError(
                            f"Review snapshot contains a symlink cycle: {path!r}"
                        ) from exc
                    if not _inside(resolved, destination):
                        raise WorkspaceError(f"Review snapshot symlink escapes its root: {path!r}")
            for directory, _, _ in os.walk(destination, followlinks=False, topdown=False):
                Path(directory).chmod(0o555)
            candidate = self._candidate(copied, checks, policy, base_revision, plan, destination)
            if before_revision != (candidate.branch, candidate.head_revision):
                raise SourceChangedError(
                    "Repository revision changed while sealing the snapshot; recapture it."
                )
            metadata = {
                "candidate_digest": candidate.candidate_digest,
                "files": copied,
                "relocated_paths": relocated,
            }
            with sidecar.open("x", encoding="utf-8") as stream:
                json.dump(
                    metadata, stream, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                )
            sidecar.chmod(0o400)
            self.verify_snapshot(candidate)
            return candidate
        except BaseException:
            for directory, _, _ in os.walk(destination, followlinks=False):
                Path(directory).chmod(0o700)
            shutil.rmtree(destination)
            sidecar.unlink(missing_ok=True)
            raise

    def _snapshot_metadata(self, candidate: Candidate) -> tuple[Path, dict[str, Any]]:
        if (
            candidate.repo_id != self.repo_id
            or candidate.worktree_id != self.worktree_id
            or candidate.repo_root != str(self.root)
            or not candidate.snapshot_path
        ):
            raise WorkspaceError("Review snapshot does not belong to this worktree.")
        destination = Path(candidate.snapshot_path)
        parent = self.state_dir / "snapshots"
        if (
            destination.parent != parent
            or len(destination.name) != 32
            or any(c not in "0123456789abcdef" for c in destination.name)
            or destination.is_symlink()
            or not destination.is_dir()
            or parent.is_symlink()
        ):
            raise WorkspaceError(
                "Review snapshot path escapes or is missing from private snapshot storage."
            )
        try:
            fd = os.open(destination.with_suffix(".json"), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(self.max_total_bytes + 1)
            if len(raw) > self.max_total_bytes:
                raise WorkspaceError("Review snapshot manifest exceeds its size limit.")
            metadata = json.loads(raw)
            if (
                not isinstance(metadata, dict)
                or metadata.get("candidate_digest") != candidate.candidate_digest
            ):
                raise ValueError("candidate digest mismatch")
            fields = candidate.model_dump(exclude={"candidate_digest", "snapshot_path"})
            if _digest({**fields, "files": metadata["files"]}) != candidate.candidate_digest:
                raise ValueError("manifest digest mismatch")
            for path, stored in metadata["relocated_paths"].items():
                self._safe_path(path)
                self._safe_path(stored)
            return destination, metadata
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WorkspaceError(f"Review snapshot manifest is missing or invalid: {exc}") from exc

    def snapshot_context(self, candidate: Candidate) -> dict[str, Any]:
        """Explain source policies retained as inert data instead of active config."""
        _, metadata = self._snapshot_metadata(candidate)
        return {
            "relocated_paths": metadata["relocated_paths"],
            "note": "Relocated Codex configuration is original source data for review; "
            "report findings using its original path. It is not executable reviewer configuration.",
        }

    def verify_snapshot(self, candidate: Candidate) -> None:
        """Reject missing, altered, or added snapshot content before accepting review."""
        destination, metadata = self._snapshot_metadata(candidate)
        manifest = metadata["files"]
        relocated = metadata["relocated_paths"]
        expected = set()
        for path, entry in manifest.items():
            stored = relocated.get(path, path)
            self._safe_path(stored)
            if entry["kind"] == "gitlink":
                location = destination / stored
                if location.is_symlink() or not location.is_dir():
                    raise SourceChangedError(
                        f"Review snapshot submodule directory changed: {path!r}"
                    )
                continue
            observed, _ = self._read_entry(stored, destination)
            if observed != entry:
                raise SourceChangedError(
                    f"Review snapshot content changed: {path!r}; recapture and rerun review."
                )
            if entry["kind"] != "missing":
                expected.add(stored)
        observed_paths = set()
        for directory, dirs, files in os.walk(destination, followlinks=False):
            for name in files + [name for name in dirs if (Path(directory) / name).is_symlink()]:
                observed_paths.add((Path(directory) / name).relative_to(destination).as_posix())
        if expected != observed_paths:
            raise SourceChangedError(
                "Review snapshot gained or lost files; recapture and rerun review."
            )
