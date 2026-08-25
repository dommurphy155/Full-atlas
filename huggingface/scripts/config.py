"""
Centralized config for huggingface project.
All paths resolved relative to this file's directory.
No hardcoded absolute paths.
"""
from pathlib import Path
import glob
import importlib
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import sys as _sys

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ROOT_DIR = PROJECT_ROOT / "huggingface"
DATA_DIR = PROJECT_ROOT / "data" / "huggingface_data"

load_dotenv(PROJECT_ROOT / ".env")

KEYS_FILE = DATA_DIR / "hf_keys.txt"
STATE_FILE = DATA_DIR / ".agentmail_state.json"
CAPTCHA_COOKIE_FILE = PROJECT_ROOT / "data" / "captcha_cookie.json"

AGENTMAIL_SCRIPT = ROOT_DIR / "scripts" / "agentmail.py"
GET_COOKIE_SCRIPT = ROOT_DIR / "scripts" / "get_cookie.py"
LAUNCH_CDP_SCRIPT = ROOT_DIR / "scripts" / "launch_cdp.sh"
HF_KEYS_SCRIPT = ROOT_DIR / "scripts" / "hf_keys.py"
ENV_FILE = PROJECT_ROOT / ".env"

CDP_HOST = "127.0.0.1"
CDP_PORT = 9333
CDP_PORT_FILE = Path("/tmp/cdp_port.txt")
CDP_PID_FILE = Path("/tmp/cdp_9333_pids.txt")
CDP_PROFILE_DIR_PREFIX = "/tmp/cdp_browser_profile_"

def cleanup_stale_cdp_profiles() -> int:
    removed = 0
    for pattern in (CDP_PROFILE_DIR_PREFIX,):
        for d in glob.glob(pattern + "*"):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    for f in (CDP_PORT_FILE, CDP_PID_FILE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    return removed

def _kill_chrome_on_cdp_port():
    try:
        subprocess.run(["fuser", "-k", f"{CDP_PORT}/tcp"],capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        subprocess.run(["pkill", "-f", f"remote-debugging-port={CDP_PORT}"],capture_output=True, timeout=5)
    except Exception:
        pass

AGENTMAIL_API_KEY = os.getenv("AGENTMAIL_API_KEY")
AGENTMAIL_BASE = "https://api.agentmail.to/v0"

HF_JOIN_URL = "https://huggingface.co/join"
HF_BASE = "https://huggingface.co"
HF_TOKEN_PREFIX = "hf_"
HF_CONFIRMATION_URL_PREFIX = "https://huggingface.co/email_confirmation/"

CDP_TIMEOUT = 60
SIGNUP_TIMEOUT = 60000
EMAIL_POLL_TIMEOUT = 180
EMAIL_POLL_INTERVAL = 1
PAGE_LOAD_TIMEOUT = 60000
ELEMENT_TIMEOUT = 15000
CAPTCHA_WAIT = 5

PASSWORD = "HuggingFace2024!SecurePass#7"

# --------------------------------------------------------------------------- #
# get_cookie.py / hf_keys.py configuration                                       #
# --------------------------------------------------------------------------- #

FIREFOX_PROFILES_DIR = PROJECT_ROOT / "data" / "firefox_profiles"
TARGET_COOKIE_NAME = "hc_accessibility"
TARGET_URL = "https://dashboard.hcaptcha.com/login"
SUCCESS_TEXT = "Set Cookie"
LIMIT_TEXT = "daily limit"
DISPLAY_NUM = ":1"
TIMEOUT_MS = 15000
PAGE_LOAD_TIMEOUT_MS = 60000


def setup_logging(name: str) -> logging.Logger:
    """Create a logger with file + stdout handlers, matching main.py pattern."""
    import logging as _logging
    logger = _logging.getLogger(name)
    logger.setLevel(_logging.INFO)
    fmt = _logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    fh = _logging.FileHandler(str(DATA_DIR / "orchestrator.log"))
    fh.setFormatter(fmt)
    sh = _logging.StreamHandler(_sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


def first_run_setup() -> None:
    """Ensure required directories exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FIREFOX_PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_dirs() -> None:
    """Backward-compat alias for hf_keys.py."""
    first_run_setup()


# --------------------------------------------------------------------------- #
# Bootstrap — run on first main.py invocation                                  #
# --------------------------------------------------------------------------- #

REQUIRED_DIRS = [
    DATA_DIR,
    FIREFOX_PROFILES_DIR,
    DATA_DIR.parent,  # ~/atlas/data
]

REQUIRED_PACKAGES = [
    "camoufox",
    "playwright",
    "httpx",
    "python-dotenv",
    "patchright",
]


# --------------------------------------------------------------------------- #
# Shared dependency-detection functions (used by get_cookie.py,             #
# add_captcha_account.py, main.py, and agentmail.py)                         #
# --------------------------------------------------------------------------- #

def is_py_module_available(module_name: str) -> bool:
    """Check whether a Python module is importable from the *active* Python
    environment — not just a specific venv or repo-relative path.

    Uses importlib.util.find_spec(), which respects the active sys.path,
    sys.prefix, and site-packages of the running interpreter.
    """
    # Map pip package names to their actual import module names
    _PIP_TO_IMPORT = {
        "python-dotenv": "dotenv",
    }
    import_name = _PIP_TO_IMPORT.get(module_name, module_name)
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def install_py_module(module_name: str, pip_name: str | None = None) -> bool:
    """Install *pip_name* (defaults to *module_name*) into the active
    environment only if is_py_module_available() returns False.
    Returns True if the module is available afterward.
    """
    if is_py_module_available(module_name):
        return True
    if pip_name is None:
        pip_name = module_name
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pip_name, "--break-system-packages"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return is_py_module_available(module_name)


def is_binary_available(name: str) -> bool:
    """Check whether a system binary is on $PATH via shutil.which()."""
    return shutil.which(name) is not None


def is_camoufox_browser_installed() -> bool:
    """Check whether Camoufox has actually downloaded browser binaries to
    its cache directory — not just whether the pip package imports.

    Camoufox stores its browsers under ~/.cache/camoufox/browsers/ and
    records the active version in ~/.cache/camoufox/config.json.
    """
    config_path = Path.home() / ".cache" / "camoufox" / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            active_version = cfg.get("active_version", "")
            if active_version:
                # active_version may or may not include "browsers/" prefix
                base = Path.home() / ".cache" / "camoufox"
                browser_path = base / active_version if (base / active_version).exists() else base / "browsers" / active_version
                if browser_path.exists() and any(browser_path.iterdir()):
                    return True
        except (json.JSONDecodeError, OSError, StopIteration):
            pass
    # Fallback: check if any browser dirs exist under the cache
    cache_root = Path.home() / ".cache" / "camoufox" / "browsers"
    if cache_root.exists():
        try:
            for entry in cache_root.iterdir():
                if entry.is_dir() and entry.name != ".links":
                    # Verify it has actual browser content
                    if any(entry.rglob("firefox")):
                        return True
        except OSError:
            pass
    return False


def is_playwright_browser_installed(browser_name: str = "chromium") -> bool:
    """Check whether a specific Playwright browser binary is installed in
    the Playwright browser cache (~/.cache/ms-playwright/).
    """
    cache_root = Path.home() / ".cache" / "ms-playwright"
    if not cache_root.exists():
        return False
    try:
        for entry in cache_root.iterdir():
            if entry.is_dir() and browser_name in entry.name.lower():
                return True
    except OSError:
        pass
    return False


def is_patchright_browser_installed() -> bool:
    """Check whether Patchright has downloaded its chromium browser binary."""
    return is_playwright_browser_installed("chromium")


def ensure_py_deps(packages: list[str] | None = None) -> None:
    """Ensure all listed Python packages are importable. Install any that
    are missing, but never reinstall something already present.

    *packages* defaults to REQUIRED_PACKAGES.
    """
    if packages is None:
        packages = REQUIRED_PACKAGES
    for pkg in packages:
        if not is_py_module_available(pkg):
            print(f"  [setup] Installing {pkg} ...", file=sys.stderr)
            install_py_module(pkg)


def ensure_camoufox_browsers() -> bool:
    """Ensure Camoufox browser binaries are installed (not just the pip
    package). Returns True if browser binaries are present or installed.
    """
    if is_camoufox_browser_installed():
        return True
    print("  [setup] Installing camoufox browsers ...", file=sys.stderr)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "camoufox", "--break-system-packages"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    # Try to trigger browser download
    try:
        from camoufox.sync_api import Camoufox  # noqa: F401
        # Instantiating once triggers the download if needed
    except Exception:
        pass
    return is_camoufox_browser_installed()


def ensure_playwright_browsers() -> bool:
    """Ensure Playwright browser binaries are installed."""
    if is_playwright_browser_installed():
        return True
    print("  [setup] Installing playwright browsers ...", file=sys.stderr)
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return is_playwright_browser_installed()


def ensure_system_deps() -> list[str]:
    """Check system binaries (Xvfb, x11vnc, websockify, novnc) and return
    the list of missing ones.  Each check uses shutil.which() for binaries
    and Path existence for novnc web dirs.
    """
    missing: list[str] = []
    if not is_binary_available("Xvfb"):
        missing.append("xvfb")
    if not is_binary_available("x11vnc"):
        missing.append("x11vnc")
    if not is_binary_available("websockify"):
        missing.append("websockify")
    _novnc_found = any(Path(d).exists() for d in (
        "/usr/share/novnc",
        "/usr/share/webapps/novnc",
        "/opt/novnc",
    ))
    if not _novnc_found:
        missing.append("novnc")
    return missing


def install_system_deps(missing: list[str]) -> None:
    """Install missing system deps via apt-get if available."""
    if not missing:
        return
    if not is_binary_available("apt-get"):
        print(f"  [setup] Missing system tools: {', '.join(missing)} — please install manually", file=sys.stderr)
        return
    print(f"  [setup] Installing {', '.join(missing)} ...", file=sys.stderr)
    subprocess.run(["apt-get", "update", "-qq"], capture_output=True, timeout=120)
    subprocess.run(["apt-get", "install", "-y", "-qq", *missing], capture_output=True, timeout=180)


def ensure_all_deps() -> None:
    """Top-level dependency ensure: Python packages, browsers, and system
    binaries.  Each category is checked globally and only installed if
    genuinely missing.  Safe to call multiple times.
    """
    # Python packages
    ensure_py_deps()

    # Camoufox browser binaries
    ensure_camoufox_browsers()

    # System binaries (Xvfb, x11vnc, websockify, novnc)
    missing_system = ensure_system_deps()
    if missing_system:
        install_system_deps(missing_system)

    print("  [setup] All dependencies ready.", file=sys.stderr)



def _detect_os() -> dict:
    """Detect OS, package manager, display, and chrome availability."""
    import platform
    info = {
        "os": platform.system(),
        "distro": "",
        "pkg_manager": "",
        "has_xvfb": is_binary_available("Xvfb"),
        "has_chrome": False,
        "chrome_path": "",
        "has_playwright_cli": is_binary_available("playwright"),
        "python_version": platform.python_version(),
    }
    if info["os"] == "Linux":
        # Package manager
        for pm in ("apt-get", "yum", "dnf", "pacman"):
            if is_binary_available(pm):
                info["pkg_manager"] = pm
                break

        # Distro
        try:
            info["distro"] = Path("/etc/os-release").read_text().split("ID=")[1].split()[0]
        except Exception:
            info["distro"] = "unknown"

        # Chrome detection
        for chrome in ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
            if Path(chrome).exists():
                info["has_chrome"] = True
                info["chrome_path"] = chrome
                break

    return info


def _ensure_directories() -> None:
    """Create all required directories."""
    for d in REQUIRED_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def _prompt(msg: str) -> str:
    """Read a line from stdin, handling EOF gracefully."""
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted by user.")
        sys.exit(1)


def _ensure_env_file_interactive() -> None:
    """Create .env if missing, then prompt for AGENTMAIL_API_KEY if absent."""
    if not ENV_FILE.exists():
        print(f"\n  [SETUP] {ENV_FILE} does not exist — creating template.")
        ENV_FILE.write_text(
            "# Atlas HuggingFace automation configuration\n"
            "AGENTMAIL_API_KEY=\n"
            "# Optional: override the Atlas proxy URL\n"
            "# ATLAS_PROXY_URL=http://127.0.0.1:8788\n"
        )
        print(f"  [SETUP] Template written to {ENV_FILE}")

    # Re-check for the key
    load_dotenv(ENV_FILE, override=True)
    if os.getenv("AGENTMAIL_API_KEY"):
        return

    print("\n  [SETUP] AGENTMAIL_API_KEY is not set in your .env file.")
    print("  [SETUP] Get your API key from: https://agentmail.to/")
    api_key = _prompt("  [SETUP] Paste your AGENTMAIL_API_KEY: ")
    if not api_key:
        print("  [SETUP] No key provided — cannot proceed.")
        sys.exit(1)

    # Write it into the .env file (append or replace)
    lines = ENV_FILE.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("AGENTMAIL_API_KEY="):
            lines[i] = f"AGENTMAIL_API_KEY={api_key}"
            found = True
            break
    if not found:
        lines.append(f"AGENTMAIL_API_KEY={api_key}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)
    print(f"  [SETUP] AGENTMAIL_API_KEY saved to {ENV_FILE}")


def _ensure_profiles_interactive() -> None:
    """If no Firefox profiles exist, run add_captcha_account.py interactively."""
    if not FIREFOX_PROFILES_DIR.exists() or not any(
        p.is_dir() for p in FIREFOX_PROFILES_DIR.iterdir()
    ):
        print("\n  [SETUP] No Firefox profiles found.")
        print("  [SETUP] You need at least one hCaptcha-verified profile to bypass")
        print("  [SETUP] hCaptcha challenges on HuggingFace signup.")
        print()
        print("  [SETUP] add_captcha_account.py will:")
        print("  [SETUP]   1. Install camoufox + playwright if missing")
        print("  [SETUP]   2. Launch a browser window on display :1")
        print("  [SETUP]   3. Prompt you for your hCaptcha dashboard email")
        print("  [SETUP]   4. You manually log in, click 'Set Cookie'")
        print("  [SETUP]   5. Profile is saved for future runs")
        print()
        _prompt("  [SETUP] Press Enter to launch the profile creator...")

        result = subprocess.run(
            [sys.executable, str(ROOT_DIR / "scripts" / "add_captcha_account.py")]
        )
        if result.returncode != 0:
            print("\n  [SETUP] Profile creation failed. Please retry manually:")
            print(f"  [SETUP]   python3 {ROOT_DIR / 'scripts' / 'add_captcha_account.py'}")
            sys.exit(1)

        print("  [SETUP] Profile created successfully.")
    else:
        profiles = [p for p in FIREFOX_PROFILES_DIR.iterdir() if p.is_dir()]
        print(f"  [SETUP] {len(profiles)} Firefox profile(s) found — skipping profile setup.")


def _ensure_system_deps(info: dict) -> None:
    """Install system-level deps if missing (Xvfb, Chrome)."""
    if info["os"] != "Linux":
        return

    if not info["has_xvfb"]:
        pm = info["pkg_manager"]
        if pm == "apt-get":
            print("  [SETUP] Installing Xvfb via apt-get...")
            subprocess.run(["apt-get", "update", "-qq"], capture_output=True, timeout=120)
            subprocess.run(
                ["apt-get", "install", "-y", "-qq", "xvfb"],
                capture_output=True, timeout=120,
            )
        elif pm:
            print(f"  [SETUP] Please install Xvfb via {pm}: sudo {pm} install xvfb")

    if not info["has_chrome"]:
        print("  [SETUP] Chrome/Chromium not found — install via your package manager")


def _ensure_python_deps() -> None:
    """Install required Python packages if missing (uses shared detection)."""
    for pkg in REQUIRED_PACKAGES:
        if not is_py_module_available(pkg):
            print(f"  [SETUP] Installing {pkg} ...")
            install_py_module(pkg)

    # Install camoufox browser binaries if the pip package is present but browsers aren't
    if is_py_module_available("camoufox") and not is_camoufox_browser_installed():
        print("  [SETUP] Downloading camoufox browsers ...")
        try:
            from camoufox.sync_api import Camoufox  # noqa: F401
        except Exception:
            pass


def bootstrap() -> None:
    """Interactive first-run bootstrap for the HuggingFace signup automation.

    Detects OS, creates dirs, prompts for missing API keys, installs system
    and Python deps, and walks the user through creating their first Firefox
    profile if none exist.  Nothing runs silently — every step is interactive.
    """
    print("=" * 60)
    print("  Atlas HF — Interactive Bootstrap")
    print("=" * 60)

    info = _detect_os()
    print(f"  OS         : {info['os']}")
    print(f"  Distro     : {info['distro']}" if info["distro"] else "  Distro     : N/A")
    print(f"  Python     : {info['python_version']}")
    print(f"  Chrome     : {'found' if info['has_chrome'] else 'MISSING — install chromium-browser'}")
    print(f"  Xvfb       : {'found' if info['has_xvfb'] else 'MISSING'}")
    print(f"  Pkg mgr    : {info['pkg_manager'] or 'N/A'}")
    print()

    # 1. Directories
    _ensure_directories()
    print("  ✓ Directories ready")

    # 2. API key
    _ensure_env_file_interactive()
    print("  ✓ AGENTMAIL_API_KEY set")

    # 3. System deps
    _ensure_system_deps(info)

    # 4. Python deps
    print("\n  Checking Python packages...")
    _ensure_python_deps()
    print("  ✓ Python dependencies ready")

    # 5. Firefox profiles
    _ensure_profiles_interactive()

    print()
    print("=" * 60)
    print("  Bootstrap complete. Ready for automation.")
    print("=" * 60)
