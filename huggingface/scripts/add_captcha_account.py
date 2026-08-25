#!/usr/bin/env python3
"""
Create and save reusable Camoufox (Firefox) profiles that stay logged into hCaptcha.

Works for anyone, not just noVNC-local users: this script boots a temporary
noVNC session (if one isn't already running) so the login can be completed
from any browser, anywhere, via a URL + password.

Saves each profile cleanly to:
    ~/atlas/data/firefox_profiles/{email_slug}/

Usage:
    python add_captcha_account.py [--port 6080]
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from config import (
    is_py_module_available,
    install_py_module,
    is_binary_available,
    is_camoufox_browser_installed,
    ensure_camoufox_browsers,
    ensure_system_deps,
    install_system_deps,
)

PROFILES_ROOT = Path.home() / "atlas" / "data" / "firefox_profiles"

VALID_DOMAINS = {
    "google": "gmail.com",
    "hotmail": "hotmail.com",
    "outlook": "outlook.com",
}

HCAPTCHA_LOGIN_URL = "https://dashboard.hcaptcha.com/login"
HCAPTCHA_SIGNUP_URL = "https://dashboard.hcaptcha.com/signup?type=accessibility"

PAGE_LOAD_TIMEOUT = 60_000
ELEMENT_TIMEOUT = 15_000

VNC_DISPLAY = ":1"
VNC_RFB_PORT = 5900
DEFAULT_NOVNC_PORT = 6080


class Colors:
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @staticmethod
    def _wrap(code: str, text: str) -> str:
        if not sys.stdout.isatty():
            return text
        return f"{code}{text}{Colors.RESET}"


def green(t: str) -> str: return Colors._wrap(Colors.GREEN, t)
def cyan(t: str) -> str: return Colors._wrap(Colors.CYAN, t)
def yellow(t: str) -> str: return Colors._wrap(Colors.YELLOW, t)
def red(t: str) -> str: return Colors._wrap(Colors.RED, t)
def bold(t: str) -> str: return Colors._wrap(Colors.BOLD, t)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Shared dependency self-installing preamble
# ---------------------------------------------------------------------------

def _ensure_self_contained() -> None:
    """Install missing Python deps, system tools, and required directories.

    All checks are performed system-wide — nothing is assumed to be local
    or repo-relative.  Each check delegates to config.py's shared detection
    functions so logic stays consistent across get_cookie.py,
    add_captcha_account.py, and main.py.
    """
    PROFILES_ROOT.mkdir(parents=True, exist_ok=True)

    # --- Python packages (checked via importlib, not repo-relative) ---
    deps = {"camoufox": "camoufox", "playwright": "playwright"}
    for module_name, pip_name in deps.items():
        if not is_py_module_available(module_name):
            print(f"  [setup] Installing {pip_name} ...", file=sys.stderr)
            install_py_module(module_name, pip_name)

    # --- Camoufox browser binaries (checked in ~/.cache/camoufox/) ---
    if not is_camoufox_browser_installed():
        print("  [setup] Downloading camoufox browsers ...", file=sys.stderr)
        ensure_camoufox_browsers()

    # --- System binaries: Xvfb, x11vnc, websockify, novnc (shutil.which) ---
    missing = ensure_system_deps()
    if missing and is_binary_available("apt-get"):
        print(f"  [setup] Installing {', '.join(missing)} ...", file=sys.stderr)
        install_system_deps(missing)

    print("  [setup] All dependencies ready.", file=sys.stderr)


_ensure_self_contained()

from camoufox.sync_api import Camoufox
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# Display / noVNC bootstrap
# ---------------------------------------------------------------------------

def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            return s.connect_ex((host, port)) == 0
        except Exception:
            return False


def _x_display_exists(display: str) -> bool:
    display_num = display.lstrip(":").split(".")[0]
    return Path(f"/tmp/.X11-unix/X{display_num}").exists()


def _x11vnc_running_on(display: str) -> bool:
    try:
        out = subprocess.run(["pgrep", "-af", "x11vnc"], capture_output=True, text=True, timeout=5)
        return display in out.stdout
    except Exception:
        return False


def _find_novnc_web_dir() -> Optional[str]:
    """Find the noVNC web directory using the shared system-dep check."""
    for d in ("/usr/share/novnc", "/usr/share/webapps/novnc", "/opt/novnc"):
        if Path(d).exists():
            return d
    return None


def get_public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me"):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    return ip
        except Exception:
            continue
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        ip = out.stdout.strip().split()[0]
        if ip:
            return ip
    except Exception:
        pass
    return "YOUR_SERVER_IP"


@dataclass
class DisplayStack:
    display: str
    port: int
    reused: bool
    procs: list
    vnc_password: str
    vnc_password_file: Optional[Path] = None


def _write_vnc_password(password: str) -> Path:
    """Write the VNC password to a private temp file (mode 0600)."""
    fd, path = tempfile.mkstemp(prefix="atlas_vnc_pass_", suffix=".txt")
    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    os.write(fd, password.encode())
    os.close(fd)
    return Path(path)


def ensure_display_stack(port: int) -> DisplayStack:
    """
    Reuse an existing display/VNC/noVNC stack if one is already running.
    Otherwise start a temporary Xvfb + x11vnc + noVNC stack.
    """
    display = os.environ.get("DISPLAY") or VNC_DISPLAY
    procs: list = []

    display_already_up = _x_display_exists(display)
    vnc_already_up = _x11vnc_running_on(display)
    novnc_already_up = _is_port_listening(port)

    vnc_password = secrets.token_urlsafe(32)
    vnc_password_file: Optional[Path] = None

    if display_already_up and vnc_already_up and novnc_already_up:
        print(green(f"  ✓ Reusing existing display {display} + noVNC on port {port}"))
        return DisplayStack(
            display=display, port=port, reused=True, procs=[],
            vnc_password=vnc_password, vnc_password_file=vnc_password_file,
        )

    if not display_already_up:
        print(yellow(f"  Starting Xvfb on {display} ..."))
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        time.sleep(1.5)
    os.environ["DISPLAY"] = display

    if not vnc_already_up:
        print(yellow(f"  Starting x11vnc on {display} (port {VNC_RFB_PORT}) ..."))
        proc = subprocess.Popen(
            [
                "x11vnc", "-display", display, "-forever", "-shared", "-quiet",
                "-rfbport", str(VNC_RFB_PORT), "-passwd", vnc_password,
                "-noxdamage",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        time.sleep(1.5)

    # Write the password to a private file so it's never printed to stdout/logs.
    vnc_password_file = _write_vnc_password(vnc_password)

    if not novnc_already_up:
        web_dir = _find_novnc_web_dir()
        print(yellow(f"  Starting noVNC on port {port} ..."))
        cmd = ["websockify"]
        if web_dir:
            cmd += ["--web", web_dir]
        cmd += [str(port), f"localhost:{VNC_RFB_PORT}"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
        time.sleep(1.5)

    return DisplayStack(
        display=display, port=port, reused=False, procs=procs,
        vnc_password=vnc_password, vnc_password_file=vnc_password_file,
    )


def teardown_display_stack(stack: DisplayStack) -> None:
    if stack.reused:
        return
    for proc in stack.procs:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    # Clean up the private password file if we created one.
    if stack.vnc_password_file and stack.vnc_password_file.exists():
        try:
            stack.vnc_password_file.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Data / validation
# ---------------------------------------------------------------------------

def sanitize_email_for_path(email: str) -> str:
    return re.sub(r"[^\w\-.]", "_", email.strip().lower())


@dataclass
class AccountSpec:
    email: str
    domain_key: str  # "google", "hotmail", or "outlook"
    has_account: bool

    @property
    def profile_dir(self) -> Path:
        return PROFILES_ROOT / sanitize_email_for_path(self.email)

    @property
    def oauth_provider(self) -> str:
        return "google" if self.domain_key == "google" else "microsoft"

    @property
    def target_url(self) -> str:
        return HCAPTCHA_LOGIN_URL if self.has_account else HCAPTCHA_SIGNUP_URL


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


def validate_email(raw: str) -> Optional[tuple]:
    raw = raw.strip().lower()
    if not EMAIL_RE.match(raw):
        return None
    _, domain = raw.rsplit("@", 1)
    for key, suffix in VALID_DOMAINS.items():
        if domain == suffix:
            return raw, key
    return None


def prompt_for_email() -> tuple:
    print(cyan("  Enter the email address you use for your hCaptcha account:"))
    print(cyan("  (only gmail.com, hotmail.com, and outlook.com are supported)"))
    print()

    while True:
        try:
            raw = input("  email> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(red("\n  Aborted."))
            sys.exit(1)

        if not raw:
            print(yellow("  Please enter an email address."))
            continue

        result = validate_email(raw)
        if result is None:
            print(red("  Only @gmail.com, @hotmail.com, and @outlook.com are accepted."))
            continue

        print(green(f"  ✓ Accepted: {result[0]} ({result[1]})"))
        return result


def prompt_has_account() -> bool:
    print()
    print(cyan("  Do you already have an hCaptcha accessibility account?"))
    while True:
        try:
            raw = input("  (y/n)> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(red("\n  Aborted."))
            sys.exit(1)
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print(yellow("  Please answer y or n."))


# ---------------------------------------------------------------------------
# Browser flow
# ---------------------------------------------------------------------------

def click_provider_button(page, provider: str) -> bool:
    texts = (
        ["Sign in with Google", "Continue with Google", "Google"]
        if provider == "google"
        else ["Sign in with Microsoft", "Continue with Microsoft", "Microsoft"]
    )
    for text in texts:
        try:
            page.get_by_text(text, exact=False).first.click(timeout=8000)
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def fill_email(page, email: str) -> bool:
    selectors = [
        'input[type="email"]',
        'input[name="identifier"]',
        'input[name="login"]',
        'input[autocomplete="username"]',
        'input[type="text"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
            loc.fill(email)
            return True
        except Exception:
            continue
    return False


def print_access_instructions(stack: DisplayStack) -> None:
    ip = get_public_ip()
    url = f"http://{ip}:{stack.port}/vnc.html?autoconnect=true&resize=remote"
    password = stack.vnc_password or ""

    print()
    print(bold("  ┌──────────────────────────────────────────────────────────────────┐"))
    print(bold("  │  The browser is ready. Complete the login on the page below.      │"))
    print(bold("  └──────────────────────────────────────────────────────────────────┘"))
    print()
    print(green(f"  URL       : {url}"))
    # Do not print the password to stdout/logs — it's in a private file.
    if stack.vnc_password_file:
        print(green(f"  Password file : {stack.vnc_password_file}"))
    print()
    print(yellow("  Open that URL in a normal browser (Chrome / Safari / Firefox)."))
    print(yellow(f"  If it doesn't load, make sure port {stack.port} is open/forwarded"))
    print(yellow("  from this machine, or access it locally if you're on this server."))
    print()
    print(cyan("  Ensure you sign up through the accessibility signup flow to get"))
    print(cyan("  access to the accessibility cookie. Only Google, Outlook, and"))
    print(cyan("  Hotmail accounts are supported."))
    print()
    print(bold("  When you are fully logged into hCaptcha, come back here and"))
    print(bold("  press Enter."))
    print()


def run_authentication_flow(spec: AccountSpec, stack: DisplayStack) -> Path:
    os.environ["DISPLAY"] = stack.display

    temp_root = Path(tempfile.mkdtemp(prefix="camoufox_profile_"))
    print(cyan(f"  ● Temp profile: {temp_root}"))

    print(yellow(f"  Launching Camoufox on {stack.display} ..."))
    with Camoufox(
        persistent_context=True,
        user_data_dir=str(temp_root),
        headless=False,
    ) as context:

        page = context.pages[0] if context.pages else context.new_page()

        print(yellow(f"  Navigating to {spec.target_url}"))
        page.goto(spec.target_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        print(green("  ✓ Page loaded"))

        print(yellow(f"  Clicking {spec.oauth_provider.title()} button ..."))
        if not click_provider_button(page, spec.oauth_provider):
            print(yellow("  Could not auto-click — you'll click it manually via the VNC link."))
        else:
            print(green("  ✓ Provider button clicked"))
            time.sleep(2)
            print(yellow("  Filling email ..."))
            if fill_email(page, spec.email):
                print(green(f"  ✓ Email filled: {spec.email}"))
            else:
                print(yellow("  Could not auto-fill email — you'll type it manually via the VNC link."))

        # Only now, once everything is set up, reveal the access URL + password.
        print_access_instructions(stack)

        try:
            input("  Press Enter once login is complete → ")
        except (EOFError, KeyboardInterrupt):
            print(red("\n  Aborted."))
            shutil.rmtree(temp_root, ignore_errors=True)
            sys.exit(1)

        print(yellow("  Closing browser and flushing profile ..."))

    time.sleep(1.5)
    return temp_root


# ---------------------------------------------------------------------------
# Save profile
# ---------------------------------------------------------------------------

def copy_profile_clean(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)

    def ignore_transient(directory, files):
        ignore = {"lock", "parent.lock", ".parentlock", "Session Storage"}
        return [f for f in files if f in ignore]

    shutil.copytree(src, dst, ignore=ignore_transient)


def save_profile(spec: AccountSpec, source_dir: Path) -> Path:
    dest = spec.profile_dir
    print(cyan(f"  Saving profile to: {dest}"))

    dest.parent.mkdir(parents=True, exist_ok=True)
    copy_profile_clean(source_dir, dest)

    meta = dest / ".atlas_profile_meta.json"
    now = datetime.now(timezone.utc).isoformat()
    meta.write_text(
        "{\n"
        f'  "email": "{spec.email}",\n'
        f'  "provider": "{spec.domain_key}",\n'
        f'  "created_at": "{now}",\n'
        f'  "engine": "camoufox",\n'
        f'  "source": "add_captcha_account.py"\n'
        "}\n"
    )
    return dest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a Camoufox hCaptcha profile.")
    parser.add_argument("--port", type=int, default=DEFAULT_NOVNC_PORT,
                         help=f"noVNC port to expose (default: {DEFAULT_NOVNC_PORT})")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print()
    print(bold("  ════════════════════════════════════════════════════"))
    print(bold("   Atlas — Camoufox hCaptcha Profile Creator"))
    print(bold("  ════════════════════════════════════════════════════"))
    print()

    email, domain_key = prompt_for_email()
    has_account = prompt_has_account()
    spec = AccountSpec(email=email, domain_key=domain_key, has_account=has_account)

    print()
    print(yellow(f"  Provider : {spec.domain_key.title()}"))
    print(yellow(f"  Email    : {spec.email}"))
    print(yellow(f"  Flow     : {'Login' if has_account else 'Signup (accessibility)'}"))
    print(yellow(f"  Target   : {spec.profile_dir}"))
    print()

    print(yellow("  Setting up display / noVNC ..."))
    stack = ensure_display_stack(args.port)

    try:
        temp_path = run_authentication_flow(spec, stack)
    except Exception as e:
        print(red(f"  ERROR: {e}"))
        import traceback
        traceback.print_exc()
        teardown_display_stack(stack)
        return 1

    try:
        saved = save_profile(spec, temp_path)
    except Exception as e:
        print(red(f"  ERROR saving profile: {e}"))
        teardown_display_stack(stack)
        return 2
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)

    teardown_display_stack(stack)

    print()
    print(green("  ┌──────────────────────────────────────────────────────────┐"))
    print(green("  │  Camoufox profile saved successfully.                  │"))
    print(green(f"  │  Location: {str(saved)[:46]:<46s} │"))
    print(green("  │  Ready for reuse with Camoufox.                        │"))
    print(green("  └──────────────────────────────────────────────────────────┘"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
