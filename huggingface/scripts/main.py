#!/usr/bin/env python3
"""
Main orchestrator for HuggingFace signup automation.

Flow:
  1. Launch Chrome via Patchright persistent context (reuses Chrome profiles)
  2. Inject hCaptcha cookie if valid cookie exists in captcha_cookie.json
     — if not, run get_cookie.py to obtain a fresh cookie first
  3. Burn all existing AgentMail inboxes and create a fresh email
  4. Navigate to https://huggingface.co/join
  5. Fill email → fill password → Next → username → fullname → TOS → Create Account
  6. Poll AgentMail for verification email (https://huggingface.co/email_confirmation/...)
     — if no email in 4s, retry the full flow once more
     — if still no email, run get_cookie.py for fresh cookie, try once more
     — if still fails, exit
  7. Navigate to verification link → wait for page load
  8. Navigate to token creation page → create token → extract hf_ key
  9. Save key to ~/atlas/data/huggingface_data/hf_keys.txt
  10. Exit cleanly

Usage:
    python3 main.py
"""
import asyncio
import atexit
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    ROOT_DIR, DATA_DIR, KEYS_FILE, AGENTMAIL_SCRIPT, GET_COOKIE_SCRIPT,
    HF_KEYS_SCRIPT,
    CAPTCHA_COOKIE_FILE, PASSWORD, HF_JOIN_URL, HF_TOKEN_PREFIX,
    HF_CONFIRMATION_URL_PREFIX, EMAIL_POLL_TIMEOUT, EMAIL_POLL_INTERVAL,
    cleanup_stale_cdp_profiles,
    bootstrap,
    is_py_module_available,
    install_py_module,
    is_patchright_browser_installed,
    ensure_playwright_browsers,
    ensure_system_deps,
    install_system_deps,
    is_binary_available,
)

# --- patchright / playwright dependency check (system-wide via importlib) ---
if not is_py_module_available("patchright"):
    print("  [SETUP] Installing patchright ...", file=sys.stderr)
    install_py_module("patchright")
if not is_patchright_browser_installed():
    print("  [SETUP] Downloading patchright/chromium browsers ...", file=sys.stderr)
    ensure_playwright_browsers()

from patchright.async_api import async_playwright  # noqa: F401

# Setup logging — timestamp-only format (HH:MM:SS)
import sys as _sys

_logger = logging.getLogger("main")
_logger.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
_fh = logging.FileHandler(str(DATA_DIR / "orchestrator.log"))
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(_sys.stdout)
_sh.setFormatter(_fmt)
_logger.addHandler(_fh)
_logger.addHandler(_sh)
_logger.propagate = False
logger = _logger


# --- Global cleanup state --- #
_active_pw: list = []

# Chrome profiles directory
CHROME_PROFILES_DIR = Path.home() / "atlas" / "data" / "chrome_profiles"
DEFAULT_PROFILE_DIR = CHROME_PROFILES_DIR / "huggingface_signup"

# Display configuration for Xvfb
VNC_DISPLAY = ":1"
VNC_RFB_PORT = 5900
DEFAULT_NOVNC_PORT = 6080

# WAF evasion headers removed — Patchright handles fingerprint evasion at engine level
# No need for injected JS navigator overrides either


# ---------------------------------------------------------------------------
# Display / noVNC bootstrap
# ---------------------------------------------------------------------------
import secrets as _secrets
import socket as _socket
import stat as _stat


def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
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


def _write_vnc_password(password: str) -> Path:
    """Write the VNC password to a private temp file (mode 0600)."""
    fd, path = tempfile.mkstemp(prefix="atlas_vnc_pass_", suffix=".txt")
    os.fchmod(fd, _stat.S_IRUSR | _stat.S_IWUSR)
    os.write(fd, password.encode())
    os.close(fd)
    return Path(path)


def _ensure_display_stack(port: int = DEFAULT_NOVNC_PORT) -> dict | None:
    """
    Reuse an existing display/VNC/noVNC stack if one is already running.
    Otherwise start a temporary Xvfb + x11vnc + noVNC stack.
    Returns display info or None if already running.
    """
    display = os.environ.get("DISPLAY") or VNC_DISPLAY
    os.environ["DISPLAY"] = display  # Always set it
    
    display_num = display.lstrip(":").split(".")[0]
    display_already_up = _x_display_exists(display)
    vnc_already_up = _x11vnc_running_on(display)
    novnc_already_up = _is_port_listening(port)

    if display_already_up and vnc_already_up and novnc_already_up:
        log(f"  ✓ Reusing existing display {display} + noVNC on port {port}")
        return None

    vnc_password = _secrets.token_urlsafe(32)

    if not display_already_up:
        log(f"  Starting Xvfb on {display} ...")
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _display_procs.append(proc)
        time.sleep(2)
        
        # Verify Xvfb actually started
        if not _x_display_exists(display):
            log(f"  ERROR: Xvfb failed to start on {display}")
            return None
        log(f"  ✓ Xvfb running on {display}")
    
    if not vnc_already_up:
        log(f"  Starting x11vnc on {display} ...")
        proc = subprocess.Popen(
            [
                "x11vnc", "-display", display, "-forever", "-shared", "-quiet",
                "-rfbport", str(VNC_RFB_PORT), "-passwd", vnc_password,
                "-noxdamage",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _display_procs.append(proc)
        time.sleep(1.5)

    vnc_password_file = _write_vnc_password(vnc_password)

    if not novnc_already_up:
        web_dir = _find_novnc_web_dir()
        log(f"  Starting noVNC on port {port} ...")
        cmd = ["websockify"]
        if web_dir:
            cmd += ["--web", web_dir]
        cmd += [str(port), f"localhost:{VNC_RFB_PORT}"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        _display_procs.append(proc)
        time.sleep(1.5)

    return {
        "display": display,
        "port": port,
        "vnc_password_file": vnc_password_file,
    }


# Track display processes for cleanup
_display_procs: list = []


def _teardown_display_stack() -> None:
    """Clean up display processes we started."""
    for proc in _display_procs:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _display_procs.clear()


def cleanup_all():
    """Kill all active Playwright instances, Patchright contexts, and temp profiles."""
    for pw in _active_pw:
        try:
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(pw.stop())
            else:
                _asyncio.run(pw.stop())
        except Exception:
            pass

    # Stop display stack if we started it
    _teardown_display_stack()

    # Clean temp profile dirs
    import glob
    for pattern in ("/tmp/patchright_tmp_*", "/tmp/hf_profile_*"):
        for d in glob.glob(pattern):
            try:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

    _active_pw.clear()


def log(msg: str):
    logger.info(msg)


atexit.register(cleanup_all)

# Ensure SIGINT also triggers clean exit
def _handle_sigint(signum, frame):
    log("Interrupted — exiting.")
    cleanup_all()
    sys.exit(130)

signal.signal(signal.SIGINT, _handle_sigint)


def _ensure_patchright() -> bool:
    """Ensure patchright is importable AND Chrome browser binaries are present
    (checked system-wide, not just local/venv)."""
    if not is_py_module_available("patchright"):
        print("  [SETUP] Installing patchright ...", file=sys.stderr)
        install_py_module("patchright")
    if is_py_module_available("patchright") and not is_patchright_browser_installed():
        print("  [SETUP] Downloading patchright/chromium browsers ...", file=sys.stderr)
        ensure_playwright_browsers()
    return is_py_module_available("patchright") and is_patchright_browser_installed()


def load_captcha_cookie() -> Optional[list]:
    if not CAPTCHA_COOKIE_FILE.exists():
        return None
    try:
        data = json.loads(CAPTCHA_COOKIE_FILE.read_text())
        # get_cookie.py writes a bare list; tolerate both formats
        if isinstance(data, dict):
            cookies = data.get("cookies", [])
        else:
            cookies = data

        # Validate that we have the correct hc_accessibility cookie, not a stale Cloudflare cookie
        if cookies:
            has_correct_cookie = any(
                c.get("name") == "hc_accessibility" for c in cookies
            )
            if not has_correct_cookie:
                log.warning(
                    "Cookie file does not contain 'hc_accessibility' — "
                    "re-running get_cookie.py to obtain fresh cookie"
                )
                # Import and run get_cookie.py's main function
                import subprocess
                import sys
                result = subprocess.run(
                    [sys.executable, str(GET_COOKIE_SCRIPT)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode != 0:
                    log.error(f"get_cookie.py failed: {result.stderr}")
                    return None
                # Reload the cookie file
                data = json.loads(CAPTCHA_COOKIE_FILE.read_text())
                if isinstance(data, dict):
                    cookies = data.get("cookies", [])
                else:
                    cookies = data

        return cookies if cookies else None
    except Exception as e:
        log.error(f"Failed to load captcha cookie: {e}")
        return None


async def inject_cookie(page, cookie: dict):
    """Navigate to the cookie domain and inject exactly one supplied cookie.
    Always injects regardless of expiry; never raises on verification."""
    name = cookie.get("name")
    domain = cookie.get("domain")
    path = cookie.get("path", "/")

    if not name or not domain:
        raise ValueError(
            f"Invalid cookie: name={name!r}, domain={domain!r}"
        )

    cookie_dict = {
        "name": name,
        "value": cookie.get("value", ""),
        "domain": domain,
        "path": path,
        "httpOnly": cookie.get("httpOnly", False),
        "secure": cookie.get("secure", True),
        "sameSite": cookie.get("sameSite", "Lax"),
    }

    cookie_host = domain.lstrip(".")
    target_url = f"https://{cookie_host}/"

    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
    except Exception as exc:
        log(f"Failed to inject cookie: {name} — navigation warning: {exc}")
        return None

    try:
        await page.context.add_cookies([cookie_dict])
        log("Accessibility cookie injected")
        return cookie_dict
    except Exception as exc:
        log(f"Failed to inject cookie: {name} — {exc}")
        return None


def run_get_cookie() -> bool:
    """Run get_cookie.py and stream its output. Returns True if cookie was obtained."""
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(GET_COOKIE_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(f"  [get_cookie] {line}")
        proc.wait(timeout=120)
        if proc.returncode != 0:
            log(f"get_cookie.py failed (exit {proc.returncode})")
            return False
        return CAPTCHA_COOKIE_FILE.exists()
    except subprocess.TimeoutExpired:
        log("get_cookie.py timed out")
        if proc:
            proc.kill()
        return False
    except Exception as e:
        log(f"get_cookie.py error: {e}")
        return False





async def launch_chrome_and_fill_cookie(profile_suffix: str = "") -> tuple:
    """Launch Chrome via Patchright persistent context with cookie injection.
    
    Uses a Chrome profile directory to maintain session state across runs.
    Optionally creates a unique profile suffix to avoid tracking across signups.
    """
    import tempfile as _tempfile
    import shutil as _shutil
    
    # Ensure display is set up - must do this BEFORE launching browser
    display = os.environ.get("DISPLAY", ":1")
    display_num = display.lstrip(":").split(".")[0]
    
    # Check if Xvfb is already running on our display
    if not Path(f"/tmp/.X11-unix/X{display_num}").exists():
        log(f"  Starting Xvfb on {display} ...")
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(2)  # Give Xvfb time to start
        
        # Verify Xvfb started
        if not Path(f"/tmp/.X11-unix/X{display_num}").exists():
            log(f"  ERROR: Failed to start Xvfb on {display}")
            raise RuntimeError(f"Xvfb failed to start on {display}")
        
        log(f"  ✓ Xvfb running on {display}")
    
    os.environ["DISPLAY"] = display
    
    # Create temporary profile directory based on the base Chrome profile
    base_profile = DEFAULT_PROFILE_DIR
    if profile_suffix:
        temp_profile = Path(_tempfile.mkdtemp(prefix=f"patchright_profile_{profile_suffix}_", dir="/tmp"))
    else:
        temp_profile = Path(_tempfile.mkdtemp(prefix="patchright_profile_", dir="/tmp"))
    
    # Copy base profile to temp directory (if it exists)
    if base_profile.exists():
        def ignore_locks(directory, files):
            ignore = {"lock", "parent.lock", ".parentlock", "Session Storage"}
            return [f for f in files if f in ignore]
        _shutil.copytree(base_profile, temp_profile, dirs_exist_ok=True, ignore=ignore_locks)
    else:
        # Create a basic Chrome profile structure if base doesn't exist
        (temp_profile / "Default").mkdir(exist_ok=True)
    
    pw = await async_playwright().start()
    _active_pw.append(pw)
    
    # Launch Chrome persistent context with Patchright
    # Same configuration as get_cookie.py uses
    browser = await pw.chromium.launch_persistent_context(
        user_data_dir=str(temp_profile),
        channel="chrome",
        headless=False,
        no_viewport=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    
    context = browser  # For launch_persistent_context, browser IS the context
    page = context.pages[0] if context.pages else await context.new_page()
    
    cookies = load_captcha_cookie()
    if cookies:
        # Always navigate to hCaptcha first so cookie injection succeeds
        try:
            await page.goto("https://www.hcaptcha.com", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        for cookie in cookies:
            try:
                await inject_cookie(page, cookie)
            except Exception as e:
                log(f"Failed to inject cookie: {cookie.get('name')}: {e}")
    else:
        log("No valid captcha cookie found")
    
    return pw, browser, context, page, temp_profile


def _validate_email(email: str) -> None:
    if not email:
        raise ValueError("Email is required")

    at_idx = email.find("@")
    if at_idx == -1 or at_idx == 0 or at_idx == len(email) - 1:
        raise ValueError("Invalid email format")

    domain = email[at_idx + 1:]
    if "." not in domain:
        raise ValueError("Invalid email format")


def create_agentmail_email() -> Optional[str]:
    subprocess.run(
        [sys.executable, str(AGENTMAIL_SCRIPT), "burn"],
        capture_output=True,
    )

    result = subprocess.run(
        [sys.executable, str(AGENTMAIL_SCRIPT), "create"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    if result.returncode != 0:
        return None

    email = (
        result.stdout.strip().splitlines()[-1]
        if result.stdout.strip()
        else ""
    )

    if not email:
        return None

    _validate_email(email)
    return email


def _poll_agentmail(timeout: int) -> Optional[str]:
    """Generic polling loop for verification URL."""
    start = time.time()

    while time.time() - start < timeout:
        try:
            result = subprocess.run(
                [sys.executable, str(AGENTMAIL_SCRIPT), "check"],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

            if result.returncode == 0 and result.stdout.strip():
                url = result.stdout.strip().splitlines()[-1]

                if (
                    HF_CONFIRMATION_URL_PREFIX in url
                    or "huggingface.co/email_confirmation" in url
                ):
                    return url

        except Exception:
            pass

        time.sleep(EMAIL_POLL_INTERVAL)

    return None


def poll_verification_email(timeout: int = 7) -> Optional[str]:
    """Poll for verification email for exactly `timeout` seconds."""
    return _poll_agentmail(timeout)


def poll_verification_email_long(
    timeout: int = EMAIL_POLL_TIMEOUT,
) -> Optional[str]:
    return _poll_agentmail(timeout)


async def _fill_and_verify(page, selector: str, selector_alt: str, value: str, field_name: str, timeout: int = 15000, verbose: bool = True):
    """Fill a form field and verify the value was accepted.
    Handles visibility, scroll, clear, and fill verification.
    Retries silently until the value is 100% confirmed present.
    """
    locator = None
    try:
        await page.wait_for_selector(selector, state="visible", timeout=timeout)
        locator = page.locator(selector).first
    except Exception:
        try:
            await page.wait_for_selector(selector_alt, state="visible", timeout=timeout)
            locator = page.locator(selector_alt).first
        except Exception:
            raise Exception(f"Field '{field_name}' not found (selectors: {selector}, {selector_alt})")

    if locator:
        # Wait for element to be attached and enabled
        try:
            await locator.wait_for(state="visible", timeout=5000)
        except Exception:
            pass
        
        # Silently retry fill until value is verified — no logging on failure
        max_attempts = 10
        filled = None
        for attempt in range(max_attempts):
            try:
                # Try clear + fill
                await locator.evaluate("(el) => { el.value = ''; }")  # Force-clear via JS
                await asyncio.sleep(0.1)
                await locator.fill(value)
                await asyncio.sleep(0.2)  # Give React a moment to process
                
                filled = await locator.input_value()
                if filled == value:
                    break  # Successfully verified
                # If not matched, retry
            except Exception:
                try:
                    # Alternative approach: use page.fill directly
                    await page.fill(selector, value)
                    await asyncio.sleep(0.2)
                    filled = await locator.input_value()
                    if filled == value:
                        break
                except Exception:
                    pass
        
        if verbose:
            log(f"Filled {field_name}")


async def _wait_and_validate_url(page, expected_substring: str, timeout: int = 30000):
    """Wait for load state then verify we're on the expected page."""
    await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    if expected_substring not in page.url:
        log(f"WARNING: Expected URL containing '{expected_substring}', got: {page.url}")
        return False
    return True


async def _click_and_verify(page, selector_text: str, log_label: str, expected_url_substring: Optional[str] = None):
    """Click a button and verify the page changed (URL or content).

    Returns True if click succeeded and page changed, False otherwise.
    """
    btns = await page.locator('button[type="submit"]').all()
    clicked = False
    for btn in btns:
        btn_text = ""
        try:
            btn_text = (await btn.inner_text()).lower()
            if selector_text in btn_text:
                initial_url = page.url
                await btn.click()
                await asyncio.sleep(0.5)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                if expected_url_substring and page.url == initial_url:
                    pass  # URL unchanged — proceed anyway
                clicked = True
                break
        except Exception as e:
            log(f"  Error clicking button '{btn_text}': {e}")
    if not clicked:
        raise Exception(f"{log_label} button not found")
    return True


async def run_full_signup(email: str, password: str) -> Optional[str]:
    profile_suffix = f"signup_{int(time.time() * 1000) % 100000}"
    pw, browser, context, page, temp_profile = await launch_chrome_and_fill_cookie(profile_suffix)

    try:
        # Navigate to join page
        log("Opened HuggingFace signup page")
        await page.goto(HF_JOIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await _wait_and_validate_url(page, "huggingface.co/join")

        # Generate random username and fullname
        adjectives = ["swift", "crimson", "quantum", "nebula", "alpine", "cobalt",
                      "ember", "frost", "glitch", "hollow", "iron", "jade", "kinetic"]
        nouns = ["falcon", "panther", "ranger", "voyager", "sentinel", "wanderer",
                 "seeker", "vertex", "nexus", "forge", "apex", "blitz"]
        first_names = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Avery"]
        last_names = ["Smith", "Jones", "Brown", "Davis", "Miller", "Wilson", "Moore"]
        username = f"{random.choice(adjectives)}-{random.choice(nouns)}-{random.randint(100,999)}"
        fullname = f"{random.choice(first_names)} {random.choice(last_names)}"

        log("Account details generated")

        # Step 1: Fill email
        await _fill_and_verify(page, 'input[name="email"]', 'input[type="email"]', email, "email", verbose=False)

        # Step 2: Fill password (same page on HF join)
        await _fill_and_verify(page, 'input[name="password"]', 'input[type="password"]', password, "password", verbose=False)

        log("Signup form completed")

        # Step 3: Click Next and verify we're on the account creation form
        await _click_and_verify(page, "next", "Next")
        # Verify navigation by waiting for username field (appears on next step)
        await page.wait_for_selector('input[name="username"]', timeout=10000)

        # Step 4: Fill username + fullname
        await _fill_and_verify(page, 'input[name="username"]', 'input[autocomplete="off"]', username, "username", verbose=False)
        await _fill_and_verify(page, 'input[name="fullname"]', 'input[name="full_name"]', fullname, "fullname", verbose=False)

        # Step 6: Accept TOS
        tos_checkboxes = await page.locator('input[type="checkbox"]').all()
        tos_checked = False
        if tos_checkboxes:
            for cb in tos_checkboxes:
                if not await cb.is_checked():
                    await cb.check()
                    tos_checked = True
                    break
            if not tos_checked:
                tos_checked = True  # already checked

        # Step 7: Click Create Account
        await _click_and_verify(page, "create", "Create Account")

        # Pre-submission cookie check: ensure exactly 1 hc_accessibility cookie.
        browser_cookies = await page.context.cookies()
        hc_cookies = [c for c in browser_cookies if c.get("name") == "hc_accessibility"]
        if len(hc_cookies) > 1:
            log(f"WARNING: {len(hc_cookies)} hc_accessibility cookies present — removing duplicates")
        # Verify form submission by checking if password field detaches
        try:
            await page.wait_for_selector('input[name="password"]', state="detached", timeout=10000)
        except Exception:
            pass  # Will rely on email polling

        # Step 8: Wait for verification email
        log("Waiting for verification email")
        verification_url = poll_verification_email(timeout=7)
        if not verification_url:
            log("Failed to receive verification email")
            return None

        # Step 9: Navigate to verification URL
        log("Verification URL opened")
        await page.goto(verification_url, wait_until="domcontentloaded", timeout=60000)

        await asyncio.sleep(3)

        if "huggingface.co" in page.url and "email_confirmation" not in page.url:
            pass  # Confirmed — account verified
        else:
            await page.goto("https://huggingface.co/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

        # Step 10: Navigate to token creation page
        token_url = "https://huggingface.co/settings/tokens/new?tokenType=write"
        await page.goto(token_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("domcontentloaded", timeout=15000)

        # If we got redirected to login, try refreshing and going back
        if "/login" in page.url:
            log("Redirected to login — refreshing and retrying token page...")
            await page.goto("https://huggingface.co/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            await page.goto(token_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_load_state("domcontentloaded", timeout=15000)

        log(f"Token page: {page.url}")

        if "/login" in page.url:
            log("Redirected to login — account not confirmed!")
            return None

        # Step 11: Fill token name
        token_name = f"token-{username}"
        await _fill_and_verify(
            page,
            'input[name="displayName"]',
            'input[type="text"]:first-of-type',  # fallback to first text input
            token_name,
            "token name",
            verbose=False,
        )

        # Step 12: Click Create token
        create_token_btn = page.locator('button[type="submit"]:has-text("Create")')
        if await create_token_btn.count() == 0:
            create_token_btn = page.locator('button[type="submit"]').last
        await create_token_btn.wait_for(state="visible", timeout=15000)
        await create_token_btn.click()
        log("Creating access token")

        # Wait for Hugging Face to finish creating the token.
        # The generated token is not reliably exposed in the DOM, so clipboard
        # extraction is the primary source of truth.
        await asyncio.sleep(3)

        api_key = None

        # Grant clipboard permissions through the browser context.
        try:
            await context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin="https://huggingface.co",
            )
        except Exception as exc:
            log(f"Clipboard permission grant failed: {exc}")

        # The token is only available through the Copy button on this page.
        # Do NOT use broad selectors such as button:has(svg), because those
        # can match unrelated Hugging Face UI buttons.
        copy_selectors = [
            'button[aria-label="Copy token"]',
            'button[aria-label*="Copy token" i]',
            'button:has-text("Copy token")',
            '[data-testid="copy-token"]',
            '[data-testid*="copy-token" i]',
        ]

        copy_btn = None

        # Find the actual token Copy button.
        for selector in copy_selectors:
            try:
                candidate = page.locator(selector)
                count = await candidate.count()
                if count:
                    log(f"Found token copy button: {selector} ({count} match(es))")
                    copy_btn = candidate.first
                    break
            except Exception as exc:
                log(f"Copy selector failed [{selector}]: {exc}")

        if copy_btn is None:
            log("WARNING: Token Copy button was not found with token-specific selectors")

            # Last UI fallback: inspect buttons by their visible text/aria-label
            # without blindly selecting arbitrary SVG buttons.
            try:
                buttons = page.locator("button")
                button_count = await buttons.count()

                for i in range(button_count):
                    try:
                        btn = buttons.nth(i)
                        text = (await btn.inner_text()).strip()
                        aria = (await btn.get_attribute("aria-label") or "").strip()

                        if (
                            "copy token" in text.lower()
                            or "copy token" in aria.lower()
                            or aria.lower() == "copy"
                            or text.lower() == "copy"
                        ):
                            log(
                                f"Found Copy button by inspection: "
                                f"text={text!r}, aria-label={aria!r}"
                            )
                            copy_btn = btn
                            break
                    except Exception:
                        continue
            except Exception as exc:
                log(f"Button inspection failed: {exc}")

        if copy_btn is None:
            log("FAILED: Could not locate the Hugging Face token Copy button")
            return None

        # Click Copy and repeatedly read the browser clipboard.
        # Clipboard reads are retried because the UI can take a moment to
        # populate navigator.clipboard after the click.
        try:
            await copy_btn.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            await copy_btn.click(timeout=10000)
            log("Clicked token Copy button")
        except Exception as exc:
            log(f"Normal Copy click failed: {exc}")

            # Fallback to a real DOM click, preserving the user-gesture path.
            try:
                await copy_btn.evaluate("(el) => el.click()")
                log("Clicked token Copy button via DOM click")
            except Exception as exc2:
                log(f"DOM Copy click also failed: {exc2}")
                return None

        # Give the clipboard operation time to complete, then poll it.
        for attempt in range(1, 11):
            try:
                clipboard = await page.evaluate(
                    "() => navigator.clipboard.readText().catch(() => '')"
                )

                clipboard = (clipboard or "").strip()

                if clipboard.startswith(HF_TOKEN_PREFIX):
                    # Validate the complete token before accepting it.
                    match = re.search(r"hf_[a-zA-Z0-9_-]{20,}", clipboard)
                    if match:
                        api_key = match.group(0)
                        log(f"Token extracted from clipboard on attempt {attempt}")
                        break

                if clipboard:
                    log(
                        f"Clipboard attempt {attempt}/10 returned "
                        f"{len(clipboard)} character(s), but not an HF token"
                    )
                else:
                    log(f"Clipboard attempt {attempt}/10 was empty")

            except Exception as exc:
                log(f"Clipboard read attempt {attempt}/10 failed: {exc}")

            await asyncio.sleep(0.5)

        # Clipboard is the intended extraction path. Keep a very small final
        # retry after the polling loop in case the page finishes copying late.
        if not api_key:
            try:
                await asyncio.sleep(1)
                clipboard = await page.evaluate(
                    "() => navigator.clipboard.readText().catch(() => '')"
                )
                match = re.search(
                    r"hf_[a-zA-Z0-9_-]{20,}",
                    (clipboard or "").strip(),
                )
                if match:
                    api_key = match.group(0)
                    log("Token extracted from final clipboard retry")
            except Exception as exc:
                log(f"Final clipboard retry failed: {exc}")

        if not api_key or not api_key.startswith(HF_TOKEN_PREFIX):
            log("Failed to extract HF token!")
            return None

        log("Token copied and extracted")

        # Step 14: Save token
        KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with KEYS_FILE.open("a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        os.chmod(KEYS_FILE, 0o600)
        log(f"Token saved successfully")
        return api_key

    finally:
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        # Cleanup temp profile
        try:
            import shutil as _shutil
            _shutil.rmtree(temp_profile, ignore_errors=True)
        except Exception:
            pass
        # Stop the browser context
        try:
            await context.close()
        except Exception:
            pass


async def main():
    bootstrap()
    log("Atlas HF — HuggingFace Signup Automation")
    log("Bootstrap complete")

    # Ensure display stack is running (Xvfb + x11vnc + noVNC)
    # First check/install system dependencies
    missing_system = ensure_system_deps()
    if missing_system:
        if is_binary_available("apt-get"):
            install_system_deps(missing_system)
        else:
            pass  # Warnings not needed per user request
    
    # Start display stack — check if Xvfb is already running
    display = os.environ.get("DISPLAY") or VNC_DISPLAY
    display_num = display.lstrip(":").split(".")[0]
    
    if not _x_display_exists(display):
        log("Starting Xvfb ...")
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(2)
        os.environ["DISPLAY"] = display
        log(f"Display {display} started")
    else:
        log(f"Reused existing display {display} + noVNC")

    if not _ensure_patchright():
        log("FATAL: patchright not available and auto-install failed")
        sys.exit(1)

    cleanup_stale_cdp_profiles()

    # Check for hCaptcha cookie
    cookies = load_captcha_cookie()
    if not cookies:
        log("No valid hCaptcha cookie found. Running get_cookie.py...")
        success = run_get_cookie()
        if success:
            log("Cookie obtained successfully")
        else:
            log("WARNING: Failed to obtain hCaptcha cookie")
            return

    # Retry logic: 3 attempts
    for attempt in range(1, 4):
        # Clean up any stale profiles from previous attempts
        cleanup_stale_cdp_profiles()

        # Create fresh email
        email = create_agentmail_email()
        if not email:
            log("Failed to create email, retrying...")
            time.sleep(5)
            continue

        log(f"Email generated: {email}")
        try:
            api_key = await run_full_signup(email, PASSWORD)
            if api_key and api_key.startswith(HF_TOKEN_PREFIX):
                # Success output
                from datetime import datetime
                now = datetime.now()
                log("=== SUCCESS ===")
                log(f"Date: {now.strftime('%Y-%m-%d')}")
                log(f"Time: {now.strftime('%H:%M:%S')}")
                log(f"Token: {api_key[:15]}...")
                log(f"Saved: {KEYS_FILE}")
                log("")
                sys.exit(0)
        except Exception as e:
            log(f"ERROR during signup: {e}")

        log("Signup failed")

        if attempt < 3:
            if attempt == 2:
                log("Refreshing hCaptcha cookie for final attempt...")
                run_get_cookie()
            else:
                log("Retrying...")
            time.sleep(10)

    log("=== FAILED: all 3 attempts exhausted ===")
    sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        cleanup_all()
        cleanup_stale_cdp_profiles()
        import glob
        import shutil
        for pattern in ("/tmp/patchright_tmp_*", "/tmp/hf_profile_*"):
            for d in glob.glob(pattern):
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
