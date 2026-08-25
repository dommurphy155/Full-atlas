"""Display + Chrome discovery for CDP launches.

Ports launch_cdp.sh's display policy to Python, cross-platform:

  CDP_HEADLESS=1          -> always headless
  usable display present  -> headed (visible via VNC / native window server)
  otherwise               -> headless

macOS needs nothing extra — Chrome opens on the real window server.
Linux uses DISPLAY when a real X server answers; never spawns Xvfb itself
(attach to an existing display, don't create one).
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .paths import default_chrome_candidates, IS_MACOS


def find_chrome() -> str | None:
    """Locate a usable Chrome/Chromium binary. CHROME_BIN wins."""
    env_bin = os.environ.get("CHROME_BIN")
    candidates = ([env_bin] if env_bin else []) + default_chrome_candidates()
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    # PATH fallback (homebrew cask links, etc.)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        which = shutil.which(name)
        if which:
            return which
    return None


def _display_usable(display: str) -> bool:
    """Can we actually talk to this X display?"""
    if not shutil.which("xdpyinfo"):
        # macOS: assume yes if DISPLAY set (XQuartz) — else irrelevant
        return bool(IS_MACOS)
    env = {**os.environ, "DISPLAY": display}
    try:
        return subprocess.run(
            ["xdpyinfo"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def resolve_display() -> tuple[str | None, bool]:
    """Returns (display_or_None, headed?).

    On macOS there is no DISPLAY concept for Chrome — returns (None, True)
    since the window server is always available for a GUI session.
    """
    forced = os.environ.get("CDP_HEADLESS", "")
    headless_requested = forced.lower() in ("1", "true", "yes", "on")

    if IS_MACOS:
        return None, not headless_requested

    requested = os.environ.get("CDP_DISPLAY", os.environ.get("DISPLAY", ":0"))
    if requested and _display_usable(requested) and not headless_requested:
        return requested, True
    return None, False


STEALTH_FLAGS: list[str] = [
    "--remote-debugging-address=127.0.0.1",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--window-size=1280,900",
]


def build_chrome_command(port: int, profile_dir: str | None = None) -> tuple[list[str], dict[str, str]] | None:
    """(argv, extra_env) to launch Chrome with CDP on `port`, or None if no Chrome."""
    chrome = find_chrome()
    if chrome is None:
        return None
    display, headed = resolve_display()
    flags = [
        f"--remote-debugging-port={port}",
        *(["--user-data-dir=" + profile_dir] if profile_dir else []),
        *STEALTH_FLAGS,
    ]
    if not headed:
        flags.append("--headless=new")
    argv = [chrome, *flags]
    env_display = display or ""
    return argv, {"DISPLAY": env_display} if (env_display and not IS_MACOS) else {}


# keep signature honest: callers unpack (argv, env)
def chrome_launch_plan(port: int, profile_dir: str | None = None):
    return build_chrome_command(port, profile_dir)
