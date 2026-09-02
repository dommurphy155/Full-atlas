#!/usr/bin/env python3
"""
Main orchestrator for HuggingFace signup automation.

Flow:
  1. Launch CDP via launch_cdp.sh (random port, connects to display:1)
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
import time
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    ROOT_DIR, DATA_DIR, KEYS_FILE, AGENTMAIL_SCRIPT, GET_COOKIE_SCRIPT,
    LAUNCH_CDP_SCRIPT, HF_KEYS_SCRIPT, CDP_HOST, CDP_PORT_FILE, CDP_PID_FILE, CDP_TIMEOUT,
    CAPTCHA_COOKIE_FILE, PASSWORD, HF_JOIN_URL, HF_TOKEN_PREFIX,
    HF_CONFIRMATION_URL_PREFIX, EMAIL_POLL_TIMEOUT, EMAIL_POLL_INTERVAL,
    cleanup_stale_cdp_profiles, _kill_chrome_on_cdp_port,
    bootstrap,
    is_py_module_available,
    install_py_module,
    is_camoufox_browser_installed,
    ensure_camoufox_browsers,
    is_patchright_browser_installed,
    ensure_playwright_browsers,
)

# --- patchright / playwright dependency check (system-wide via importlib) ---
if not is_py_module_available("patchright"):
    print("  [SETUP] Installing patchright ...", file=sys.stderr)
    install_py_module("patchright")
if not is_patchright_browser_installed():
    print("  [SETUP] Downloading patchright/chromium browsers ...", file=sys.stderr)
    ensure_playwright_browsers()

from patchright.async_api import async_playwright

# Setup logging — flush immediately for real-time output
import sys as _sys

_logger = logging.getLogger("main")
_logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
_fh = logging.FileHandler(str(DATA_DIR / "orchestrator.log"))
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(_sys.stdout)
_sh.setFormatter(_fmt)
_logger.addHandler(_fh)
_logger.addHandler(_sh)
_logger.propagate = False
logger = _logger


# --- Global cleanup state --- #
_active_cdp_procs: list = []
_active_pw: list = []


def _force_kill_cdp(port: int):
    """Kill Chrome processes associated with a CDP port."""
    import shutil as _shutil
    for cmd in [
        ["pkill", "-f", f"remote-debugging-port={port}"],
        ["fuser", "-k", f"{port}/tcp"],
    ]:
        try:
            _shutil.which(cmd[0]) and subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception:
            pass


def cleanup_all():
    """Kill all active CDP processes, Playwright instances, and temp profiles."""
    # Kill CDP subprocesses by process group
    for proc in _active_cdp_procs:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except Exception:
                pass

    # Kill by port as fallback
    for port in CDP_PORTS:
        _force_kill_cdp(port)

    # Clean temp profile dirs
    import glob
    for pattern in ("/tmp/cdp_browser_profile_*", "/tmp/hf_profile_*"):
        for d in glob.glob(pattern):
            try:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

    _active_cdp_procs.clear()


CDP_PORTS: list = []


def log(msg: str):
    logger.info(msg)


atexit.register(cleanup_all)

# Ensure SIGINT also triggers clean exit
def _handle_sigint(signum, frame):
    log("Interrupted — exiting.")
    cleanup_all()
    sys.exit(130)

signal.signal(signal.SIGINT, _handle_sigint)


def _ensure_camoufox() -> bool:
    """Ensure camoufox is importable AND browser binaries are present
    (checked system-wide, not just local/venv)."""
    if not is_py_module_available("camoufox"):
        print("  [SETUP] Installing camoufox ...", file=sys.stderr)
        install_py_module("camoufox")
    if is_py_module_available("camoufox") and not is_camoufox_browser_installed():
        print("  [SETUP] Downloading camoufox browsers ...", file=sys.stderr)
        ensure_camoufox_browsers()
    return is_py_module_available("camoufox") and is_camoufox_browser_installed()


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
    """Navigate to the cookie domain, inject exactly one supplied cookie,
    and verify that it landed in the browser cookie jar."""
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

    expiry = cookie.get("expires")
    if expiry and expiry > 0:
        cookie_dict["expires"] = expiry

    cookie_host = domain.lstrip(".")
    target_url = f"https://{cookie_host}/"

    log(
        f"Cookie target: name={name!r} "
        f"domain={domain!r} path={path!r}"
    )

    # Navigate to the supplied cookie's domain first.
    try:
        await page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=15000,
        )
    except Exception as exc:
        log(f"Cookie-domain navigation warning: {exc}")

    # Inject ONLY the cookie supplied by the caller.
    log(
        f"Injecting cookie: name={name!r} "
        f"domain={domain!r} path={path!r}"
    )
    await page.context.add_cookies([cookie_dict])

    # Immediately inspect the browser cookie jar.
    browser_cookies = await page.context.cookies()

    # DEBUG: dump all cookies to see what Playwright actually returns
    log(f"DEBUG: {len(browser_cookies)} cookies in jar after add_cookies:")
    for c in browser_cookies:
        log(f"  DEBUG cookie: name={c.get('name')!r} domain={c.get('domain')!r} path={c.get('path')!r} expires={c.get('expires')!r}")

    # Playwright normalizes cookie domains: a leading dot (.hcaptcha.com)
    # is stored/returned as the bare domain (hcaptcha.com). Normalize both
    # sides so the match succeeds regardless of how the cookie was supplied.
    matches = [
        c for c in browser_cookies
        if c.get("name") == name
        and c.get("domain", "").lstrip(".") == domain.lstrip(".")
        and c.get("path", "/") == path
    ]

    if not matches:
        # Fallback: try matching with raw domain (in case normalization differs)
        matches_raw = [
            c for c in browser_cookies
            if c.get("name") == name and c.get("domain", "") == domain
        ]
        log(f"DEBUG: normalized match failed, raw match count={len(matches_raw)}")
        if matches_raw:
            log(f"DEBUG: raw match domain={matches_raw[0].get('domain')!r}")
            matches = matches_raw

    if not matches:
        log(
            f"COOKIE VERIFICATION FAILED: "
            f"name={name!r} domain={domain!r} path={path!r}"
        )

        log("Browser cookies currently present:")
        for c in browser_cookies:
            log(
                f"  name={c.get('name')!r} "
                f"domain={c.get('domain')!r} "
                f"path={c.get('path', '/')!r}"
            )

        raise RuntimeError(
            f"Cookie did not appear in browser jar: "
            f"{name!r} {domain!r} {path!r}"
        )

    landed = matches[0]

    log(
        f"COOKIE VERIFIED: "
        f"name={landed.get('name')!r} "
        f"domain={landed.get('domain')!r} "
        f"path={landed.get('path', '/')!r}"
    )

    return landed




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


async def _verify_cookies_injected(page, cookies: list) -> bool:
    """Verify all supplied cookies are present in the browser jar
    with correct name + domain + path. Domain must match after
    stripping a leading dot (browser normalizes '.hcaptcha.com' → 'hcaptcha.com').
    """
    browser_cookies = await page.context.cookies()
    for cookie in cookies:
        name = cookie.get("name")
        domain = cookie.get("domain", "").lstrip(".")
        path = cookie.get("path", "/")
        found = any(
            c.get("name") == name
            and c.get("domain", "").lstrip(".") == domain
            and c.get("path", "/") == path
            for c in browser_cookies
        )
        if not found:
            log(f"Cookie verification failed: {name} @ {domain}{path}")
            return False
    return True


async def launch_cdp_and_fill_cookie(profile_suffix: str = "") -> tuple:
    """Launch CDP and optionally use a unique /tmp profile to avoid Google
    profile tracking across multiple signups."""
    port = find_free_port()
    CDP_PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CDP_PORT_FILE.write_text(str(port), encoding="utf-8")
    os.chmod(CDP_PORT_FILE, 0o600)

    env = {**os.environ, "DISPLAY": ":1", "CDP_PORT": str(port)}
    if profile_suffix:
        env["HF_PROFILE_SUFFIX"] = profile_suffix

    cdp_proc = subprocess.Popen(
        ["bash", str(LAUNCH_CDP_SCRIPT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )

    CDP_PORTS.append(port)
    _active_cdp_procs.append(cdp_proc)

    if not wait_for_cdp(port, timeout=CDP_TIMEOUT):
        try:
            os.killpg(os.getpgid(cdp_proc.pid), signal.SIGKILL)
        except Exception:
            cdp_proc.kill()
        raise RuntimeError("CDP did not become ready")

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(f"http://{CDP_HOST}:{port}")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()

    cookies = load_captcha_cookie()
    if cookies:
        # Always navigate to hCaptcha first so cookie injection succeeds
        try:
            await page.goto("https://www.hcaptcha.com", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        injected = []
        failed = []
        for cookie in cookies:
            try:
                await inject_cookie(page, cookie)
                injected.append(cookie.get("name"))
            except Exception as e:
                failed.append(f"{cookie.get('name')}: {e}")
        ok = await _verify_cookies_injected(page, cookies)
        if injected and ok:
            log(f"Injected cookie: {', '.join(injected)}")
        else:
            log(f"Failed to inject cookie: {'; '.join(failed) if failed else 'verification failed'}")
    else:
        log("No valid captcha cookie found")

    return pw, browser, context, page, port, cdp_proc


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
    """
    locator = None
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        locator = page.locator(selector).first
    except Exception:
        try:
            await page.wait_for_selector(selector_alt, timeout=timeout)
            locator = page.locator(selector_alt).first
        except Exception:
            raise Exception(f"Field '{field_name}' not found (selectors: {selector}, {selector_alt})")

    if locator:
        try:
            await locator.scroll_into_view_if_needed()
        except Exception:
            pass  # Element may already be visible or detached — try fill anyway
        await locator.clear()
        await locator.fill(value)
        await asyncio.sleep(0.1)  # Small delay between fills

        filled = await locator.input_value()
        if filled != value:
            log(f"WARNING: {field_name} fill rejected — expected {value[:5]}..., got {filled[:5]}...")
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
    pw, browser, context, page, port, cdp_proc = await launch_cdp_and_fill_cookie(profile_suffix)

    try:
        # Navigate to join page
        log("Navigating to https://huggingface.co/join ...")
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

        log(f"Username: {username}, Fullname: {fullname}")
        log("Filling signup form...")

        # Step 1: Fill email
        await _fill_and_verify(page, 'input[name="email"]', 'input[type="email"]', email, "email", verbose=False)

        # Step 2: Fill password (same page on HF join)
        await _fill_and_verify(page, 'input[name="password"]', 'input[type="password"]', password, "password", verbose=False)

        log("Clicking Next...")

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
        log("Submitting form...")
        # Verify form submission by checking if password field detaches
        try:
            await page.wait_for_selector('input[name="password"]', state="detached", timeout=10000)
        except Exception:
            pass  # Will rely on email polling

        # Step 8: Wait for verification email
        log("Waiting for verification email...")
        verification_url = poll_verification_email(timeout=7)
        if not verification_url:
            log("Failed to receive verification email")
            return None

        # Step 9: Navigate to verification URL
        log("Navigating to verification URL...")
        await page.goto(verification_url, wait_until="domcontentloaded", timeout=60000)

        await asyncio.sleep(3)

        if "huggingface.co" in page.url and "email_confirmation" not in page.url:
            pass  # Confirmed — account verified
        else:
            await page.goto("https://huggingface.co/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

        # Step 10: Navigate to token creation page
        log("Navigating to token creation page...")
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
        log("Creating token...")

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
            log("Clipboard permissions granted")
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

        log(f"Token extracted: {api_key[:10]}...")

        # Step 14: Save token
        KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with KEYS_FILE.open("a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        os.chmod(KEYS_FILE, 0o600)
        log(f"Token saved to {KEYS_FILE}")
        return api_key

    finally:
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        # Kill the CDP process group (bash + Chrome + Xvfb)
        try:
            os.killpg(os.getpgid(cdp_proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                cdp_proc.kill()
            except Exception:
                pass
        try:
            cdp_proc.wait(timeout=5)
        except Exception:
            pass
        # Port-based cleanup as fallback
        _force_kill_cdp(port)
        # Remove from active lists
        if cdp_proc in _active_cdp_procs:
            _active_cdp_procs.remove(cdp_proc)


async def main():
    bootstrap()
    log("=" * 60)
    log("HuggingFace Signup Automation")
    log("=" * 60)

    if not _ensure_camoufox():
        log("FATAL: camoufox not available and auto-install failed")
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

    # Retry logic: 3 attempts
    # Attempt 1: initial
    # Attempt 2: same cookie, retry (hCaptcha might have been rate-limited)
    # Attempt 3: fresh cookie from get_cookie.py
    for attempt in range(1, 4):
        log(f"\n{'='*60}")
        log(f"Attempt {attempt}/3")
        log(f"{'='*60}")

        # Clean up any stale profiles from previous attempts
        cleanup_stale_cdp_profiles()

        # Create fresh email
        email = create_agentmail_email()
        if not email:
            log("Failed to create email, retrying...")
            time.sleep(5)
            continue

        log(f"Email: {email}")
        try:
            api_key = await run_full_signup(email, PASSWORD)
            if api_key and api_key.startswith(HF_TOKEN_PREFIX):
                log(f"\n=== SUCCESS ===")
                log(f"Token: {api_key[:15]}...")
                log(f"Saved to: {KEYS_FILE}")
                log("=" * 60)
                sys.exit(0)
        except Exception as e:
            log(f"ERROR during signup: {e}")
            import traceback
            traceback.print_exc()

        log("Signup failed")

        if attempt < 3:
            if attempt == 2:
                log("Refreshing hCaptcha cookie for final attempt...")
                run_get_cookie()
            else:
                log("Retrying...")
            log("Waiting 10s before retry...")
            time.sleep(10)

    log(f"\n=== FAILED: all 3 attempts exhausted ===")
    sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        cleanup_all()
        cleanup_stale_cdp_profiles()
        import glob
        import shutil
        for pattern in ("/tmp/cdp_browser_profile_*", "/tmp/hf_profile_*"):
            for d in glob.glob(pattern):
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
