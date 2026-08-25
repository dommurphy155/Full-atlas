#!/usr/bin/env python3
"""Retrieve a reusable hCaptcha cookie by reusing saved Chrome profiles.

This module iterates over every Chrome profile saved by the profile-creator,
launches a real Chrome browser on DISPLAY=:1 with that persistent profile,
and tries to obtain a valid hCaptcha session cookie.  As soon as any profile
yields a cookie it is written to ``~/atlas/data/captcha_cookie.json`` and the
script exits successfully.  If every profile fails the user is informed to
re-run the profile creator.

Usage:
    ~/.venv/bin/python3 -m huggingface.scripts.get_cookie
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Import config for canonical paths (cookie output + project root)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CAPTCHA_COOKIE_FILE as COOKIE_OUTPUT_PATH, PROJECT_ROOT

from patchright.sync_api import sync_playwright as pw_sync
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Profiles now resolve from the shared Firefox-profile directory
CHROME_PROFILES_ROOT = PROJECT_ROOT / "data" / "firefox_profiles"

HCAPTCHA_LOGIN_URL = "https://dashboard.hcaptcha.com/login"
HCAPTCHA_SET_COOKIE_BTN = "text=Set Cookie"
HCAPTCHA_COOKIE_SET_BTN = "text=Cookie Set"
HCAPTCHA_COOKIE_SUCCESS_TEXTS = ["Cookie Set", "cookie set"]

# How long to wait for the "Set Cookie" button to appear (seconds)
SET_COOKIE_TIMEOUT = 30_000
# How long to wait for page loads / navigations
PAGE_LOAD_TIMEOUT = 60_000
# How long to wait for provider button visibility
ELEMENT_TIMEOUT = 15_000
# Max time to wait for authentication to complete (auto-login from stored session)
AUTH_WAIT_TIMEOUT = 45_000

DISPLAY = ":1"

# Chrome stealth flags — make the browser look non-automated
CHROME_FLAGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-web-security",
    "--disable-site-isolation-trials",
    "--disable-features=IsolateOrigins,site-per-process,Translate,MediaRouter",
    "--window-size=1920,1080",
    "--no-first-run",
    "--no-default-browser-check",
    "--lang=en-GB",
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("get_cookie")


def log_info(msg: str) -> None:
    log.info(msg)


def log_warn(msg: str) -> None:
    log.warning(msg)


def log_error(msg: str) -> None:
    log.error(msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_free_port() -> int:
    """Allocate a free TCP port for Chrome's CDP endpoint."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def find_chrome_binary() -> str:
    """Return the path to the system Chrome binary."""
    for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(candidate)
        if path:
            return path
    raise FileNotFoundError(
        "No Chrome/Chromium binary found on PATH. Install google-chrome or chromium."
    )


def discover_profiles() -> list[tuple[Path, str]]:
    """Find all saved Firefox/profile directories.

    Scans the shared profile root for subdirectories that contain Firefox
    profile data (e.g. *prefs.js* / *cookies.sqlite*).  Returns a list of
    *(profile_dir, provider)* tuples where *provider* is ``"google"`` or
    ``"hotmail"`` inferred from the profile directory name.
    """
    profiles: list[tuple[Path, str]] = []
    if not CHROME_PROFILES_ROOT.exists():
        log_warn(f"Profile root not found: {CHROME_PROFILES_ROOT}")
        return profiles

    for entry in sorted(CHROME_PROFILES_ROOT.rglob("*")):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name.startswith("_") or entry.name in ("firefox", "chrome"):
            continue
        # Only treat directories that look like Firefox profiles (contain prefs.js or cookies.sqlite)
        if not (entry / "prefs.js").exists() and not (entry / "cookies.sqlite").exists():
            continue
        provider = "google" if "gmail" in entry.name.lower() else "hotmail"
        profiles.append((entry, provider))
        log_info(f"Discovered profile: {entry.name} (provider: {provider})")
    return profiles


# ---------------------------------------------------------------------------
# Browser driver
# ---------------------------------------------------------------------------

class ProfileBrowser:
    """Manage a single Chrome instance launched with a persistent profile.

    This class encapsulates the full lifecycle:

    * Launch Chrome with ``--remote-debugging-port`` on a free port.
    * Connect via Playwright/CDP.
    * Navigate, click, wait, and extract cookies.
    * Clean shutdown.
    """

    def __init__(self, profile_dir: Path, provider: str):
        self.profile_dir = profile_dir
        self.provider = provider
        self.debug_port: Optional[int] = None
        self.chrome_pid: Optional[int] = None
        self._pw = None
        self._browser = None
        self._page = None
        self._chrome_binary: Optional[str] = None

    def launch_chrome(self) -> None:
        """Launch Chrome via patchright with persistent profile and stealth.

        Uses patchright's launch_persistent_context which properly handles
        --user-data-dir and injects stealth automatically.
        """
        os.environ["DISPLAY"] = DISPLAY

        log_info(f"Launching Chrome with profile: {self.profile_dir}")
        self._chrome_binary = find_chrome_binary()

        # Build Chrome args — include persistent profile + stealth flags
        chrome_args = [
            f"--user-data-dir={self.profile_dir}",
        ] + CHROME_FLAGS

        log_info(f"Chrome args: {chrome_args}")

        # Use patchright to launch Chrome with persistent context
        # This gives us a browser + context that we can connect to via CDP
        import subprocess
        self.debug_port = get_free_port()
        log_info(f"CDP port: {self.debug_port}")

        full_cmd = [self._chrome_binary, f"--remote-debugging-port={self.debug_port}"] + chrome_args

        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.chrome_pid = proc.pid
        log_info(f"Chrome process PID: {self.chrome_pid}")

        # Wait for CDP to be ready by polling the /json/version endpoint
        import urllib.request
        cdp_url = f"http://127.0.0.1:{self.debug_port}/json/version"
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"Chrome process exited prematurely (code={proc.returncode})")
            try:
                with urllib.request.urlopen(cdp_url, timeout=3) as resp:
                    if resp.status == 200:
                        log_info("Chrome CDP is ready")
                        return
            except Exception:
                pass
            time.sleep(0.5)

        raise RuntimeError("Chrome CDP did not become ready within 30s")

    def connect(self) -> None:
        """Connect to the running Chrome via patchright CDP and apply stealth."""
        log_info("Connecting via patchright over CDP ...")
        self._pw = pw_sync().start()

        cdp_endpoint = f"http://127.0.0.1:{self.debug_port}"
        self._browser = self._pw.chromium.connect_over_cdp(cdp_endpoint)
        log_info(f"Connected to Chrome via CDP: {cdp_endpoint}")

        # Grab the first available page / create a new one
        contexts = self._browser.contexts
        if contexts:
            context = contexts[0]
        else:
            context = self._browser

        pages = context.pages
        if pages:
            self._page = pages[0]
        else:
            self._page = context.new_page()

        # Apply stealth via patchright's built-in stealth + navigator overrides
        self._apply_stealth()

    def _apply_stealth(self) -> None:
        """Inject lightweight stealth overrides into the page context."""
        if not self._page:
            return

        stealth_js = """
        // Overwrite webdriver property
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // Overwrite plugins
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5].map(i => ({ filename: `plugin${i}`, description: `Plugin ${i}` })),
        });

        // Overwrite languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-GB', 'en'],
        });

        // Overwrite permissions
        const origQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (origQuery) {
            window.navigator.permissions.query = (parameters) => {
                return Promise.resolve({ state: 'granted' }).then(permission => {
                    return permission;
                });
            };
        }
        """
        try:
            self._page.add_init_script(stealth_js)
            log_info("Stealth overrides injected")
        except Exception as e:
            log_warn(f"Stealth injection failed: {e}")

    def disconnect(self) -> None:
        """Cleanly close the browser and stop Chrome."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass

        if self.chrome_pid:
            try:
                import signal

                os.kill(self.chrome_pid, signal.SIGTERM)
                time.sleep(1)
                # Force-kill if still alive
                try:
                    os.kill(self.chrome_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Page interactions
    # ------------------------------------------------------------------

    def navigate_to_login(self) -> None:
        """Navigate to the hCaptcha dashboard login page."""
        log_info(f"Navigating to {HCAPTCHA_LOGIN_URL}")
        self._page.set_default_timeout(PAGE_LOAD_TIMEOUT)
        self._page.goto(HCAPTCHA_LOGIN_URL, wait_until="domcontentloaded")
        self._page.wait_for_load_state("networkidle")
        log_info(f"Page loaded: title='{self._page.title()}'")

    def click_provider_signin(self) -> bool:
        """Click the Google or Microsoft sign-in button based on provider type.

        Returns True if a known button was clicked, False otherwise.
        """
        if self.provider == "google":
            text_label = "Sign in with Google"
            fallback_text = "Google"
        else:
            text_label = "Sign in with Microsoft"
            fallback_text = "Mircosoft"

        selectors = [
            f"text={text_label}",
            f"button:has-text('{text_label}')",
            f"button:has-text('{fallback_text}')",
            f"div:has-text('{text_label}')",
            f"span:has-text('{text_label}')",
        ]

        for sel in selectors:
            try:
                locator = self._page.locator(sel)
                count = locator.count()
                if count > 0:
                    # Wait for the button to be visible and clickable
                    locator.first.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
                    before_url = self._page.url
                    locator.first.click()
                    log_info(f"Clicked: '{text_label}' ({sel})")

                    # Wait for navigation or URL change
                    try:
                        self._page.wait_for_load_state("networkidle", timeout=AUTH_WAIT_TIMEOUT)
                    except PlaywrightTimeoutError:
                        log_warn("Navigation after click timed out — may be a popup or SPA update")

                    return True
            except Exception as e:
                log_warn(f"Selector '{sel}' failed: {e}")
                continue

        log_warn(f"Could not find '{text_label}' button — user may already be logged in")
        return False

    def wait_for_set_cookie_button(self) -> bool:
        """Wait for the 'Set Cookie' button to appear on the page.

        Returns True if found, False on timeout.
        """
        log_info("Waiting for 'Set Cookie' button ...")
        try:
            self._page.wait_for_selector(HCAPTCHA_SET_COOKIE_BTN, state="visible", timeout=SET_COOKIE_TIMEOUT)
            log_info("Found 'Set Cookie' button")
            return True
        except PlaywrightTimeoutError:
            log_warn("Timed out waiting for 'Set Cookie' button")
            return False

    def click_set_cookie(self) -> bool:
        """Click the 'Set Cookie' button and wait for success confirmation."""
        log_info("Clicking 'Set Cookie' ...")
        try:
            locator = self._page.locator(HCAPTCHA_SET_COOKIE_BTN)
            locator.first.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
            locator.first.click()
        except Exception as e:
            log_error(f"Failed to click 'Set Cookie': {e}")
            return False

        # Wait for the button to change to "Cookie Set" or another success indicator
        log_info("Waiting for cookie confirmation ...")
        for success_text in HCAPTCHA_COOKIE_SUCCESS_TEXTS:
            try:
                self._page.wait_for_selector(f"text={success_text}", state="visible", timeout=SET_COOKIE_TIMEOUT)
                log_info(f"Cookie confirmation found: '{success_text}'")
                return True
            except PlaywrightTimeoutError:
                continue

        # Also check if the button text itself changed to "Cookie Set"
        try:
            self._page.wait_for_selector(HCAPTCHA_COOKIE_SET_BTN, state="visible", timeout=5_000)
            log_info("Button changed to 'Cookie Set'")
            return True
        except PlaywrightTimeoutError:
            pass

        # Last check: maybe the page shows a success message or redirect
        current_url = self._page.url
        if "set" in current_url.lower() or "cookie" in current_url.lower():
            log_info(f"Possible success — URL changed to: {current_url}")
            return True

        # Final fallback — check button text directly
        try:
            time.sleep(3)
            btn = self._page.locator("text=Cookie Set")
            if btn.count() > 0:
                log_info("Button text is now 'Cookie Set'")
                return True
        except Exception:
            pass

        log_warn("No explicit cookie-set confirmation found")
        return False

    def extract_cookies(self) -> list[dict]:
        """Extract all cookies from the current browser context."""
        if not self._page:
            raise RuntimeError("No page context to extract cookies from")

        context = self._page.context
        cookies = context.cookies()
        log_info(f"Extracted {len(cookies)} cookie(s)")
        return cookies

    def is_login_page(self) -> bool:
        """Check whether we're still on the login page (i.e., auth failed).

        Returns True if the login page buttons are still present, indicating
        the profile is NOT authenticated.
        """
        try:
            for selector in ["text=Sign in with Google", "text=Sign in with Microsoft"]:
                if self._page.locator(selector).count() > 0:
                    return True
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Cookie extraction & persistence
# ---------------------------------------------------------------------------

def build_cookie_json(cookies: list[dict]) -> str:
    """Serialise only hc_accessibility cookies into a structured JSON document."""
    hcaptcha_cookies = [c for c in cookies if c.get("name") == "hc_accessibility"]
    if not hcaptcha_cookies:
        log_warn("No hc_accessibility cookie found in extracted cookies")
    data = {
        "cookies": hcaptcha_cookies,
        "domain": ".hcaptcha.com",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "get_cookie.py",
        "description": "Authenticated hCaptcha session cookies obtained via saved Chrome profile.",
    }
    return json.dumps(data, indent=2)


def save_cookie_file(cookies: list[dict]) -> Path:
    """Filter to hc_accessibility only and write the cookie JSON to the canonical output path."""
    hcaptcha_cookies = [c for c in cookies if c.get("name") == "hc_accessibility"]
    log_info(f"Saving {len(hcaptcha_cookies)} hc_accessibility cookie(s) to: {COOKIE_OUTPUT_PATH}")

    COOKIE_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    json_str = build_cookie_json(hcaptcha_cookies)
    COOKIE_OUTPUT_PATH.write_text(json_str, encoding="utf-8")
    log_info(f"Cookies saved to: {COOKIE_OUTPUT_PATH}")
    return COOKIE_OUTPUT_PATH


# ---------------------------------------------------------------------------
# Per-profile login attempt
# ---------------------------------------------------------------------------

def attempt_profile(profile_dir: Path, provider: str) -> Optional[list[dict]]:
    """Try to obtain hCaptcha cookies using a single profile.

    Returns the cookie list on success, or None on failure.
    """
    browser = ProfileBrowser(profile_dir, provider)
    try:
        browser.launch_chrome()
        browser.connect()

        browser.navigate_to_login()

        # Check if we're already on the login page (not authenticated)
        # If the profile is authenticated, the provider buttons won't be visible
        # and we should see the dashboard or "Set Cookie" directly.
        if browser.is_login_page():
            log_warn(f"Profile '{profile_dir.name}' is on login page — session may be expired")
            clicked = browser.click_provider_signin()
            if not clicked:
                log_warn("Could not find provider sign-in button — assuming session expired")
                return None

            # Give auth an opportunity to resolve automatically
            try:
                browser._page.wait_for_load_state("networkidle", timeout=AUTH_WAIT_TIMEOUT)
            except PlaywrightTimeoutError:
                log_warn("Auth flow did not auto-complete — may need manual intervention")
                # Still try to find the Set Cookie button
        else:
            log_info("Already authenticated — skipping sign-in")

        # Now wait for the Set Cookie button
        if not browser.wait_for_set_cookie_button():
            # Maybe the page didn't load correctly — check URL
            current = browser._page.url
            log_warn(f"Set Cookie button not found. Current URL: {current}")
            return None

        # Click Set Cookie and wait for confirmation
        if not browser.click_set_cookie():
            log_warn("Cookie was not confirmed as set")
            return None

        # Extract cookies
        cookies = browser.extract_cookies()
        if not cookies:
            log_warn("No cookies extracted")
            return None

        return cookies

    except Exception as e:
        log_error(f"Error with profile '{profile_dir.name}': {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        browser.disconnect()
        log_info(f"Chrome closed for profile '{profile_dir.name}'")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> int:
    print()
    log_info("=" * 60)
    log_info("Atlas — hCaptcha Cookie Retriever")
    log_info("=" * 60)
    print()

    # Ensure DISPLAY is set
    os.environ["DISPLAY"] = DISPLAY

    # Discover profiles
    log_info(f"Searching for profiles in: {CHROME_PROFILES_ROOT}")
    profiles = discover_profiles()

    if not profiles:
        log_error("No saved Chrome profiles found.")
        print()
        print("  Please run the profile creation utility first:")
        print("    ~/.venv/bin/python3 -m setup.google_profiles.add_captcha_account")
        print()
        return 2

    log_info(f"Found {len(profiles)} profile(s) to try")
    print()

    # Iterate through profiles
    for profile_dir, provider in profiles:
        log_info("-" * 60)
        log_info(f"Trying profile: {profile_dir.name}  (provider: {provider})")
        log_info("-" * 60)

        cookies = attempt_profile(profile_dir, provider)

        if cookies:
            log_info("✓ SUCCESS — authenticated hCaptcha session obtained")
            save_cookie_file(cookies)
            print()
            log_info("=" * 60)
            log_info("Cookie retrieval complete.")
            log_info(f"  Saved to: {COOKIE_OUTPUT_PATH}")
            log_info("=" * 60)
            return 0
        else:
            log_warn(f"Profile '{profile_dir.name}' did not yield a cookie")
            print()

    # All profiles exhausted
    print()
    log_error("=" * 60)
    log_error("All profiles exhausted — no authenticated session obtained.")
    log_error("=" * 60)
    print()
    print("  No saved Chrome profile could produce a valid hCaptcha session.")
    print("  This usually means the stored session has expired.")
    print()
    print("  To fix this, re-create an authenticated profile:")
    print("    ~/.venv/bin/python3 -m setup.google_profiles.add_captcha_account")
    print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
