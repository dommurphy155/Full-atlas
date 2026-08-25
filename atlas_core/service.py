"""ServiceBackend — how Atlas services run.

Two backends:
  - DaemonBackend (default, all supported OSes): ProcessManager children,
    PID files, log files under data/logs/. Survives the parent exiting.
  - SystemdBackend (Linux opt-in): delegates to systemctl exactly as before.
    Only used when the user explicitly opts in during install.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .paths import PROJECT_ROOT, venv_python, IS_LINUX
from .process import ProcessManager, ProcSpec, tail_log

PY = None  # resolved lazily so backend works before/after venv creation


def _venv_py() -> str:
    global PY
    if PY is None:
        p = venv_python()
        PY = str(p) if p.exists() else sys.executable
    return PY


class ServiceBackend:
    names: tuple[str, ...] = ()

    def start(self, name: str) -> bool: ...
    def stop(self, name: str) -> bool: ...
    def restart(self, name: str) -> bool:
        self.stop(name)
        return self.start(name)
    def is_running(self, name: str) -> bool: ...
    def logs(self, name: str, follow: bool = True) -> None: ...


SERVICE_SPECS: dict[str, dict] = {
    # name -> (script path relative to root, env extras)
    "proxy": {
        "argv_module": ("-m", "proxy.main"),
        "desc": "Atlas proxy",
    },
    "openrouter": {
        "argv_script": "openrouter/scripts/scheduler.py",
        "argv_args": ("--runs", "0", "--delay", "240"),
        "desc": "OpenRouter signup automation",
    },
    "nvidia": {
        "argv_script": "nvidia/scripts/main.py",
        "argv_args": (),
        "desc": "NVIDIA signup automation",
    },
    "huggingface": {
        "argv_script": "huggingface/scripts/hf_scheduler.py",
        "argv_args": ("--runs", "0", "--delay", "240"),
        "desc": "HuggingFace signup automation",
    },
}


def _spec_for(name: str) -> ProcSpec:
    s = SERVICE_SPECS[name]
    if "argv_module" in s:
        argv = [_venv_py(), *s["argv_module"]]
    else:
        argv = [_venv_py(), str(PROJECT_ROOT / s["argv_script"]), *s["argv_args"]]
    return ProcSpec(name=name, argv=argv)


class DaemonBackend(ServiceBackend):
    """Default everywhere. No systemd, no journalctl."""

    def __init__(self) -> None:
        self.pm = ProcessManager()

    def start(self, name: str) -> bool:
        handle, started = self.pm.start(_spec_for(name))
        return True  # running either way; caller can check is_running()

    def stop(self, name: str) -> bool:
        return self.pm.spec_handle(name).stop()

    def is_running(self, name: str) -> bool:
        return self.pm.spec_handle(name).is_running()

    def logs(self, name: str, follow: bool = True) -> None:
        tail_log(name, follow=follow)


class SystemdBackend(ServiceBackend):
    """Linux opt-in. Same unit names and behaviour as the original CLI."""

    def __init__(self) -> None:
        if not shutil.which("systemctl"):
            raise RuntimeError("systemctl not available")

    def _unit(self, name: str) -> str:
        units = {
            "proxy": "atlas-proxy.service",
            "openrouter": "openrouter-signup.service",
            "nvidia": "nvidia-automation.service",
            "huggingface": "huggingface-automation.service",
        }
        return units[name]

    def _sc(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", *args], capture_output=True, text=True, check=False
        )

    def start(self, name: str) -> bool:
        return self._sc("start", self._unit(name)).returncode == 0

    def stop(self, name: str) -> bool:
        return self._sc("stop", self._unit(name)).returncode == 0

    def is_running(self, name: str) -> bool:
        r = self._sc("is-active", self._unit(name))
        return r.stdout.strip() == "active"

    def logs(self, name: str, follow: bool = True) -> None:
        cmd = ["journalctl", "-u", self._unit(name)] + (["-f"] if follow else ["-n", "50"])
        try:
            subprocess.run(cmd, check=False)
        except KeyboardInterrupt:
            pass


def get_backend(prefer_systemd: bool = False) -> ServiceBackend:
    """Daemon everywhere unless the user explicitly opted into systemd."""
    if prefer_systemd and IS_LINUX and shutil.which("systemctl"):
        try:
            return SystemdBackend()
        except RuntimeError:
            pass
    return DaemonBackend()
