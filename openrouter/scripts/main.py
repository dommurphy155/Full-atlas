#!/usr/bin/env python3
"""
Main orchestrator for OpenRouter signup automation.

Flow:
  1. Bootstrap: detect distro, install deps, prompt for API key (first run only)
  2. Launch CDP via launch_cdp.sh (random port, Xvfb)
  3. Burn + create fresh OpenMail email
  4. Generate random password
  5. Call signup_automation.py with email + password
  6. Key is saved to data/openroute_keys.txt by signup_automation.py
  7. Burn the email inbox
  8. Exit cleanly

Usage:
    python3 main.py
"""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import random
import string
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from config import (
    PROJECT_ROOT, DATA_DIR, RUN_DIR, KEYS_FILE, ENV_FILE, LOG_DIR,
    CDP_PORT_FILE, CDP_PID_FILE, CDP_HOST, CDP_TIMEOUT,
    OPEN_EMAIL_SCRIPT, SIGNUP_SCRIPT, LAUNCH_CDP_SCRIPT,
    OPENMAIL_API_KEY, ensure_dirs, find_chrome,
)


def get_logger():
    """Get logger with file + stdout handlers. Idempotent."""
    _logger = logging.getLogger("main")
    if _logger.handlers:
        return _logger
    ensure_dirs()
    _logger.setLevel(logging.INFO)
    _fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    _fh = RotatingFileHandler(str(LOG_DIR / "orchestrator.log"), maxBytes=5*1024*1024, backupCount=3)
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    _logger.addHandler(_fh)
    _logger.addHandler(_sh)
    _logger.propagate = False
    return _logger


logger = get_logger()


def log(msg: str):
    logger.info(msg)


def detect_distro():
    """Detect Linux distribution and package manager."""
    try:
        os_release = Path("/etc/os-release").read_text()
        info = {}
        for line in os_release.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip().lower()] = v.strip().strip("\"'")
        distro_id = info.get("id", "unknown")
        distro_name = info.get("name", "Unknown")
        return distro_id, distro_name
    except Exception:
        return "unknown", "Unknown"


def is_debian_like(distro_id):
    return distro_id in ("debian", "ubuntu", "mint", "pop")


def _chrome_present() -> bool:
    """Check if Chrome/Chromium is already installed via any known binary path."""
    return find_chrome() is not None


def _add_google_chrome_repo() -> bool:
    """Add Google's apt repo so google-chrome-stable is installable.
    It is NOT in default Ubuntu/Debian repos."""
    try:
        subprocess.run(
            ["bash", "-c",
             "curl -fsSL https://dl.google.com/linux/linux_signing_key.pub "
             "| gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg 2>/dev/null"],
            capture_output=True, timeout=30,
        )
        subprocess.run(
            ["bash", "-c",
             'echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] '
             'http://dl.google.com/linux/chrome/deb/ stable main" '
             "> /etc/apt/sources.list.d/google-chrome.list"],
            capture_output=True, timeout=15,
        )
        result = subprocess.run(["apt-get", "update", "-qq"], capture_output=True, timeout=120)
        return result.returncode == 0
    except Exception as e:
        log(f"Could not add Google Chrome repo: {e}")
        return False


def install_system_deps(distro_id, distro_name):
    """Install required system packages via apt. Never raises — always
    returns True/False and lets the caller decide whether to proceed
    with degraded capability instead of crashing the whole run."""
    if not is_debian_like(distro_id):
        log(f"Distro '{distro_name}' is not Debian-like. Please install manually:")
        log("  apt-get install -y xvfb google-chrome-stable curl fuser")
        return _chrome_present()  # ok to proceed if Chrome/Chromium already present

    log(f"Detected {distro_name}. Installing system deps via apt...")
    pkgs = ["xvfb", "curl", "psmisc"]  # psmisc provides fuser

    cmd = ["apt-get", "update", "-qq"]
    subprocess.run(cmd, capture_output=True, timeout=120)

    cmd = ["apt-get", "install", "-y", "-qq"] + pkgs
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        log(f"WARNING: base apt-get install failed: {result.stderr.decode()[:300]}")
        # Non-fatal — keep going, Chrome check below is what actually matters

    if _chrome_present():
        log("Chrome/Chromium already installed.")
    else:
        log("Chrome not found — attempting install via google-chrome-stable...")
        result = subprocess.run(
            ["apt-get", "install", "-y", "-qq", "google-chrome-stable"],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            log("google-chrome-stable not found in default repos — adding Google's apt repo...")
            if _add_google_chrome_repo():
                result = subprocess.run(
                    ["apt-get", "install", "-y", "-qq", "google-chrome-stable"],
                    capture_output=True, timeout=300,
                )
            if result.returncode != 0:
                log("google-chrome-stable install failed — trying chromium as fallback...")
                subprocess.run(
                    ["apt-get", "install", "-y", "-qq", "chromium-browser"],
                    capture_output=True, timeout=300,
                )

    if not _chrome_present():
        log("WARNING: No Chrome/Chromium binary found after install attempts. "
            "Set CHROME_BIN env var to a valid browser path, or install manually:")
        log("  sudo apt-get install -y google-chrome-stable")
        log("Continuing anyway — CDP launch will fail loudly later if still missing.")

    log("System dependency setup complete.")
    return True  # Never block bootstrap on this — let CDP launch surface the real error if any


def install_py_deps():
    """Install required Python packages via pip or uv."""
    import importlib

    deps = {
        "httpx": "httpx",
        "dotenv": "python-dotenv",
        "patchright": "patchright",
    }
    missing = []
    for mod, pkg in deps.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)

    if not missing:
        log("All Python dependencies already installed.")
        return True

    log(f"Installing Python deps: {', '.join(missing)}")

    # Try pip first, then uv as fallback
    installed = False
    try:
        # Try with --break-system-packages for PEP 668 environments
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--break-system-packages", "--quiet"] + missing,
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            # Retry without the flag (in case it's not supported)
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
                capture_output=True, timeout=300,
            )
        if result.returncode == 0:
            installed = True
    except Exception:
        pass

    if not installed:
        # Fall back to uv targeting the current venv
        result = subprocess.run(
            ["uv", "pip", "install", "--python", sys.executable, "--quiet"] + missing,
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            log(f"pip/uv install failed: {result.stderr.decode()[:500]}")
            return False

    # Install chromium for patchright if not already present
    try:
        importlib.import_module("patchright")
        result = subprocess.run(
            [sys.executable, "-m", "patchright", "install", "chromium"],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            # Try via uv if patchright is in a different location
            pass
    except ImportError:
        pass

    log("Python dependencies installed.")
    return True


def is_first_run():
    """Check if .env config exists with required keys."""
    if not ENV_FILE.exists():
        return True
    content = ENV_FILE.read_text()
    return "OPENMAIL_API_KEY=" not in content or "OPENMAIL_API_KEY=\"\"" in content or "OPENMAIL_API_KEY=''" in content


def bootstrap():
    """First-run bootstrap: install deps, prompt for API key."""
    if not is_first_run():
        log("Setup already complete — skipping bootstrap.")
        return

    log("=" * 60)
    log("First-Run Setup")
    log("=" * 60)

    # 1. Detect distro
    distro_id, distro_name = detect_distro()
    log(f"Detected: {distro_name} ({distro_id})")

    # 2. Install system deps — warn but never abort the whole run over this
    if not install_system_deps(distro_id, distro_name):
        log("WARNING: system dependency installation had issues — continuing anyway.")

    # 3. Install Python deps
    if not install_py_deps():
        raise RuntimeError(
            "Required Python dependencies could not be installed."
        )

    # 4. Check Chrome
    chrome = find_chrome()
    if not chrome:
        log("WARNING: Chrome/Chromium not found. Install google-chrome or set CHROME_BIN env var.")
    else:
        log(f"Chrome found: {chrome}")

    # 5. Prompt for API keys
    log("\nRequired API keys:")
    log("  1. OPENMAIL_API_KEY — get from https://openmail.sh")
    log("  2. OPENROUTER_API_KEY (optional, for verification)")

    openmail_key = None
    try:
        openmail_key = input("  OPENMAIL_API_KEY: ").strip()
        while not openmail_key:
            log("  OPENMAIL_API_KEY is required.")
            openmail_key = input("  OPENMAIL_API_KEY: ").strip()
    except EOFError:
        pass

    if not openmail_key:
        # Fallback: try from environment
        openmail_key = os.environ.get("OPENMAIL_API_KEY", "")
        if openmail_key:
            log(f"Using OPENMAIL_API_KEY from environment")
        else:
            log("ERROR: OPENMAIL_API_KEY is required. Set it in .env or as env var.")
            sys.exit(1)

    optional_key = ""
    try:
        optional_key = input("  OPENROUTER_API_KEY (optional, press Enter to skip): ").strip()
    except EOFError:
        # Use env var if available
        optional_key = os.environ.get("OPENROUTER_API_KEY", "")

    ensure_dirs()
    config_content = f'OPENMAIL_API_KEY="{openmail_key}"\n'
    if optional_key:
        config_content += f'OPENROUTER_API_KEY="{optional_key}"\n'

    ENV_FILE.write_text(config_content)
    os.chmod(ENV_FILE, 0o600)
    log(f"Config written to {ENV_FILE}")
    log("=" * 60)


def find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def wait_for_cdp(port: int, timeout: int = CDP_TIMEOUT) -> bool:
    import urllib.request
    cdp_url = f"http://{CDP_HOST}:{port}/json/version"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(cdp_url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def launch_cdp(port: int):
    """Launch CDP browser via launch_cdp.sh. Returns Popen handle."""
    log(f"Launching CDP on port {port}...")

    env = {
        **os.environ,
        "CDP_PORT": str(port),
        "CDP_PORT_FILE": str(CDP_PORT_FILE),
    }

    # Do not force DISPLAY. launch_cdp.sh decides whether an existing
    # X display is usable and otherwise falls back to headless Chrome.
    proc = subprocess.Popen(
        ["bash", str(LAUNCH_CDP_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        cwd=str(PROJECT_ROOT),
    )

    # Stream launch_cdp.sh output for visibility
    def stream_output():
        if proc.stdout:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    print(f"  [CDP] {line}", flush=True)

    import threading
    t = threading.Thread(target=stream_output, daemon=True)
    t.start()

    if not wait_for_cdp(port, timeout=CDP_TIMEOUT):
        log("CDP failed to start!")
        proc.kill()
        raise RuntimeError("CDP did not become ready")

    log(f"CDP ready on port {port}")
    return proc


def create_openmail_email() -> str | None:
    load_env()
    log("Burning all existing OpenMail inboxes...")
    # Burn uses the first working key; it will exit(1) if all keys exhausted
    burn_result = subprocess.run(
        [sys.executable, str(OPEN_EMAIL_SCRIPT), "burn"],
        capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT),
    )
    if burn_result.returncode != 0:
        # Log burn output but continue — create_inbox will try all keys anyway
        log(f"Burn warning (continuing): {burn_result.stderr.strip()}")

    log("Creating fresh OpenMail inbox...")
    # Creates inbox using whichever API key has quota (tries keys 1..5)
    result = subprocess.run(
        [sys.executable, str(OPEN_EMAIL_SCRIPT), "create"],
        capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        # Combine stderr and stdout for full error context
        error_msg = result.stderr.strip() or result.stdout.strip()
        log(f"Failed to create email: {error_msg}")
        return None
    email = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not email:
        log("Empty email address returned")
        return None
    log(f"Email created")  # already short, keep as-is
    return email


def generate_password() -> str:
    chars = string.ascii_letters + string.digits
    password = "".join(random.choice(chars) for _ in range(16))
    password += "!A1"
    return password


def run_signup(email: str, password: str) -> bool:
    log("Starting signup automation...")
    proc = subprocess.Popen(
        [sys.executable, str(SIGNUP_SCRIPT), email],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        cwd=str(PROJECT_ROOT),
    )
    proc.stdin.write(password + "\n")
    proc.stdin.close()

    if proc.stdout:
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip()
            if line:
                print(f"  {line}", flush=True)

    try:
        proc.wait(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        log("Signup automation timed out (600s)")
        return False

    if proc.returncode == 0:
        log("Signup automation completed successfully")
        return True
    else:
        log(f"Signup automation failed (exit code {proc.returncode})")
        return False


def cleanup_cdp(port: int):
    """Clean up only the Chrome process owned by this project."""
    if CDP_PID_FILE.exists():
        try:
            raw = CDP_PID_FILE.read_text().strip()
            CDP_PID_FILE.unlink(missing_ok=True)

            for value in raw.split():
                try:
                    pid = int(value)
                    os.kill(pid, 15)
                except (ProcessLookupError, ValueError, PermissionError):
                    pass
        except Exception as e:
            logger.warning(f"CDP PID cleanup failed: {e}")

    # Fallback for stale PID files.
    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        pass


def load_env():
    """Load .env file if it exists."""
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def main():
    # Set up CDP_PORT for launch_cdp.sh and signup_automation.py
    port = find_free_port()
    os.environ["CDP_PORT"] = str(port)
    os.environ["CDP_PORT_FILE"] = str(CDP_PORT_FILE)

    # First-run bootstrap
    bootstrap()

    # Reload env after .env is written
    load_env()

    # Ensure dirs exist
    ensure_dirs()

    # Purge stale debug artifacts (screenshots/HTML dumps) from prior
    # runs so failure debugging never leaks disk space over time.
    for pattern in ["debug_*.png", "debug_*.html", "signup_form_missing.png"]:
        for stale in DATA_DIR.glob(pattern):
            try:
                stale.unlink()
            except Exception:
                pass

    log("=" * 60)
    log("OpenRouter Signup Automation")
    log("=" * 60)

    # Retry logic: up to 3 attempts
    for attempt in range(1, 4):
        log(f"\n{'='*60}")
        log(f"Attempt {attempt}/3")
        log(f"{'='*60}")

        port = find_free_port()
        os.environ["CDP_PORT"] = str(port)
        os.environ["CDP_PORT_FILE"] = str(CDP_PORT_FILE)

        cdp_proc = None
        try:
            cdp_proc = launch_cdp(port)

            email = create_openmail_email()
            if not email:
                log("Could not create email, retrying...")
                continue

            password = generate_password()
            log("Generated password (not shown)")

            success = run_signup(email, password)

            if success:
                if KEYS_FILE.exists():
                    keys = [k for k in KEYS_FILE.read_text().strip().splitlines() if k.strip()]
                    log(f"\n=== SUCCESS ===")
                    log(f"Total keys now: {len(keys)}")
                    log(f"Saved to: {KEYS_FILE}")
                    log("=" * 60)
                    sys.exit(0)
                else:
                    log("Key file not found after successful signup — unexpected")
            else:
                log("Signup failed, will retry...")

        except Exception as e:
            log(f"ERROR: {e}")

        finally:
            if cdp_proc:
                try:
                    cdp_proc.terminate()  # SIGTERM lets launch_cdp.sh's trap clean its own profile dir
                    cdp_proc.wait(timeout=8)
                except Exception:
                    try:
                        cdp_proc.kill()
                        cdp_proc.wait(timeout=5)
                    except Exception:
                        pass
            cleanup_cdp(port)
            # Force-delete ANY leftover chrome-profile dirs regardless of
            # whether the trap fired — never let a profile survive a run.
            import shutil as _shutil
            for stale_profile in RUN_DIR.glob("chrome-profile.*"):
                try:
                    _shutil.rmtree(stale_profile, ignore_errors=True)
                except Exception:
                    pass

        if attempt < 3:
            log("Waiting 10s before retry...")
            time.sleep(10)

    log("\n=== FAILED: all 3 attempts exhausted ===")
    sys.exit(1)


if __name__ == "__main__":
    main()
