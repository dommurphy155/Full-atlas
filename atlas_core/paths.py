"""OS-aware paths. POSIX only (Linux/macOS); Windows is rejected upstream."""

from __future__ import annotations

import os
import platform
import sys
import tempfile
from pathlib import Path


def _find_project_root() -> Path:
    # atlas_core/ lives directly under the project root.
    here = Path(__file__).resolve().parent.parent
    if (here / "proxy" / "main.py").exists():
        return here
    return Path.cwd().resolve()


PROJECT_ROOT = _find_project_root()
DATA_DIR = PROJECT_ROOT / "data"
RUN_DIR = DATA_DIR / "run"
LOG_DIR = DATA_DIR / "logs"

IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
SUPPORTED = IS_MACOS or IS_LINUX


def ensure_runtime_dirs() -> None:
    """Create runtime directories if missing. Idempotent."""
    for d in (DATA_DIR, RUN_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def venv_python(venv_dir: Path | None = None) -> Path:
    """Path to the venv's python interpreter. Same layout on Linux and macOS."""
    venv_dir = venv_dir or (PROJECT_ROOT / ".venv")
    return venv_dir / "bin" / "python"


def default_chrome_candidates() -> list[str]:
    """Platform-appropriate Chrome/Chromium search order."""
    if IS_MACOS:
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
        ]
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]


def atomic_write_600(path: Path, text: str) -> None:
    """Write file atomically with 0600 perms. chmod failure is non-fatal
    (e.g. exotic filesystems) — content correctness matters more."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".atlas-tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def assert_supported() -> None:
    """Hard gate: refuse to run setup on unsupported platforms."""
    if SUPPORTED:
        return
    raise SystemExit(
        f"Atlas does not support {platform.system()}.\n"
        "Supported platforms: Linux, macOS.\n"
        "On Windows, use WSL2: https://learn.microsoft.com/en-us/windows/wsl/install"
    )
