"""
Centralized config for OpenRouter signup automation.
All paths resolved relative to this file's directory.
Stateless: no hardcoded usernames, /root, or absolute project paths.
Linux-only.
"""
import os
from pathlib import Path

# --- Project paths (all derived from this file) ---
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR  # This IS the project root
DATA_DIR = PROJECT_ROOT / "data"
RUN_DIR = PROJECT_ROOT / "run"
STATE_FILE = DATA_DIR / "openmail_state.json"
KEYS_FILE = DATA_DIR / "openroute_keys.txt"
ENV_FILE = PROJECT_ROOT / ".env"
LOG_DIR = DATA_DIR / "logs"
CDP_PORT_FILE = RUN_DIR / "cdp_port.txt"
CDP_PID_FILE = RUN_DIR / "cdp_pids.txt"
STEALTH_DIR = PROJECT_ROOT / "stealth-extension"

# Script paths
OPEN_EMAIL_SCRIPT = SCRIPT_DIR / "open_email.py"
SIGNUP_SCRIPT = SCRIPT_DIR / "signup_automation.py"
LAUNCH_CDP_SCRIPT = SCRIPT_DIR / "launch_cdp.sh"

# CDP defaults
CDP_HOST = os.environ.get("CDP_HOST", "127.0.0.1")
CDP_DISPLAY = os.environ.get("CDP_DISPLAY", ":1")
CDP_HEADLESS = os.environ.get("CDP_HEADLESS", "").strip().lower() in {
    "1", "true", "yes", "on"
}
CDP_PORTS = [9333, 9444, 9555, 9666]
CDP_TIMEOUT = 60

# OpenMail defaults
OPENMAIL_BASE = os.environ.get("OPENMAIL_BASE", "https://api.openmail.sh")


def get_openmail_api_keys():
    """Collect all OpenMail API keys from environment.
    Checks keys in priority order:
      1. OPENMAIL_API_KEY (single key, for backward compatibility)
      2. OPENMAIL_API_KEY_1 through OPENMAIL_API_KEY_5
    Returns a list of non-empty keys.
    """
    keys = []
    single = os.environ.get("OPENMAIL_API_KEY", "").strip()
    if single:
        keys.append(single)
    for i in range(1, 6):
        k = os.environ.get(f"OPENMAIL_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    return keys


def get_openmail_api_key():
    """Return the first available OpenMail API key (for single-key usage)."""
    keys = get_openmail_api_keys()
    return keys[0] if keys else ""


OPENMAIL_API_KEY = get_openmail_api_key()

# OpenRouter
OPENROUTER_SIGNUP_URL = "https://openrouter.ai/sign-up"
OPENROUTER_BASE = "https://openrouter.ai"

# Timeouts (seconds)
EMAIL_POLL_TIMEOUT = 60


def ensure_dirs():
    """Create all required directories."""
    for d in [DATA_DIR, RUN_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def find_chrome():
    """Find Chrome binary: env var, common paths, then PATH."""
    custom = os.environ.get("CHROME_BIN")
    if custom and Path(custom).exists():
        return custom
    common = [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for p in common:
        if Path(p).exists():
            return p
    import shutil
    found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if found:
        return found
    return None


CHROME_BIN = None  # lazy — check at runtime, not import time
