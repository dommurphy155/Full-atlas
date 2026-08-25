#!/usr/bin/env python3
"""
OpenRouter Signup Automation via Patchright (Playwright-compatible).
Connects to existing Chrome CDP on a dynamically-assigned port.
Linux-only, portable paths from config.py.
"""
import asyncio
import os
import re
import sys
import subprocess
import time
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Import config first to set up paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    PROJECT_ROOT, DATA_DIR, RUN_DIR, KEYS_FILE, OPEN_EMAIL_SCRIPT, CDP_HOST, CDP_PORTS,
    EMAIL_POLL_TIMEOUT, LOG_DIR, CDP_PORT_FILE, ensure_dirs,
)

# Setup logging — flush immediately for real-time output
_logger = logging.getLogger("signup_automation")
_logger.setLevel(logging.INFO)
_fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ensure_dirs()
_fh = RotatingFileHandler(str(LOG_DIR / "signup.log"), maxBytes=5*1024*1024, backupCount=3)
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
_sh.setLevel(logging.INFO)
_logger.addHandler(_fh)
_logger.addHandler(_sh)
_logger.propagate = False
logger = _logger

# Lazy import — patchright is installed during bootstrap by main.py
async_playwright = None


async def _ensure_playwright():
    """Import patchright lazily at runtime, not at module load."""
    global async_playwright
    if async_playwright is None:
        try:
            from patchright.async_api import async_playwright as _pw
            async_playwright = _pw
        except ImportError:
            # Try pip, then uv as fallback
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "--break-system-packages", "patchright"])
            except Exception:
                try:
                    subprocess.check_call([sys.executable, "-m", "pip", "install", "patchright"])
                except Exception:
                    subprocess.check_call(["uv", "pip", "install", "--python", sys.executable, "patchright"])
            subprocess.check_call([sys.executable, "-m", "patchright", "install", "chromium"])
            from patchright.async_api import async_playwright as _pw
            async_playwright = _pw
    return async_playwright


def _get_cdp_url(timeout: int = 30) -> str:
    """Read CDP port from project-local port file or env var."""
    port_file_str = os.environ.get("CDP_PORT_FILE", str(CDP_PORT_FILE))
    deadline = time.time() + timeout
    while time.time() < deadline:
        port_file = Path(port_file_str)
        if port_file.exists():
            port = port_file.read_text().strip()
            if port:
                return f"http://{CDP_HOST}:{int(port)}"
        # Also check env var set by main.py
        env_port = os.environ.get("CDP_PORT")
        if env_port:
            return f"http://{CDP_HOST}:{int(env_port)}"
        time.sleep(0.1)
    # Fallback: try env var
    env_port = os.environ.get("CDP_PORT")
    if env_port:
        return f"http://{CDP_HOST}:{int(env_port)}"
    # Last resort: default ports
    url = f"http://{CDP_HOST}:{CDP_PORTS[0]}"
    logger.warning(f"CDP port not found after {timeout}s, falling back to {url}")
    return url


class OpenRouterSignup:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None

    async def connect(self):
        """Connect to existing Chrome via CDP"""
        cdp_url = _get_cdp_url()
        pw = await _ensure_playwright()
        self.playwright = await pw().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        try:
            await self.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://openrouter.ai")
        except Exception:
            pass
        logger.info(f"Connected to CDP browser, page ready")

    async def handle_cloudflare(self, continue_selector: str, email: str, email_selectors: list,
                                  password: str, pwd_selectors: list, legal_selector: str,
                                  max_retries: int = 5):
        """Poll for the Turnstile iframe, click it, retry Continue, and as a last
        resort refresh the page and redo the form + Turnstile from scratch."""
        logger.info("Handling Cloudflare Turnstile...")

        async def try_click_checkbox() -> bool:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    for frame in self.page.frames:
                        if "challenges.cloudflare.com" not in (frame.url or ""):
                            continue
                        cb = await frame.query_selector("input[type='checkbox']")
                        if cb:
                            await cb.click()
                            logger.info("Clicked Turnstile checkbox")
                            return True
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            return False

        clicked = await try_click_checkbox()
        if not clicked:
            logger.info("Turnstile checkbox not found — may auto-solve")

        await asyncio.sleep(2)

        # Still stuck on signup? Try re-clicking Continue once.
        if "sign-up" in self.page.url and "verify" not in self.page.url:
            try:
                await self.page.locator(continue_selector).first.click(timeout=5000)
                logger.info("Re-clicked Continue after Turnstile")
                await asyncio.sleep(2)
            except Exception:
                pass

        # Still stuck? Refresh the page and redo email/password/legal/Continue/Turnstile once.
        if "sign-up" in self.page.url and "verify" not in self.page.url:
            logger.info("Still stuck on signup page — refreshing and retrying...")
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)

                await self._fill_form_field(email_selectors, email, "email")
                await self._fill_form_field(pwd_selectors, password, "password")
                await self.page.wait_for_selector(legal_selector, timeout=10000)
                await self.page.locator(legal_selector).first.check()
                await self.page.locator(continue_selector).first.click(timeout=10000)
                logger.info("Refilled form and re-clicked Continue after refresh")
                await asyncio.sleep(2)

                clicked = await try_click_checkbox()
                if clicked:
                    logger.info("Clicked Turnstile checkbox after refresh")
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"Refresh-and-retry failed: {str(e)[:80]}")

        return True

    async def _fill_form_field(self, selectors: list, value: str, field_name: str):
        """Fill a form field with multiple fallback selectors."""
        last_error = None
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                await locator.wait_for(state="visible", timeout=10000)
                await locator.fill(value)
                logger.info(f"Filled {field_name}")
                return
            except Exception as e:
                last_error = e
                continue
        # All selectors failed — save debug info
        await self.page.screenshot(path=str(DATA_DIR / f"debug_{field_name}.png"))
        html = await self.page.content()
        Path(DATA_DIR / f"debug_{field_name}.html").write_text(html, encoding="utf-8")
        raise Exception(f"Could not fill {field_name}. Last error: {last_error}")

    async def run_signup(self, email: str, password: str):
        """Execute full signup flow"""
        logger.info("=== Starting OpenRouter Signup ===")

        # Step 1: Navigate to signup, retry if form doesn't appear
        email_selectors = [
            "input[id='emailAddress-field']",
            "input[name='emailAddress']",
            "input[type='email']",
        ]
        for attempt in range(3):
            try:
                await self.page.goto("https://openrouter.ai/sign-up",
                                     wait_until="networkidle", timeout=30000)
            except Exception:
                # domcontentloaded fallback if networkidle times out (Cloudflare)
                await self.page.goto("https://openrouter.ai/sign-up",
                                     wait_until="domcontentloaded", timeout=30000)

            try:
                await self.page.wait_for_selector(email_selectors[0], timeout=15000)
                break
            except Exception:
                if attempt == 0:
                    logger.warning("Sign-up form not found, clearing cookies and retrying...")
                    await self.context.clear_cookies()
                    await self.page.reload(wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(3)
                elif attempt == 1:
                    logger.warning("Still no form — taking screenshot for debugging...")
                    await self.page.screenshot(path=str(DATA_DIR / "signup_form_missing.png"))
                    await self.context.clear_cookies()
                    await asyncio.sleep(3)
                else:
                    raise Exception("Sign-up form not found after 3 retries — Cloudflare may be blocking")
        logger.info("Navigated to signup page")

        # Step 2: Fill email
        await self._fill_form_field(email_selectors, email, "email")

        # Step 3: Fill password
        pwd_selectors = [
            "input[id='password-field']",
            "input[name='password']",
            "input[type='password']",
        ]
        await self._fill_form_field(pwd_selectors, password, "password")

        # Step 4: Click legal checkbox
        legal_selector = "input[id='legalAccepted-field'], input[name='legalAccepted'], input[type='checkbox'][required]"
        await self.page.wait_for_selector(legal_selector, timeout=10000)
        await self.page.locator(legal_selector).first.check()
        logger.info("Checked legal acceptance")

        # Step 5: Click Continue button
        continue_selector = "button.cl-formButtonPrimary:has(span.cl-internal-2iusy0:has-text('Continue'))"
        await self.page.wait_for_selector(continue_selector, timeout=10000)
        await self.page.locator(continue_selector).first.click()
        logger.info("Clicked Continue")

        # Step 6: Handle Cloudflare challenge — retries with form re-fill + re-click Continue
        cf_passed = await self.handle_cloudflare(
            continue_selector=continue_selector,
            email=email,
            email_selectors=email_selectors,
            password=password,
            pwd_selectors=pwd_selectors,
            legal_selector=legal_selector,
        )
        if not cf_passed:
            logger.warning("Cloudflare not fully solved — but will try to continue")

        # Brief wait for Cloudflare redirect to process
        await asyncio.sleep(2)
        logger.info(f"Post-submit page URL: {self.page.url}")

        # Step 7: Poll for verification email
        logger.info("Polling for verification email...")
        verification_url = await self.poll_verification_email()
        if not verification_url:
            raise Exception("No verification email received")

        logger.info(f"Got verification URL: {verification_url[:60]}...")

        # Step 8: Navigate to verification URL then poll until on openrouter.ai root
        logger.info(f"Navigating to verification URL: {verification_url[:60]}...")
        await self.page.goto(verification_url, wait_until="domcontentloaded", timeout=60000)

        # Poll current URL — clerk may redirect via JS with no nav event
        for _ in range(20):
            current = self.page.url
            if "openrouter.ai" in current and "verify" not in current and "sign-up" not in current:
                break
            await asyncio.sleep(1)
        else:
            # Stuck on verify page — navigate directly
            logger.warning(f"Stuck on {self.page.url}, navigating directly to openrouter.ai...")
            await self.page.goto("https://openrouter.ai/", wait_until="domcontentloaded", timeout=30000)

        await asyncio.sleep(3)
        logger.info(f"Redirected to: {self.page.url}")

        # Step 9: Click Next on onboarding modal — poll across the settle period,
        # since the modal can appear late after the post-verify redirect.
        next_found = False
        selectors = [
            "button:has-text('Next')",
            "button:has(span:has-text('Next'))",
            "[role='button']:has-text('Next')",
        ]
        deadline = time.time() + 15
        while time.time() < deadline and not next_found:
            for selector in selectors:
                try:
                    next_btn = self.page.locator(selector).first
                    if await next_btn.is_visible(timeout=1000):
                        await next_btn.scroll_into_view_if_needed()
                        await asyncio.sleep(0.3)
                        await next_btn.evaluate("el => el.click()")
                        logger.info("✓ Clicked Next on onboarding")
                        next_found = True
                        await asyncio.sleep(2)
                        break
                except Exception:
                    continue
            if not next_found:
                await asyncio.sleep(1)

        if not next_found:
            logger.info("No Next button found within 15s (onboarding may be skipped)")

        # Step 10: JS-click copy button
        copy_selector = "button[aria-label='Copy API key to clipboard'], button:has(svg.lucide-copy)"
        await self.page.wait_for_selector(copy_selector, timeout=30000)
        await self.page.locator(copy_selector).first.evaluate("el => el.click()")
        logger.info("Clicked Copy API key")
        await asyncio.sleep(1)

# Step 11: Extract API key — try clipboard first (Copy button was
        # already clicked, and the DOM only ever shows the masked version)
        try:
            api_key = await self._extract_api_key()
        except Exception:
            await self.dump_page_state("key_extraction_failure")
            raise

        # Step 12: Save API key (append, don't overwrite)
        KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with KEYS_FILE.open("a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        os.chmod(KEYS_FILE, 0o600)
        logger.info(f"Saved API key to {KEYS_FILE}")

        return api_key

    async def _extract_api_key(self) -> str:
        """Extract the API key: clipboard first, DOM as fallback."""
        api_key = ""
        try:
            clip_val = await self.page.evaluate("navigator.clipboard.readText()")
            if clip_val and re.match(r'^sk-or-[a-zA-Z0-9_-]{40,}$', clip_val) and '\u2022' not in clip_val:
                api_key = clip_val
        except Exception:
            api_key = ""

        if api_key:
            logger.info("Got API key from clipboard")
        else:
            logger.info("Clipboard empty/invalid — falling back to DOM search")
            api_key = await self.page.evaluate(r"""() => {
            // Look for key in common locations
            const patterns = [
                () => [...document.querySelectorAll('code, pre')].map(el => el.textContent.trim()).find(t => t.startsWith('sk-or-')),
                () => [...document.querySelectorAll('input')].map(el => el.value.trim()).find(v => v.startsWith('sk-or-')),
                () => [...document.querySelectorAll('[class*="key"], [class*="token"], [class*="api"]')].map(el => el.textContent.trim()).find(t => t.startsWith('sk-or-')),
                () => [...document.querySelectorAll('.truncate')].map(el => el.textContent.trim()).find(t => t.startsWith('sk-or-')),
                () => (document.body.innerText.match(/sk-or-v1-[a-zA-Z0-9_-]{50,}/) || [])[0],
                // Bearer pattern: 'Bearer sk-or-v1-xxx'
                () => {
                    const bearer = document.body.innerText.match(/Bearer\s+(sk-or-v1-[a-zA-Z0-9_-]{50,})/i);
                    return bearer ? bearer[1] : null;
                },
            ];
            for (const fn of patterns) {
                try { const v = fn(); if (v) return v; } catch {}
            }
            return '';
        }""")

        if not api_key or '\u2022' in api_key or not re.match(r'^sk-or-v1-[a-zA-Z0-9_-]{50,120}$', api_key):
            raise Exception(f"Failed to extract valid API key from DOM. Got: {api_key[:30] if api_key else 'None'}. Page URL: {self.page.url}")

        logger.info(f"Extracted API key: {api_key[:20]}...")
        return api_key

    async def poll_verification_email(self, timeout: int = EMAIL_POLL_TIMEOUT) -> Optional[str]:
        """Poll OpenMail subprocess output for verification URL."""
        start_time = time.time()
        iteration = 0

        while time.time() - start_time < timeout:
            iteration += 1

            try:
                remaining = max(1, int(timeout - (time.time() - start_time)))
                check_timeout = min(60, remaining)

                logger.info(
                    f"Checking OpenMail inbox "
                    f"(poll {iteration}, timeout={check_timeout}s)..."
                )

                result = subprocess.run(
                    [
                        sys.executable,
                        str(OPEN_EMAIL_SCRIPT),
                        "check",
                        str(check_timeout),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=check_timeout + 10,
                    cwd=str(PROJECT_ROOT),
                )

                stdout = result.stdout or ""
                stderr = result.stderr or ""

                for line in stderr.splitlines():
                    if "Received email" in line:
                        logger.info("Verification email received")

                if result.returncode != 0:
                    logger.warning(
                        f"OpenMail check exited with code {result.returncode}"
                    )

                # Extract verification URL from stdout
                for line in stdout.strip().splitlines():
                    line = line.strip()
                    if line.startswith("https://") and "openrouter.ai" in line:
                        logger.info(f"Found verification URL in stdout")
                        return line

                elapsed = int(time.time() - start_time)
                logger.info(
                    f"OpenMail debug poll complete "
                    f"({elapsed}s/{timeout}s)"
                )

            except subprocess.TimeoutExpired:
                logger.warning("OpenMail polling subprocess timed out")

            except Exception as e:
                logger.warning(
                    f"OpenMail polling error: "
                    f"{type(e).__name__}: {e}"
                )

            await asyncio.sleep(2)

        return None

    async def dump_page_state(self, label: str):
        """Debug: dump page HTML and take screenshot"""
        try:
            html = await self.page.content()
            Path(DATA_DIR / f"debug_{label}.html").write_text(html, encoding="utf-8")
            await self.page.screenshot(path=str(DATA_DIR / f"debug_{label}.png"))
            logger.info(f"Dumped page state: {label}")
        except Exception as e:
            logger.warning(f"Could not dump page state {label}: {e}")

    async def close(self):
        if self.browser:
            await self.browser.close()
        if hasattr(self, 'playwright'):
            await self.playwright.stop()


def _validate_email(email: str) -> None:
    """Basic email format validation."""
    if not email:
        raise ValueError("Email is required")
    if not re.match(r'^[a-zA-Z0-9._%+-_]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        raise ValueError("Invalid email format")


def _validate_password(password: str) -> None:
    """Basic password strength validation."""
    if not password:
        raise ValueError("Password is required")
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    if not any(c.isdigit() for c in password):
        raise ValueError("Password must contain at least one digit")


async def main():
    if len(sys.argv) < 2:
        print("Usage: signup_automation.py <email>  (password read from stdin)", file=sys.stderr)
        sys.exit(1)

    email = sys.argv[1]
    password = sys.stdin.read().strip()

    _validate_email(email)
    _validate_password(password)

    logger.info(f"Email: {email}")

    signup = OpenRouterSignup()
    try:
        await signup.connect()
        api_key = await signup.run_signup(email, password)
        logger.info(f"SUCCESS: {api_key[:20]}...")
    except Exception as e:
        logger.error(f"FAILED: {e}")
        raise
    finally:
        await signup.close()
        # Burn email
        subprocess.run([sys.executable, str(OPEN_EMAIL_SCRIPT), "burn"], capture_output=True, cwd=str(PROJECT_ROOT))


if __name__ == "__main__":
    asyncio.run(main())

# Add this debug method to the OpenRouterSignup class (around line 90, after handle_cloudflare)