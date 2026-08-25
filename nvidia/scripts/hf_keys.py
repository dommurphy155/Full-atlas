#!/usr/bin/env python3
"""
NVIDIA API Key Signup Automation via Patchright (Playwright-compatible).
Connects to existing Chrome CDP launched by launch_cdp.sh.

Flow:
  1. Navigate to https://build.nvidia.com/explore/discover?modal=signin
  2. Fill email → click Next
  3. Fill password + confirm → click hCaptcha checkbox → Create Account
  4. Poll AgentMail for 6-digit verification code
  5. Fill code → Continue → Submit
  6. Fill org name → Create NVIDIA Cloud Account
  7. Navigate to API keys page → Generate key → Extract nvapi- key
  8. Save to ~/atlas/data/nvidia_data/nvda_keys.txt

Usage:
    python3 hf_keys.py <email> <password>
"""

import asyncio
import os
import re
import subprocess
import sys
import random
import string
from pathlib import Path
from typing import Optional

# Import config for paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    ROOT_DIR, DATA_DIR, KEYS_FILE, AGENTMAIL_SCRIPT, CDP_HOST,
    CDP_PORT_FILE, CDP_TIMEOUT, SIGNUP_TIMEOUT, EMAIL_POLL_TIMEOUT,
    EMAIL_POLL_INTERVAL, PAGE_LOAD_TIMEOUT, ELEMENT_TIMEOUT,
    CAPTCHA_WAIT, PASSWORD, NVIDIA_LOGIN_URL, NVIDIA_TOKEN_PREFIX,
    NVIDIA_SETTINGS_URL, AGENTMAIL_API_KEY, AGENTMAIL_BASE,
)

try:
    from patchright.async_api import async_playwright
except ImportError:
    print("Installing patchright...", file=sys.stderr)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "patchright"])
    from patchright.async_api import async_playwright

try:
    import httpx
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx"])
    import httpx


def _get_cdp_url() -> str:
    """Read the CDP port from /tmp/cdp_port.txt, fallback to 9333."""
    try:
        port = CDP_PORT_FILE.read_text().strip()
        return f"http://{CDP_HOST}:{port}"
    except Exception:
        return f"http://{CDP_HOST}:9333"


CDP_URL = _get_cdp_url()


def generate_password() -> str:
    """Generate a strong password that meets NVIDIA's requirements."""
    # NVIDIA requires: min 8 chars, at least one uppercase, lowercase, number
    upper = random.choice(string.ascii_uppercase)
    lower = random.choice(string.ascii_lowercase)
    digit = random.choice(string.digits)
    special = random.choice("!@#$%^&*")
    rest = "".join(random.choices(string.ascii_letters + string.digits + "!@#$%^&*", k=12))
    return f"{upper}{lower}{digit}{special}{rest}"


def generate_org_name() -> str:
    """Generate a random organization name."""
    adjectives = ["Nexus", "Quantum", "Apex", "Vertex", "Stellar", "Cobalt",
                  "Aurum", "Verve", "Kairos", "Meridian", "Solaris", "Lumen"]
    nouns = ["Labs", "Systems", "Studios", "Works", "Collective", "Forge",
             "Dynamics", "Innovations", "Group", "Team", "Solutions", "Tech"]
    return f"{random.choice(adjectives)} {random.choice(nouns)}"


class NvidiaSignup:
    """Main NVIDIA signup automation class."""

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.org_name = generate_org_name()
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None
        self._api_key = os.getenv("AGENTMAIL_API_KEY")

    async def connect(self):
        """Connect to existing Chrome via CDP."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(CDP_URL)
        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        print(f"[NVIDIA] Connected to CDP browser, page: {self.page.url}")

    async def check_verification_email(self, timeout: int = 120) -> Optional[str]:
        """
        Poll AgentMail inbox for NVIDIA verification email.
        Extracts the 6-digit code from the email body.

        NVIDIA sends codes like "292-752" or "292752".
        """
        import json

        # Read state file to get inbox_id
        state_file = DATA_DIR / ".agentmail_state.json"
        if not state_file.exists():
            print("[NVIDIA] No AgentMail state file found", file=sys.stderr)
            return None

        state = json.loads(state_file.read_text())
        inbox_id = state.get("inbox_id")
        if not inbox_id:
            print("[NVIDIA] No inbox_id in state file", file=sys.stderr)
            return None

        headers = {"Authorization": f"Bearer {self._api_key}"}
        start = asyncio.get_event_loop().time()

        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() - start < timeout:
                try:
                    r = await client.get(
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

                        mr = await client.get(
                            f"{AGENTMAIL_BASE}/inboxes/{inbox_id}/messages/{msg_id}",
                            headers=headers, timeout=30
                        )
                        body = mr.json().get("body", "") or mr.json().get("html", "") or str(mr.json())

                        # Look for 6-digit code pattern: "292-752" or "292752"
                        # or "verification code is: 292-752"
                        code_patterns = [
                            r'(\d{3}-\d{3})',          # 292-752 format
                            r'(\d{6})',               # 292752 format
                        ]
                        for pattern in code_patterns:
                            m = re.search(pattern, body)
                            if m:
                                code = m.group(1)
                                # Normalize: remove dashes for filling
                                code_clean = code.replace("-", "")
                                if len(code_clean) == 6:
                                    print(f"[NVIDIA] Found verification code: {code}")
                                    return code
                except Exception as e:
                    print(f"[NVIDIA] Email poll error: {e}", file=sys.stderr)
                await asyncio.sleep(EMAIL_POLL_INTERVAL)

        return None

    async def run_signup(self) -> str:
        """Execute full NVIDIA signup flow. Returns the nvapi- key."""
        print(f"[NVIDIA] === Starting NVIDIA Signup ===")
        print(f"[NVIDIA] Email: {self.email}")
        print(f"[NVIDIA] Password: {self.password[:5]}...")

        # Step 1: Navigate to NVIDIA login page
        print("[NVIDIA] Navigating to NVIDIA login page...")
        await self.page.set_default_timeout(SIGNUP_TIMEOUT)
        await self.page.goto(NVIDIA_LOGIN_URL, wait_until="domcontentloaded", timeout=SIGNUP_TIMEOUT)
        await self.page.wait_for_load_state("networkidle", timeout=30000)
        print(f"[NVIDIA] Page loaded: title='{await self.page.title()}'")

        # Step 2: Fill email
        email_selector = 'input[name="login_hint"][type="email"]'
        try:
            await self.page.wait_for_selector(email_selector, timeout=ELEMENT_TIMEOUT)
            await self.page.locator(email_selector).first.fill(self.email)
            print(f"[NVIDIA] Filled email: {self.email}")
        except Exception:
            # Try broader selector
            email_selector_alt = 'input[type="email"]'
            await self.page.wait_for_selector(email_selector_alt, timeout=ELEMENT_TIMEOUT)
            await self.page.locator(email_selector_alt).first.fill(self.email)
            print(f"[NVIDIA] Filled email (alt): {self.email}")

        # Step 3: Click Next button
        next_selector = 'button[type="submit"]'
        next_btn = None
        buttons = await self.page.locator(next_selector).all()
        for btn in buttons:
            btn_text = await btn.inner_text()
            if "next" in btn_text.lower():
                next_btn = btn
                break
        if next_btn is None and buttons:
            next_btn = buttons[0]
        if next_btn:
            await next_btn.click()
            print("[NVIDIA] Clicked Next")
        else:
            raise Exception("Next button not found")

        await asyncio.sleep(2)
        await self.page.wait_for_load_state("networkidle", timeout=30000)

        # Step 4: Fill password
        pwd_selector = 'input[id="registration_password"][type="password"]'
        try:
            await self.page.wait_for_selector(pwd_selector, timeout=ELEMENT_TIMEOUT)
            await self.page.locator(pwd_selector).first.fill(self.password)
        except Exception:
            # Try broader
            pwd_alt = 'input[formcontrolname="password"][type="password"]'
            await self.page.wait_for_selector(pwd_alt, timeout=ELEMENT_TIMEOUT)
            await self.page.locator(pwd_alt).first.fill(self.password)
        print("[NVIDIA] Filled password")

        # Step 5: Fill confirm password
        pwd_confirm_selector = 'input[id="registration_passwordConfirm"][type="password"]'
        try:
            await self.page.wait_for_selector(pwd_confirm_selector, timeout=ELEMENT_TIMEOUT)
            await self.page.locator(pwd_confirm_selector).first.fill(self.password)
        except Exception:
            pwd_confirm_alt = 'input[formcontrolname="confirmPassword"][type="password"]'
            await self.page.wait_for_selector(pwd_confirm_alt, timeout=ELEMENT_TIMEOUT)
            await self.page.locator(pwd_confirm_alt).first.fill(self.password)
        print("[NVIDIA] Filled confirm password")

        # Step 6: Click hCaptcha checkbox
        print("[NVIDIA] Waiting for hCaptcha iframe...")
        await asyncio.sleep(CAPTCHA_WAIT)

        # Try clicking the hCaptcha checkbox — it's inside an iframe
        try:
            # Look for hCaptcha iframe and click the checkbox inside
            hcaptcha_iframe = await self.page.wait_for_selector(
                'iframe[src*="hcaptcha.com"], iframe[title*="hcaptcha"], iframe[title*="captcha"]',
                timeout=20000
            )
            frame = await hcaptcha_iframe.content_frame()
            if frame:
                # Click the checkbox inside the hCaptcha iframe
                checkbox = await frame.wait_for_selector(
                    '#checkbox, div#checkbox, [role="checkbox"]',
                    timeout=10000
                )
                await checkbox.click()
                print("[NVIDIA] Clicked hCaptcha checkbox")

                # Wait for hCaptcha to resolve (might need manual intervention)
                await asyncio.sleep(10)
                print("[NVIDIA] Waiting for hCaptcha to resolve...")
                # Give user time if it's an image challenge
                await self.page.wait_for_timeout(15000)
            else:
                print("[NVIDIA] Could not get hCaptcha iframe content — may need manual solve")
        except Exception as e:
            print(f"[NVIDIA] hCaptcha interaction note: {e}")
            print("[NVIDIA] If hCaptcha requires manual solving, complete it in the browser window")
            await asyncio.sleep(15)

        # Step 7: Click Create Account
        print("[NVIDIA] Looking for Create Account button...")
        create_btns = await self.page.locator('button').all()
        clicked_create = False
        for btn in create_btns:
            btn_text = await btn.inner_text()
            if "create" in btn_text.lower() and "account" in btn_text.lower():
                await btn.click()
                print("[NVIDIA] Clicked Create Account")
                clicked_create = True
                break
        if not clicked_create:
            # Fallback: look for span with "Create Account"
            create_span = self.page.locator('span:has-text("Create Account")')
            if await create_span.count() > 0:
                parent_btn = create_span.locator("xpath=ancestor::button")
                if await parent_btn.count() > 0:
                    await parent_btn.first.click()
                    print("[NVIDIA] Clicked Create Account (via span)")
                    clicked_create = True
        if not clicked_create:
            raise Exception("Create Account button not found")

        print("[NVIDIA] Waiting for verification code email...")

        # Step 8: Poll for 6-digit verification code from AgentMail
        code = await self.check_verification_email(timeout=120)
        if not code:
            raise Exception("No verification code received via email")
        print(f"[NVIDIA] Got verification code: {code}")

        # Step 9: Fill the verification code
        # NVIDIA uses 6 separate input boxes for the 6-digit code
        # Or sometimes a single input
        try:
            # Try individual input boxes first
            code_inputs = await self.page.locator('input[inputmode="numeric"], input[type="text"][inputmode="numeric"]').all()
            if len(code_inputs) >= 6:
                for i, char in enumerate(code):
                    if i < len(code_inputs):
                        await code_inputs[i].fill(char)
                print(f"[NVIDIA] Filled code into individual inputs: {code}")
            else:
                # Try single input
                code_input = self.page.locator('input[placeholder*="code"], input[placeholder*="Code"], input[aria-label*="code"]')
                if await code_input.count() > 0:
                    await code_input.first.fill(code)
                    print(f"[NVIDIA] Filled code: {code}")
                else:
                    # Fallback: try to fill into any visible input
                    await self.page.evaluate(f"""
                        () => {{
                            const inputs = document.querySelectorAll('input');
                            const code = '{code}';
                            inputs.forEach((el, i) => {{
                                if (i < code.length) el.value = code[i];
                            }});
                        }}
                    """)
                    print(f"[NVIDIA] Filled code via JS: {code}")
        except Exception as e:
            print(f"[NVIDIA] Code fill attempt: {e}")
            # Try JS injection
            await self.page.evaluate(f"""
                () => {{
                    const inputs = document.querySelectorAll('input');
                    const code = '{code}';
                    inputs.forEach((el, i) => {{
                        if (i < code.length) el.value = code[i];
                    }});
                }}
            """)
            print(f"[NVIDIA] Filled code via JS fallback: {code}")

        await asyncio.sleep(1)

        # Step 10: Click "Continue"
        print("[NVIDIA] Clicking Continue...")
        continue_btn = self.page.locator('button:has-text("Continue"), span:has-text("Continue")')
        if await continue_btn.count() > 0:
            await continue_btn.first.click()
            print("[NVIDIA] Clicked Continue")
        else:
            # Try any button on the page
            all_btns = await self.page.locator('button').all()
            for btn in all_btns:
                btn_text = await btn.inner_text()
                if "continue" in btn_text.lower():
                    await btn.click()
                    print("[NVIDIA] Clicked Continue (fallback)")
                    break

        await asyncio.sleep(3)

        # Step 11: Click "Submit"
        print("[NVIDIA] Clicking Submit...")
        submit_btn = self.page.locator('button:has-text("Submit"), button[type="submit"]')
        if await submit_btn.count() > 0:
            await submit_btn.first.click()
            print("[NVIDIA] Clicked Submit")
        await asyncio.sleep(2)

        # Step 12: Fill organization name
        print("[NVIDIA] Looking for organization name field...")
        org_selectors = [
            'input[data-testid="kui-text-input-element"]',
            'input[placeholder*="Organization"]',
            'input[name="name"]',
            'input[type="text"]',
        ]
        org_filled = False
        for sel in org_selectors:
            try:
                inputs = await self.page.locator(sel).all()
                for inp in inputs:
                    is_visible = await inp.is_visible()
                    if is_visible:
                        await inp.fill(self.org_name)
                        print(f"[NVIDIA] Filled org name: {self.org_name}")
                        org_filled = True
                        break
                if org_filled:
                    break
            except Exception:
                continue
        if not org_filled:
            print("[NVIDIA] Warning: could not fill org name automatically")

        # Step 13: Click "Create NVIDIA Cloud Account"
        print("[NVIDIA] Clicking Create NVIDIA Cloud Account...")
        create_nvc_btn = self.page.locator('button:has-text("Create NVIDIA Cloud Account")')
        if await create_nvc_btn.count() > 0:
            await create_nvc_btn.first.click()
            print("[NVIDIA] Clicked Create NVIDIA Cloud Account")
        else:
            # Fallback
            all_btns = await self.page.locator('button').all()
            for btn in all_btns:
                btn_text = await btn.inner_text()
                if "create" in btn_text.lower():
                    await btn.click()
                    print(f"[NVIDIA] Clicked create button (fallback): {btn_text.strip()}")
                    break

        await asyncio.sleep(5)
        print(f"[NVIDIA] Current URL: {self.page.url}")

        # Step 14: Navigate to API keys page
        print("[NVIDIA] Navigating to API keys page...")
        await self.page.goto(NVIDIA_SETTINGS_URL, wait_until="domcontentloaded", timeout=SIGNUP_TIMEOUT)
        await self.page.wait_for_load_state("networkidle", timeout=30000)
        print(f"[NVIDIA] API keys page: {self.page.url}")

        # Step 15: Click "Generate API Key" button
        print("[NVIDIA] Looking for Generate API Key button...")
        gen_btn = self.page.locator('button:has-text("Generate API Key")')
        if await gen_btn.count() > 0:
            await gen_btn.first.click()
            print("[NVIDIA] Clicked Generate API Key")
        else:
            # Try broader
            gen_btn2 = self.page.locator('button:has-text("Generate"), button:has-text("Create Key")')
            if await gen_btn2.count() > 0:
                await gen_btn2.first.click()
                print("[NVIDIA] Clicked generate/create button")
            else:
                raise Exception("Generate API Key button not found")

        await asyncio.sleep(2)

        # Step 16: Fill key name
        print("[NVIDIA] Filling key name...")
        key_name = f"key-{self.org_name.replace(' ', '-').lower()}"
        key_name_input = self.page.locator('input[name="name"], input[placeholder*="key name"], input[placeholder*="name"]')
        if await key_name_input.count() > 0:
            await key_name_input.first.fill(key_name)
            print(f"[NVIDIA] Filled key name: {key_name}")

        # Step 17: Click "Generate Key"
        gen_key_btn = self.page.locator('button:has-text("Generate"), button:has-text("Create")')
        if await gen_key_btn.count() > 0:
            # Click the last matching button (usually the final action button)
            btns = await gen_key_btn.all()
            await btns[-1].click()
            print("[NVIDIA] Clicked Generate Key")
        else:
            raise Exception("Generate Key button not found")

        # Step 18: Extract the API key
        await asyncio.sleep(3)
        api_key = await self.page.evaluate("""
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
                        const els = document.querySelectorAll('pre, code, .token, [class*="key"], [data-testid*="key"]');
                        for (const el of els) {
                            const text = el.textContent.trim() || el.value?.trim();
                            if (text && text.startsWith('nvapi-')) return text;
                        }
                        return null;
                    },
                    () => {
                        const match = document.body.innerText.match(/nvapi-[a-zA-Z0-9_-]{20,}/);
                        return match ? match[0] : null;
                    },
                ];
                for (const fn of patterns) {
                    try { const v = fn(); if (v) return v; } catch(e) {}
                }
                return '';
            }
        """)

        if not api_key or not api_key.startswith(NVIDIA_TOKEN_PREFIX):
            # Try clicking copy button
            try:
                copy_btn = self.page.locator("button[aria-label*='Copy'], button:has(svg)")
                if await copy_btn.count() > 0:
                    await copy_btn.first.click()
                    await asyncio.sleep(1)
                    api_key = await self.page.evaluate("() => navigator.clipboard.readText().catch(() => '')")
            except Exception:
                pass

        if not api_key or not api_key.startswith(NVIDIA_TOKEN_PREFIX):
            raise Exception(f"Failed to extract NVIDIA API key. URL: {self.page.url}")

        print(f"[NVIDIA] Extracted key: {api_key[:15]}...")

        # Step 19: Save key
        with KEYS_FILE.open("a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        print(f"[NVIDIA] Saved key to {KEYS_FILE}")

        return api_key

    async def close(self):
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


async def main():
    if len(sys.argv) < 3:
        print("Usage: hf_keys.py <email> <password>")
        sys.exit(1)

    email = sys.argv[1]
    password = sys.argv[2]

    signup = NvidiaSignup(email, password)
    try:
        await signup.connect()
        api_key = await signup.run_signup()
        print(f"[NVIDIA] SUCCESS: {api_key[:15]}...")
    except Exception as e:
        print(f"[NVIDIA] FAILED: {e}")
        raise
    finally:
        await signup.close()


if __name__ == "__main__":
    asyncio.run(main())
