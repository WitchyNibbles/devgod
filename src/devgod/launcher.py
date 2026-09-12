"""Linux subreaper for one pinned Codex runtime, launched with Python ``-I``.

Only Codex executes repository commands. This supervisor inherits its protocol
stdio and stays alive until all descendants, including detached ones, are reaped.
"""

from __future__ import annotations

import ctypes
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _birth(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (FileNotFoundError, ProcessLookupError):
        return None


def _children() -> list[int]:
    return [
        int(value) for value in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
    ]


def _kill_children() -> None:
    """Reparenting to a live subreaper catches descendants that called setsid."""
    while True:
        for pid in _children():
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                if int(stat[1]) == os.getpid():
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
            finally:
                os.close(descriptor)
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return  # ECHILD is the only successful completion condition.
        if pid == 0:
            time.sleep(0.02)


def supervise(command: list[str], receipt_dir: Path, nonce: str) -> int:
    """Internal supervisor primitive; the executable entry fixes the command."""
    stop_requested = False

    def request_stop(_signal: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, request_stop)
    parent_pid = os.getppid()
    parent_birth = _birth(parent_pid)
    libc = ctypes.CDLL(None, use_errno=True)
    for option, value in ((36, 1), (1, signal.SIGTERM)):
        if libc.prctl(option, value, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot establish the Codex supervisor lifecycle")
    if os.getppid() != parent_pid or _birth(parent_pid) != parent_birth:
        stop_requested = True
    os.setsid()
    own_birth = _birth(os.getpid())
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    exit_code = 1
    try:
        if not stop_requested:
            primary = subprocess.Popen(command)
            while not stop_requested:
                code = primary.poll()
                if code is not None:
                    exit_code = code
                    break
                time.sleep(0.02)
    finally:
        _kill_children()
    receipt = {
        "nonce": nonce,
        "pid": os.getpid(),
        "start_ticks": own_birth,
        "boot_id": boot_id,
        "descendants_reaped": True,
    }
    descriptor = os.open(
        receipt_dir / "stopped.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "w") as handle:
        json.dump(receipt, handle)
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(receipt_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return exit_code


def main() -> None:
    if sys.platform != "linux":
        raise SystemExit("DevGod managed execution requires Linux subreaper support.")
    if len(sys.argv) != 3:
        raise SystemExit("DevGod supervisor requires its private receipt directory and nonce.")
    receipt_dir = Path(sys.argv[1])
    nonce = sys.argv[2]
    if not receipt_dir.is_absolute() or not receipt_dir.is_dir() or receipt_dir.is_symlink():
        raise SystemExit("Invalid DevGod supervisor receipt directory.")
    if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
        raise SystemExit("Invalid DevGod supervisor nonce.")
    if importlib.metadata.version("openai-codex-cli-bin") != "0.154.0":
        raise SystemExit("DevGod requires the locked Codex runtime 0.154.0.")
    from codex_cli_bin import bundled_codex_path, bundled_path_dir  # type: ignore[import-untyped]

    runtime = str(bundled_codex_path())
    helper_dir = bundled_path_dir()
    if helper_dir is not None:
        os.environ["PATH"] = str(helper_dir) + os.pathsep + os.environ.get("PATH", "")
    raise SystemExit(supervise([runtime, "app-server", "--listen", "stdio://"], receipt_dir, nonce))


if __name__ == "__main__":
    main()
