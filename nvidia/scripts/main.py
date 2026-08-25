#!/usr/bin/env python3
"""
Main orchestrator for NVIDIA signup automation.

Flow:
  1. Launch CDP via launch_cdp.sh on a random port, connects to display:1
  2. Check for valid hCaptcha cookie in ~/atlas/data/captcha_cookie.json
     — if valid, inject into browser; if not, run get_cookie.py first
  3. Burn all existing AgentMail inboxes, create a fresh email
  4. Navigate to https://build.nvidia.com/explore/discover?modal=signin
  5. Fill email → Next → password (×2) → hCaptcha → Create Account
  6. Poll AgentMail for 6-digit verification code
     — if no email in 4s, retry full flow once more
     — if still no email, run get_cookie.py for fresh cookie, try again
     — if 3rd attempt fails, exit
  7. Fill code → Continue → Submit
  8. Fill org name → Create NVIDIA Cloud Account
  9. Navigate to API keys page → Generate key → Extract nvapi- key
  10. Save to ~/atlas/data/nvidia_data/nvda_keys.txt
  11. Exit cleanly

Usage:
    python3 main.py
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    ROOT_DIR, DATA_DIR, KEYS_FILE, AGENTMAIL_SCRIPT, GET_COOKIE_SCRIPT,
    LAUNCH_CDP_SCRIPT, HF_KEYS_SCRIPT, CDP_HOST, CDP_PORT_FILE, CDP_TIMEOUT,
    CAPTCHA_COOKIE_FILE, PASSWORD, NVIDIA_TOKEN_PREFIX,
    AGENTMAIL_API_KEY, AGENTMAIL_BASE, EMAIL_POLL_INTERVAL,
)

try:
    from patchright.async_api import async_playwright
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "patchright"])
    from patchright.async_api import async_playwright

try:
    import httpx
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx"])
    import httpx


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def find_free_port() -> int:
    """Let the OS pick a free port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def wait_for_cdp(port: int, timeout: int = CDP_TIMEOUT) -> bool:
    """Poll the CDP endpoint until it responds."""
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
    """Load hCaptcha cookies from the shared cookie file."""
    if not CAPTCHA_COOKIE_FILE.exists():
        return None
    try:
        data = json.loads(CAPTCHA_COOKIE_FILE.read_text())
        # get_cookie.py writes {"cookies": [...]}, but tolerate a bare list
        if isinstance(data, dict):
            cookies = data.get("cookies", [])
        else:
            cookies = data
        return cookies if cookies else None
    except Exception:
        return None


async def inject_cookie(page, cookie: dict):
    """Inject a single cookie into the browser context."""
    cookie_dict = {
        "name": cookie.get("name"),
        "value": cookie.get("value"),
        "domain": cookie.get("domain", ".build.nvidia.com"),
        "path": cookie.get("path", "/"),
        "httpOnly": cookie.get("httpOnly", False),
        "secure": cookie.get("secure", True),
        "sameSite": cookie.get("sameSite", "Lax"),
    }
    expiry = cookie.get("expires")
    if expiry and expiry > 0:
        cookie_dict["expires"] = expiry
    await page.context.add_cookies([cookie_dict])


def run_get_cookie() -> bool:
    """Run get_cookie.py to obtain a fresh hCaptcha cookie."""
    log("Running get_cookie.py to obtain fresh hCaptcha cookie...")
    result = subprocess.run(
        [sys.executable, str(GET_COOKIE_SCRIPT)],
        capture_output=False,
        timeout=30,
    )
    return result.returncode == 0 and CAPTCHA_COOKIE_FILE.exists()


def create_agentmail_email() -> Optional[str]:
    """Burn all inboxes, create a fresh one, return the email address."""
    log("Burning all existing AgentMail inboxes...")
    subprocess.run(
        [sys.executable, str(AGENTMAIL_SCRIPT), "burn"],
        capture_output=True, timeout=60,
    )
    log("Creating fresh AgentMail inbox...")
    result = subprocess.run(
        [sys.executable, str(AGENTMAIL_SCRIPT), "create"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        log(f"Failed to create email: {result.stderr}")
        return None
    email = result.stdout.strip()
    if not email:
        log("Empty email address returned")
        return None
    log(f"Created email: {email}")
    return email


def poll_verification_code_fast(timeout: int = 4) -> Optional[str]:
    """Quick poll (default 4s) for NVIDIA 6-digit verification code."""
    return poll_verification_code(timeout)


def poll_verification_code(timeout: int = 4) -> Optional[str]:
    """Poll AgentMail inbox for NVIDIA 6-digit verification code."""
    state_file = DATA_DIR / ".agentmail_state.json"
    if not state_file.exists():
        return None

    state = json.loads(state_file.read_text())
    inbox_id = state.get("inbox_id")
    if not inbox_id:
        return None

    api_key = os.getenv("AGENTMAIL_API_KEY")
    if not api_key:
        return None

    headers = {"Authorization": f"Bearer {api_key}"}
    start = time.time()

    with httpx.Client() as client:
        while time.time() - start < timeout:
            try:
                r = client.get(
                    f"{AGENTMAIL_BASE}/inboxes/{inbox_id}/messages",
                    headers=headers, timeout=30
                )
                data = r.json()
                msgs = data if isinstance(data, list) else data.get("messages", [])
                for msg in msgs:
                    msg_id = (msg.get("id") or msg.get("message_id")
                              or msg.get("uid") or msg.get("messageId"))
                    if not msg_id:
                        continue

                    mr = client.get(
                        f"{AGENTMAIL_BASE}/inboxes/{inbox_id}/messages/{msg_id}",
                        headers=headers, timeout=30
                    )
                    body = mr.json().get("body", "") or mr.json().get("html", "") or str(mr.json())

                    # Look for 6-digit code: 292-752 or 292752
                    patterns = [r'(\d{3}-\d{3})', r'(\d{6})']
                    for pattern in patterns:
                        m = re.search(pattern, body)
                        if m:
                            code = m.group(1)
                            code_clean = code.replace("-", "")
                            if len(code_clean) == 6:
                                log(f"Found verification code: {code}")
                                return code
            except Exception as e:
                log(f"Poll error: {e}")
            time.sleep(EMAIL_POLL_INTERVAL)
    return None


async def launch_cdp_and_fill_cookie() -> tuple:
    """
    Launch CDP Chrome via launch_cdp.sh on a random port, then inject
    hCaptcha cookies into the browser context.

    Returns (playwright, browser, context, page, port, cdp_proc).
    """
    import re  # noqa: F811
    port = find_free_port()
    log(f"Launching CDP on port {port} (display :1)...")

    # Launch CDP in background with CDP_PORT env var so launch_cdp.sh uses our port
    cdp_proc = subprocess.Popen(
        ["bash", str(LAUNCH_CDP_SCRIPT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "DISPLAY": ":1", "CDP_PORT": str(port)},
        start_new_session=True,
    )

    # Write the port to the file that hf_keys.py reads
    CDP_PORT_FILE.write_text(str(port), encoding="utf-8")

    # Wait for CDP to be ready
    if not wait_for_cdp(port, timeout=CDP_TIMEOUT):
        log("CDP failed to start!")
        cdp_proc.kill()
        raise RuntimeError("CDP did not become ready")

    log(f"CDP ready on port {port}")

    # Connect via Playwright CDP
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(f"http://{CDP_HOST}:{port}")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()

    # Inject hCaptcha cookies if available
    cookies = load_captcha_cookie()
    if cookies:
        log(f"Injecting {len(cookies)} hCaptcha cookie(s) into browser...")
        # Navigate to nvidia.com first so the context has a domain
        await page.goto("https://build.nvidia.com", wait_until="domcontentloaded", timeout=30000)
        for cookie in cookies:
            try:
                await inject_cookie(page, cookie)
                log(f"  Injected: {cookie.get('name')}")
            except Exception as e:
                log(f"  Warning: could not inject cookie {cookie.get('name')}: {e}")
    else:
        log("No valid captcha cookie found — will run get_cookie.py if needed")

    return pw, browser, context, page, port, cdp_proc


async def run_full_signup(email: str, password: str) -> Optional[str]:
    """
    Run the complete NVIDIA signup flow.

    Returns the nvapi- key on success, None on failure.
    """
    pw, browser, context, page, port, cdp_proc = await launch_cdp_and_fill_cookie()
    
    try:
        log("Navigating to NVIDIA login page...")
        page.set_default_timeout(60000)
        # Reload with modal URL — cookie injection navigates to build.nvidia.com which may close modals
        await page.goto("https://build.nvidia.com/explore/discover?modal=signin", 
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        log(f"Page loaded: title='{await page.title()}'")

        import random
        org_name = f"{random.choice(['Nexus','Quantum','Apex','Vertex','Stellar'])} {random.choice(['Labs','Systems','Studios','Works','Collective'])}"
        log(f"Org name: {org_name}")

        # Step 1: Fill email directly in the modal
        log("Looking for email input...")
        email_filled = False
        for sel in ['input[name="email"]', 'input[type="email"]']:
            try:
                await page.wait_for_selector(sel, timeout=15000)
                await page.locator(sel).first.fill(email)
                email_filled = True
                log(f"Filled email: {email}")
                break
            except Exception:
                continue
        if not email_filled:
            raise Exception("Email input not found")

        # Step 3: Click Next
        log("Looking for Next button...")
        next_btns = await page.locator('button:has-text("Next")').all()
        clicked_next = False
        for btn in next_btns:
            txt = await btn.inner_text()
            if "Next" in txt:
                await btn.click(force=True)
                log("Clicked Next")
                clicked_next = True
                break
        if not clicked_next:
            raise Exception("Next button not found")

        await asyncio.sleep(3)
        await page.wait_for_load_state("networkidle", timeout=30000)

        # Step 4: Fill password
        log("Looking for password input...")
        pwd_filled = False
        for sel in ['input[name="password"]', 'input[id="registration_password"]', 'input[formcontrolname="password"]', 'input[type="password"]']:
            try:
                await page.wait_for_selector(sel, timeout=15000)
                await page.locator(sel).first.fill(password)
                pwd_filled = True
                log("Filled password")
                break
            except Exception:
                continue
        if not pwd_filled:
            raise Exception("Password input not found")

        # Step 5: Fill confirm password
        for sel in ['input[name="passwordConfirm"]', 'input[id="registration_passwordConfirm"]', 'input[formcontrolname="confirmPassword"]']:
            try:
                await page.wait_for_selector(sel, timeout=10000)
                await page.locator(sel).first.fill(password)
                log("Filled confirm password")
                break
            except Exception:
                continue

        # Step 6: Click Next or Create Account
        await asyncio.sleep(2)
        create_clicked = False
        for btn_text in ["Create Account", "Next"]:
            btns = await page.locator(f'button:has-text("{btn_text}")').all()
            for btn in btns:
                try:
                    await btn.click(force=True)
                    log(f"Clicked {btn_text}")
                    create_clicked = True
                    break
                except Exception:
                    continue
            if create_clicked:
                break
        if not create_clicked:
            raise Exception("Create Account / Next button not found")

        log("Waiting for verification code email...")
        await asyncio.sleep(5)

        # Step 7: Poll for 6-digit code
        code = poll_verification_code_fast(timeout=4)
        if not code:
            log("No email in 4s — doing longer poll (120s)...")
            code = poll_verification_code(timeout=120)
        if not code:
            log("No verification code received!")
            return None

        log(f"Got verification code: {code}")

        # Step 8: Fill verification code
        code_inputs = await page.locator('input[inputmode="numeric"]').all()
        if len(code_inputs) >= 6:
            for i, char in enumerate(code):
                if i < len(code_inputs):
                    await code_inputs[i].fill(char)
            log(f"Filled code into individual inputs: {code}")
        else:
            code_input = page.locator('input[placeholder*="code"], input[aria-label*="code"]')
            if await code_input.count() > 0:
                await code_input.first.fill(code)
                log(f"Filled code: {code}")
            else:
                await page.evaluate(f"""
                    () => {{
                        const inputs = document.querySelectorAll('input');
                        const code = '{code}';
                        inputs.forEach((el, i) => {{
                            if (i < code.length) el.value = code[i];
                        }});
                    }}
                """)
                log(f"Filled code via JS: {code}")

        await asyncio.sleep(1)

        # Step 9: Click Continue
        log("Clicking Continue...")
        continue_btn = page.locator('button:has-text("Continue"), span:has-text("Continue")')
        if await continue_btn.count() > 0:
            await continue_btn.first.click(force=True)
            log("Clicked Continue")
        else:
            all_btns = await page.locator('button').all()
            for btn in all_btns:
                btn_text = await btn.inner_text()
                if "continue" in btn_text.lower():
                    await btn.click(force=True)
                    log("Clicked Continue (fallback)")
                    break

        await asyncio.sleep(5)

        # Step 10: Click Submit
        log("Clicking Submit...")
        submit_btn = page.locator('button:has-text("Submit"), button[type="submit"]')
        if await submit_btn.count() > 0:
            await submit_btn.first.click(force=True)
            log("Clicked Submit")
        await asyncio.sleep(2)

        # Step 11: Fill org name
        log("Looking for organization name field...")
        org_selectors = [
            'input[data-testid="kui-text-input-element"]',
            'input[placeholder*="Organization"]',
            'input[name="name"]',
            'input[type="text"]',
        ]
        org_filled = False
        for sel in org_selectors:
            try:
                inputs = await page.locator(sel).all()
                for inp in inputs:
                    is_visible = await inp.is_visible()
                    if is_visible:
                        await inp.fill(org_name)
                        log(f"Filled org name: {org_name}")
                        org_filled = True
                        break
                if org_filled:
                    break
            except Exception:
                continue

        # Step 12: Click Create NVIDIA Cloud Account
        log("Clicking Create NVIDIA Cloud Account...")
        create_nvc_btn = page.locator('button:has-text("Create NVIDIA Cloud Account")')
        if await create_nvc_btn.count() > 0:
            await create_nvc_btn.first.click(force=True)
            log("Clicked Create NVIDIA Cloud Account")
        else:
            all_btns = await page.locator('button').all()
            for btn in all_btns:
                btn_text = await btn.inner_text()
                if "create" in btn_text.lower():
                    await btn.click(force=True)
                    log(f"Clicked create button (fallback): {btn_text.strip()}")
                    break

        await asyncio.sleep(8)
        log(f"Current URL: {page.url}")

        # Step 13: Navigate to API keys page
        log("Navigating to API keys page...")
        await page.goto("https://build.nvidia.com/settings/api-keys", 
                       wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        log(f"API keys page: {page.url}")

        # Step 14: Click Generate API Key
        log("Looking for Generate API Key button...")
        gen_btn = page.locator('button:has-text("Generate API Key")')
        if await gen_btn.count() > 0:
            await gen_btn.first.click(force=True)
            log("Clicked Generate API Key")
        else:
            gen_btn2 = page.locator('button:has-text("Generate"), button:has-text("Create Key"), button:has-text("Create New Key")')
            if await gen_btn2.count() > 0:
                await gen_btn2.first.click(force=True)
                log("Clicked generate/create button (fallback)")
            else:
                log("Generate API Key button not found!")
                return None

        await asyncio.sleep(3)

        # Step 15: Extract key
        log("Extracting API key...")
        api_key = await page.evaluate("""
            () => {
                const patterns = [
                    () => {
                        const el = document.querySelector('code');
                        return el ? el.textContent.trim() : null;
                    },
                    () => {
                        const el = document.querySelector('input[value^="nvapi-"]');
                        return el ? el.value.trim() : null;
                    },
                    () => {
                        const match = document.body.innerText.match(/nvapi-[a-zA-Z0-9_-]{20,}/);
                        return match ? match[0] : null;
                    },
                    () => {
                        const el = document.querySelector('[data-key]');
                        return el ? el.getAttribute('data-key') : null;
                    },
                ];
                for (const fn of patterns) {
                    try { const v = fn(); if (v) return v; } catch(e) {}
                }
                return '';
            }
        """)

        if not api_key or not api_key.startswith(NVIDIA_TOKEN_PREFIX):
            try:
                copy_btn = page.locator("button[aria-label*='Copy'], button:has(svg)")
                if await copy_btn.count() > 0:
                    await copy_btn.first.click()
                    await asyncio.sleep(1)
                    api_key = await page.evaluate("() => navigator.clipboard.readText().catch(() => '')")
            except Exception:
                pass

        if not api_key or not api_key.startswith(NVIDIA_TOKEN_PREFIX):
            log("Failed to extract NVIDIA API key!")
            return None

        log(f"Extracted API key: {api_key[:20]}...")

        with KEYS_FILE.open("a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        log(f"Saved key to {KEYS_FILE}")

        return api_key

    finally:
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        try:
            cdp_proc.kill()
            cdp_proc.wait(timeout=5)
        except Exception:
            pass


async def main():
    log("=" * 60)
    log("NVIDIA Signup Automation")
    log("=" * 60)

    # Check for hCaptcha cookie
    cookies = load_captcha_cookie()
    if not cookies:
        log("No valid hCaptcha cookie found. Running get_cookie.py...")
        success = run_get_cookie()
        if success:
            log("Cookie obtained successfully")
        else:
            log("WARNING: Failed to obtain hCaptcha cookie — proceeding without it")
            log("The signup may still work but hCaptcha challenges will be harder to solve")

    # Retry logic: up to 3 full attempts
    for attempt in range(1, 4):
        log(f"\n{'='*60}")
        log(f"Attempt {attempt}/3")
        log(f"{'='*60}")

        # Burn all emails and create a fresh one
        email = create_agentmail_email()
        if not email:
            log("Failed to create email, retrying...")
            time.sleep(5)
            continue

        # Run the signup
        try:
            api_key = await run_full_signup(email, PASSWORD)
            if api_key and api_key.startswith(NVIDIA_TOKEN_PREFIX):
                log(f"\n=== SUCCESS ===")
                log(f"Key: {api_key[:20]}...")
                log(f"Saved to: {KEYS_FILE}")
                log("=" * 60)
                sys.exit(0)
        except Exception as e:
            log(f"Attempt {attempt} failed: {e}")

        # Burn the email regardless
        log("Burning email...")
        subprocess.run([sys.executable, str(AGENTMAIL_SCRIPT), "burn"], capture_output=True)

        # Refresh cookie if not the last attempt
        if attempt < 3:
            log(f"Retrying... (attempt {attempt + 1})")
            log("Refreshing hCaptcha cookie...")
            try:
                run_get_cookie()
            except Exception:
                log("Cookie refresh failed — continuing anyway")
            time.sleep(3)

    log("\n=== ALL ATTEMPTS FAILED ===")
    log(f"Keys saved so far: {KEYS_FILE}")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
