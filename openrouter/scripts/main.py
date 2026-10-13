#!/usr/bin/env python3
"""
OpenRouter signup automation using Patchright (Chromium-based anti-detect browser).
"""

import asyncio
import os
import sys
import json
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    ROOT_DIR,
    DATA_DIR,
    STATE_FILE,
    ENV_FILE,
    KEYS_FILE,
    AGENTMAIL_API_KEY,
    AGENTMAIL_BASE,
    VIRTUAL_DISPLAY,
    HEADLESS,
    BROWSER_TIMEOUT,
    NAVIGATION_TIMEOUT,
    SIGNUP_URL,
    ensure_dependencies,
    is_py_module_available,
    install_py_module,
)
from agentmail import AgentMailManager
from fingerprint import inject_fingerprint


async def bootstrap():
    print("[Bootstrap] Checking dependencies...")
    ensure_dependencies()

    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)

    api_key = os.getenv("AGENTMAIL_API_KEY")
    if not api_key:
        print("[Bootstrap] ERROR: AGENTMAIL_API_KEY not found in .env", file=sys.stderr)
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("[Bootstrap] Checking Patchright browser...")
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "patchright", "install", "chromium"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[Bootstrap] Patchright install failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    print("[Bootstrap] All checks passed!")
    return api_key


async def create_email(api_key: str) -> str:
    print("[Email] Creating fresh AgentMail inbox...")
    mgr = AgentMailManager(api_key)

    print("[Email] Nuking all existing inboxes...")
    await mgr.nuke_all()
    await asyncio.sleep(2)

    inbox = await mgr.create_inbox()
    inbox_id = inbox.get("inbox_id") or inbox.get("id")
    address = inbox.get("address") or inbox.get("email")
    if not address:
        local = (
            inbox.get("local_part")
            or inbox.get("username")
            or inbox.get("local")
            or inbox.get("name")
        )
        domain = inbox.get("domain") or "agentmail.to"
        if not local:
            raise RuntimeError(f"Cannot find local part in response: {inbox}")
        address = f"{local}@{domain}"

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"inbox_id": inbox_id, "address": address}) + "\n", encoding="utf-8")

    print(f"[Email] Created: {address}")
    return address


async def get_verification_url(api_key: str, timeout: int = 180) -> str:
    print("[Email] Waiting for OpenRouter verification email...")
    if not STATE_FILE.exists():
        raise RuntimeError("No state file found. Run create first.")

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    inbox_id = state["inbox_id"]
    mgr = AgentMailManager(api_key)

    start = asyncio.get_event_loop().time()
    async with __import__("httpx").AsyncClient() as client:
        while asyncio.get_event_loop().time() - start < timeout:
            try:
                r = await client.get(f"{AGENTMAIL_BASE}/inboxes/{inbox_id}/messages", headers=mgr._headers(), timeout=30)
                data = r.json()
                msgs = data if isinstance(data, list) else data.get("messages", [])
                for msg in msgs:
                    msg_id = (msg.get("id") or msg.get("message_id") or msg.get("uid") or msg.get("messageId"))
                    if not msg_id:
                        continue

                    mr = await client.get(f"{AGENTMAIL_BASE}/inboxes/{inbox_id}/messages/{msg_id}", headers=mgr._headers(), timeout=30)
                    msg_data = mr.json()
                    full_html = msg_data.get("html", "") or msg_data.get("extracted_html", "") or msg_data.get("body", "") or ""
                    import html
                    full_html = html.unescape(full_html)

                    m = re.search(r'https://clerk[.]openrouter[.]ai/v1/verify\?[^\s"\'<>]+', full_html)
                    if m:
                        return m.group()

                    m = re.search(r'https://openrouter[.]ai/verify/[^\s"\'<>]+', full_html)
                    if m:
                        return m.group()

                    m = re.search(r'(?:href|src)=["\']([^"\']*clerk[.]openrouter[.]ai/v1/verify[^"\']*)["\']', full_html, re.I)
                    if m:
                        return m.group(1)

                    m = re.search(r'(?:href|src)=["\']([^"\']*openrouter[.]ai/verify[^"\']*)["\']', full_html, re.I)
                    if m:
                        return m.group(1)
            except Exception as e:
                print(f"[Email] Poll error: {e}", file=sys.stderr)
            await asyncio.sleep(6)

    raise RuntimeError("Timed out waiting for verification URL")


async def run_signup(email: str, password: str = "K7#mP9$vL2@nQ5!xR4&wE8*zT1") -> str:
    print("[Browser] Launching Patchright (headed on virtual display :1)...")

    from patchright.async_api import async_playwright
    import tempfile
    import uuid
    import shutil

    # Create unique user data dir for complete isolation each run
    user_data_dir = tempfile.mkdtemp(prefix=f"chrome_profile_{uuid.uuid4().hex[:8]}_")

    # Start Xvfb on display :1 if not already running
    import subprocess
    xvfb_proc = subprocess.Popen(
        ["Xvfb", ":1", "-screen", "0", "1920x1080x24", "-ac", "+extension", "GLX", "+render", "-noreset"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Give Xvfb a moment to start
    await asyncio.sleep(1)

    # Set DISPLAY for the browser
    os.environ["DISPLAY"] = VIRTUAL_DISPLAY

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=HEADLESS,
            channel="chrome",
        )
        
        context = browser
        
        page = await context.new_page()
        page.set_default_timeout(BROWSER_TIMEOUT)

        # Inject Mac Mini fingerprint before any navigation
        await inject_fingerprint(page)

        try:
            print(f"[Browser] Navigating to {SIGNUP_URL}...")
            await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)

            print("[Browser] Waiting for form to be ready...")
            await page.wait_for_selector('#emailAddress-field', state="visible", timeout=30000)
            await page.wait_for_function("() => document.querySelector('#emailAddress-field')?.offsetParent !== null", timeout=10000)

            print("[Browser] Filling email field...")
            await page.fill('#emailAddress-field', email)

            print("[Browser] Filling password field...")
            await page.fill('#password-field', password)

            print("[Browser] Clicking legal acceptance checkbox...")
            await page.check('#legalAccepted-field')

            print("[Browser] Clicking signup button...")
            await page.click('button.cl-formButtonPrimary.cl-button[data-localization-key="formButtonPrimary"][data-variant="solid"][data-color="primary"]')

            print("[Browser] Waiting for navigation after signup...")
            await page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            await page.wait_for_url("**/*", timeout=NAVIGATION_TIMEOUT)

            print("[Browser] Signup form submitted, checking email for verification...")
            verification_url = await get_verification_url(os.getenv("AGENTMAIL_API_KEY"))

            print("[Browser] Navigating to verification url")
            await page.goto(verification_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            await page.wait_for_load_state("networkidle", timeout=15000)

            print("[Browser] Clicking Next button...")
            await page.wait_for_selector('button:has-text("Next"), button:has-text("Verify"), button:has-text("Continue"), button[type="submit"], button[type="button"]', state="visible", timeout=30000)
            await page.click('button:has-text("Next"), button:has-text("Verify"), button:has-text("Continue"), button[type="submit"], button[type="button"]')

            print("[Browser] Waiting for API key page...")
            await page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            await page.wait_for_selector('button[aria-label="Copy API key to clipboard"]', state="visible", timeout=30000)

            print("[Browser] Clicking copy API key button...")
            copy_button = page.locator('button[aria-label="Copy API key to clipboard"]')
            await copy_button.wait_for(state="visible", timeout=10000)
            await copy_button.click()

            print("[Browser] Extracting API key from page...")
            await page.wait_for_timeout(1000)
            page_text = await page.locator('code, pre, [data-api-key]').first.text_content()
            if page_text:
                import re
                m = re.search(r'(sk-or-v1-[a-zA-Z0-9]+)', page_text)
                if m:
                    api_key = m.group(1)
                else:
                    api_key = page_text.strip()
            if not api_key or len(api_key) < 20:
                raise RuntimeError("Failed to extract API key")

            print(f"[Browser] Got API key: {api_key[:20]}...")

            KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(KEYS_FILE, "a") as f:
                f.write(f"{api_key}\n")
            print(f"[Browser] Saved API key to {KEYS_FILE}")

            await context.close()
            await browser.close()
            xvfb_proc.terminate()
            shutil.rmtree(user_data_dir, ignore_errors=True)
            return api_key

        except Exception as e:
            await context.close()
            await browser.close()
            xvfb_proc.terminate()
            shutil.rmtree(user_data_dir, ignore_errors=True)
            raise


async def main():
    print("=" * 50)
    print("OpenRouter Signup Automation (Patchright)")
    print("=" * 50)

    api_key = await bootstrap()
    email = await create_email(api_key)

    try:
        result = await run_signup(email)
        print(f"\n[SUCCESS] OpenRouter API key saved: {result[:20]}...")
    except Exception as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())