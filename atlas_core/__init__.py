"""atlas_core — platform abstraction layer for the Atlas Bundle.

POSIX-only by design (Linux + macOS). Windows is explicitly unsupported.

Modules:
    paths    — OS-aware filesystem locations (venv python, run dirs, log dirs)
    process  — ProcessManager: spawn/supervise/stop background processes via PID files
    service  — ServiceBackend: background-daemon (default) and systemd (Linux opt-in)
    display  — display detection + Chrome binary discovery for CDP launches
"""

from .paths import (
    PROJECT_ROOT,
    DATA_DIR,
    RUN_DIR,
    LOG_DIR,
    venv_python,
    atomic_write_600,
)

from .process import ProcessManager, ProcSpec
from .service import ServiceBackend, get_backend
from .display import find_chrome, resolve_display, build_chrome_command

__version__ = "1.0.0"
