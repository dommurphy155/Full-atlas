"""ProcessManager — spawn, supervise, and stop background processes.

POSIX-only. Replaces pgrep/pkill/systemd for the default (non-systemd)
service backend. State lives in data/run/*.pid; logs in data/logs/.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import RUN_DIR, LOG_DIR, ensure_runtime_dirs


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. POSIX kill(0)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    except OSError as e:
        return e.errno != errno.ESRCH
    return True


@dataclass
class ProcSpec:
    name: str
    argv: list[str]
    env_extra: dict[str, str] | None = None
    cwd: Path | None = None


class ManagedProcess:
    """Handle for one supervised background process."""

    def __init__(self, name: str, pid_file: Path, log_file: Path):
        self.name = name
        self.pid_file = pid_file
        self.log_file = log_file

    def pid(self) -> int | None:
        try:
            return int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def is_running(self) -> bool:
        pid = self.pid()
        if pid is None or pid == os.getpid():
            return False
        if not _pid_alive(pid):
            # stale pid file
            try:
                self.pid_file.unlink()
            except OSError:
                pass
            return False
        return True

    def stop(self, timeout: float = 10.0) -> bool:
        """SIGTERM, wait, then SIGKILL. Returns True when nothing is left running."""
        pid = self.pid()
        if not self.is_running() or pid is None:
            return True
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.2)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.pid_file.unlink()
        except OSError:
            pass
        return True


class ProcessManager:
    def __init__(self) -> None:
        ensure_runtime_dirs()

    def spec_handle(self, name: str) -> ManagedProcess:
        return ManagedProcess(name, RUN_DIR / f"{name}.pid", LOG_DIR / f"{name}.log")

    def start(self, spec: ProcSpec, overwrite: bool = False) -> tuple[ManagedProcess, bool]:
        """Start a background process. Returns (handle, started).

        started=False means it was already running (or overwrite=False hit an
        existing live process).
        """
        handle = self.spec_handle(spec.name)
        if handle.is_running():
            if not overwrite:
                return handle, False
            handle.stop()

        log_f = open(handle.log_file, "ab", buffering=0)
        env = {**os.environ, **(spec.env_extra or {})}
        # start_new_session detaches from our process group so children
        # survive the installer exiting.
        proc = subprocess.Popen(
            spec.argv,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=str(spec.cwd or PROJECT_ROOT_FALLBACK),
            start_new_session=True,
        )
        handle.pid_file.write_text(f"{proc.pid}\n")
        return handle, True

    def status(self, names: list[str]) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []
        for n in names:
            h = self.spec_handle(n)
            out.append((n, h.is_running()))
        return out


# Avoid circulars: process.py needs a root for cwd default only.
PROJECT_ROOT_FALLBACK: Path = RUN_DIR.parent.parent


# --------------------------------------------------------------------------
# Log tailing (replaces journalctl -f)
# --------------------------------------------------------------------------

def tail_log(name: str, follow: bool = False, lines: int = 50) -> None:
    """Print the last `lines` of a service log; optionally follow like tail -f."""
    log_path = LOG_DIR / f"{name}.log"
    if not log_path.exists():
        print(f"No log file yet: {log_path}")
        return
    with open(log_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 65536))
        chunk = f.read().decode(errors="replace")
        buf = chunk.splitlines()
        for line in buf[-lines:]:
            print(line)
    if not follow:
        return
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line.decode(errors="replace"))
                    sys.stdout.flush()
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        pass
