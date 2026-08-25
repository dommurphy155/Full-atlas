#!/usr/bin/env python3
"""
hf_keys.py — Standalone HuggingFace signup + token extraction via patchright.

Connects to an existing Chrome CDP instance (launched by launch_cdp.sh or
main.py) on the port in /tmp/huggingface_cdp_port.txt (fallback 9333).

Flow:
    1. Connect to Chrome via CDP
    2. Navigate to https://huggingface.co/join
    3. Fill email, password, username, fullname, TOS
    4. Poll AgentMail for verification email
    5. Click verification link
    6. Create a write token, extract via clipboard (primary) or DOM (fallback)
    7. Save token to hf_keys.txt
    8. Burn the email, clean up temp profiles

Usage:
    python3 hf_keys.py <email> <password>
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import random
import glob
import shutil
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from config import (
    ROOT_DIR, DATA_DIR, KEYS_FILE, AGENTMAIL_SCRIPT, CDP_HOST,
    CDP_PORT_FILE, CDP_TIMEOUT, SIGNUP_TIMEOUT, EMAIL_POLL_TIMEOUT,
    EMAIL_POLL_INTERVAL, ELEMENT_TIMEOUT, PASSWORD, HF_JOIN_URL,
    HF_TOKEN_PREFIX, HF_CONFIRMATION_URL_PREFIX, CAPTCHA_COOKIE_FILE,
    HF_BASE, setup_logging, first_run_setup, _ensure_dirs,
)

log = setup_logging("hf_keys")


def _log(msg: str) -> None:
    log.info(msg)


def _get_cdp_url() -> str:
    """Read the CDP port from /tmp/huggingface_cdp_port.txt, fallback 9333."""
    try:
        port = CDP_PORT_FILE.read_text().strip()
        return f"http://{CDP_HOST}:{port}"
    except Exception:
        return f"http://{CDP_HOST}:9333"


def cleanup_stale_profiles() -> None:
    """Remove stale /tmp/cdp_browser_profile_* dirs."""
    for d in glob.glob("/tmp/cdp_browser_profile_*"):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)


def generate_username() -> str:
    """Generate a random HF-compatible username."""
    adjectives = [
        "swift", "crimson", "quantum", "nebula", "alpine", "cobalt",
        "ember", "frost", "glitch", "hollow", "iron", "jade", "kinetic",
        "lunar", "mira", "nova", "onyx", "paper", "quartz", "radar",
        "silver", "titan", "umbra", "vivid", "wraith", "xenon", "yarrow",
        "zephyr", "aether", "biolume", "cinder", "drift",
    ]
    nouns = [
        "falcon", "panther", "ranger", "voyager", "sentinel", "wanderer",
        "seeker", "shdw", "vertex", "nexus", "forge", "apex", "blitz",
        "cipher", "drift", "ember", "flux", "glyph", "hatch", "ivory",
        "jest", "karma", "lantern", "mist", "node", "orb", "pulse",
        "quill", "rift", "spark", "thorn", "umbra", "vault", "wisp",
    ]
    return f"{random.choice(adjectives)}-{random.choice(nouns)}-{random.randint(100, 999)}"


def generate_fullname() -> str:
    first_names = ["Alex", "Jordan", "Taylor", "Morgan", "Casey",
                   "Riley", "Avery", "Quinn", "Reese", "Devon", "Drew",
                   "Jamie", "Finley", "Parker", "Skyler", "Logan",
                   "Sawyer", "Emerson", "Harper"]
    last_names = ["Smith", "Jones", "Brown", "Davis", "Miller", "Wilson",
                  "Moore", "Taylor", "Anderson", "Thomas", "Jackson",
                  "White", "Harris", "Martin", "Thompson", "Garcia",
                  "Martinez", "Robinson", "Clark", "Lewis", "Lee",
                  "Walker", "Hall", "Allen", "Young", "King"]
    return f"{random.choice(first_names)} {random.choice(last_names)}"


class HuggingFaceSignup:
    """Main signup automation class."""

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.username = generate_username()
        self.fullname = generate_fullname()
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None

    async def connect(self) -> None:
        """Connect to existing Chrome via CDP."""
        self.playwright = await async_playwright().start()
        cdp_url = _get_cdp_url()
        self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        _log(f"Connected to CDP browser: {cdp_url}")

    async def is_captcha_cookie_valid(self) -> bool:
        if not CAPTCHA_COOKIE_FILE.exists():
            return False
        try:
            data = json.loads(CAPTCHA_COOKIE_FILE.read_text())
            cookies = data.get("cookies", []) if isinstance(data, dict) else data
            return len(cookies) > 0
        except Exception:
            return False

    async def run_signup(self) -> str:
        _log("=== Starting HuggingFace Signup ===")
        _log(f"Email: {self.email}")
        _log(f"Username: {self.username}")
        _log(f"Fullname: {self.fullname}")

        # Step 1: Navigate to join page
        _log("Navigating to join page...")
        self.page.set_default_timeout(SIGNUP_TIMEOUT)
        await self.page.goto(HF_JOIN_URL, wait_until="domcontentloaded", timeout=SIGNUP_TIMEOUT)
        await self.page.wait_for_load_state("networkidle", timeout=30000)
        _log(f"Page loaded: title='{await self.page.title()}'")

        # Step 2: Fill email
        email_selector = 'input[autocomplete="email"][name="email"]'
        await self.page.wait_for_selector(email_selector, timeout=ELEMENT_TIMEOUT)
        await self.page.locator(email_selector).first.fill(self.email)
        _log(f"Filled email: {self.email}")

        # Step 3: Click Next
        next_btn = await self._find_submit_button("Next")
        if next_btn:
            await next_btn.click()
            _log("Clicked Next")
        else:
            raise Exception("Could not find Next button")

        await asyncio.sleep(2)
        await self.page.wait_for_load_state("networkidle", timeout=30000)

        # Step 4: Fill password
        pwd_selector = 'input[autocomplete="new-password"][name="password"]'
        try:
            await self.page.wait_for_selector(pwd_selector, timeout=ELEMENT_TIMEOUT)
            await self.page.locator(pwd_selector).first.fill(self.password)
            _log("Filled password")
        except Exception:
            pwd_alt = 'input[name="password"]'
            await self.page.wait_for_selector(pwd_alt, timeout=ELEMENT_TIMEOUT)
            await self.page.locator(pwd_alt).first.fill(self.password)
            _log("Filled password (alt selector)")

        # Step 5: Fill username
        username_selector = 'input[name="username"]'
        await self.page.wait_for_selector(username_selector, timeout=ELEMENT_TIMEOUT)
        await self.page.locator(username_selector).first.fill(self.username)
        _log(f"Filled username: {self.username}")

        # Step 6: Fill fullname
        fullname_selector = 'input[name="fullname"]'
        await self.page.wait_for_selector(fullname_selector, timeout=ELEMENT_TIMEOUT)
        await self.page.locator(fullname_selector).first.fill(self.fullname)
        _log(f"Filled fullname: {self.fullname}")

        # Step 7: Accept TOS
        tos_checkboxes = await self.page.locator('input[type="checkbox"]').all()
        for cb in tos_checkboxes:
            if not await cb.is_checked():
                await cb.check()
                _log("Checked TOS checkbox")
                break

        # Step 8: Click Create Account
        create_btn = await self._find_create_button()
        if create_btn:
            await create_btn.click()
            _log("Clicked Create Account")
        else:
            raise Exception("Could not find Create Account button")

        await asyncio.sleep(2)
        _log("Form submitted, waiting for confirmation...")

        # Step 9: Poll for verification email
        _log("Polling for verification email...")
        verification_url = await self.poll_verification_email()
        if not verification_url:
            raise Exception("No verification email received")

        _log(f"Got verification URL: {verification_url}")
        await self.page.goto(verification_url, wait_until="domcontentloaded", timeout=SIGNUP_TIMEOUT)
        await asyncio.sleep(3)

        if "huggingface.co" in self.page.url and "email_confirmation" not in self.page.url:
            _log("Confirmed! Now on HuggingFace main site")
        else:
            _log(f"Still on: {self.page.url}")
            await self.page.goto(f"{HF_BASE}/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

        # Step 10: Navigate to token creation page
        _log("Navigating to token creation page...")
        token_url = f"{HF_BASE}/settings/tokens/new?tokenType=write"
        await self.page.goto(token_url, wait_until="domcontentloaded", timeout=SIGNUP_TIMEOUT)
        await self.page.wait_for_load_state("networkidle", timeout=30000)
        _log(f"Token page loaded: {self.page.url}")

        if "/login" in self.page.url:
            raise Exception("Redirected to login — account not confirmed")

        # Step 11: Fill token name
        token_name_selector = 'input[name="displayName"]'
        await self.page.wait_for_selector(token_name_selector, timeout=ELEMENT_TIMEOUT)
        token_name = f"token-{self.username}"
        await self.page.locator(token_name_selector).first.fill(token_name)
        _log(f"Filled token name: {token_name}")

        # Step 12: Click Create token
        create_token_btn = self.page.locator('button:has-text("Create token"), button:has-text("Create Token")')
        if await create_token_btn.count() == 0:
            create_token_btn = self.page.locator('button[type="submit"]').last
        await create_token_btn.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
        await create_token_btn.click()
        _log("Clicked Create token")

        # Step 13: Extract token
        api_key = await self._extract_token()
        if not api_key or not api_key.startswith(HF_TOKEN_PREFIX):
            raise Exception(f"Failed to extract HF token. URL: {self.page.url}")

        masked = api_key[:4] + "***" + api_key[-4:] if len(api_key) > 14 else api_key[:4] + "***"
        _log(f"Extracted token: {masked} (len={len(api_key)})")

        # Step 14: Save token
        KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with KEYS_FILE.open("a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        os.chmod(KEYS_FILE, 0o600)
        _log(f"Saved token to {KEYS_FILE}")

        return api_key

    async def _find_submit_button(self, text: str):
        """Find a submit button containing *text*."""
        btns = await self.page.locator('button[type="submit"]').all()
        for btn in btns:
            try:
                btn_text = (await btn.inner_text()).lower()
                if text.lower() in btn_text:
                    return btn
            except Exception:
                pass
        return None

    async def _find_create_button(self):
        """Find the Create Account button."""
        selectors = [
            'button.btn[type="submit"]:has-text("Create account")',
            'button.btn[type="submit"]:has-text("Create Account")',
            'button[type="submit"]:has-text("Create")',
        ]
        for sel in selectors:
            try:
                locator = self.page.locator(sel)
                if await locator.count() > 0:
                    return locator.first
            except Exception:
                continue
        # Fallback
        submit_btns = await self.page.locator('button[type="submit"]').all()
        for btn in submit_btns:
            btn_text = await btn.inner_text()
            if "create" in btn_text.lower() or "sign" in btn_text.lower():
                return btn
        return None

    async def _extract_token(self) -> str:
        """Extract HF token: clipboard first, DOM fallback."""
        await asyncio.sleep(2)

        # Clipboard (primary)
        clipboard_value = ""
        try:
            clipboard_value = await self.page.evaluate(
                "() => navigator.clipboard.readText().catch(() => '')"
            )
            clipboard_value = (clipboard_value or "").strip()
        except Exception:
            pass

        if re.fullmatch(r"hf_[a-zA-Z0-9_-]{20,}", clipboard_value):
            return clipboard_value

        # DOM fallback
        _log("Clipboard did not return a valid HF token; trying DOM fallback")
        dom_value = await self.page.evaluate("""
            () => {
                const TOKEN_RE = /hf_[a-zA-Z0-9_-]{20,}/;
                const candidates = [];
                for (const el of document.querySelectorAll("code, pre")) {
                    candidates.push(el.textContent || "");
                }
                for (const el of document.querySelectorAll("input, textarea")) {
                    candidates.push(el.value || "");
                    candidates.push(el.getAttribute("value") || "");
                }
                for (const el of document.querySelectorAll('[class*="token"], [class*="key"], [class*="code"], .alert')) {
                    candidates.push(el.textContent || "");
                }
                try { candidates.push(document.body.innerText || ""); } catch (_) {}
                for (const value of candidates) {
                    const match = String(value).match(TOKEN_RE);
                    if (match) return match[0];
                }
                return "";
            }
        """)
        return (dom_value or "").strip()

    async def poll_verification_email(self, timeout: int = EMAIL_POLL_TIMEOUT) -> Optional[str]:
        """Poll AgentMail for HF verification URL."""
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            try:
                result = subprocess.run(
                    [sys.executable, str(AGENTMAIL_SCRIPT), "check"],
                    capture_output=True, text=True, timeout=180
                )
                if result.returncode == 0 and result.stdout.strip():
                    url = result.stdout.strip()
                    if HF_CONFIRMATION_URL_PREFIX in url or "huggingface.co" in url:
                        return url
            except Exception as e:
                _log(f"Poll error: {e}")
            await asyncio.sleep(EMAIL_POLL_INTERVAL)
        return None

    async def close(self) -> None:
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass


async def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: hf_keys.py <email> <password>")
        sys.exit(1)

    email = sys.argv[1]
    password = sys.argv[2]

    signup = HuggingFaceSignup(email, password)
    try:
        await signup.connect()
        api_key = await signup.run_signup()
        masked = api_key[:4] + "***" + api_key[-4:] if len(api_key) > 14 else api_key[:4] + "***"
        _log(f"SUCCESS: {masked}")
    except Exception as e:
        _log(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        await signup.close()
        cleanup_stale_profiles()
        # Burn email
        subprocess.run([sys.executable, str(AGENTMAIL_SCRIPT), "burn"],
                       capture_output=True)


if __name__ == "__main__":
    asyncio.run(main())
