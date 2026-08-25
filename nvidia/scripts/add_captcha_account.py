#!/usr/bin/env python3
"""
Create and save reusable Camoufox (Firefox) profiles that stay logged into hCaptcha.

Saves each profile cleanly to:
    ~/atlas/data/firefox_profiles/{email_slug}/

Usage:
    python add_captcha_account_camoufox.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from camoufox.sync_api import Camoufox
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROFILES_ROOT = Path.home() / "atlas" / "data" / "firefox_profiles"

VALID_DOMAINS = {
    "google": "gmail.com",
    "hotmail": "hotmail.com",
}

HCAPTCHA_LOGIN_URL = "https://dashboard.hcaptcha.com/login"
PAGE_LOAD_TIMEOUT = 60_000
ELEMENT_TIMEOUT = 15_000
DISPLAY = ":1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Colors:
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @staticmethod
    def _wrap(code: str, text: str) -> str:
        if not sys.stdout.isatty():
            return text
        return f"{code}{text}{Colors.RESET}"


def green(t: str) -> str: return Colors._wrap(Colors.GREEN, t)
def cyan(t: str) -> str: return Colors._wrap(Colors.CYAN, t)
def yellow(t: str) -> str: return Colors._wrap(Colors.YELLOW, t)
def red(t: str) -> str: return Colors._wrap(Colors.RED, t)
def bold(t: str) -> str: return Colors._wrap(Colors.BOLD, t)


def sanitize_email_for_path(email: str) -> str:
    return re.sub(r"[^\w\-.]", "_", email.strip().lower())


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class AccountSpec:
    email: str
    domain_key: str  # "google" or "hotmail"

    @property
    def profile_dir(self) -> Path:
        return PROFILES_ROOT / sanitize_email_for_path(self.email)


# ---------------------------------------------------------------------------
# Email validation
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


def validate_email(raw: str) -> Optional[AccountSpec]:
    raw = raw.strip().lower()
    if not EMAIL_RE.match(raw):
        return None
    _, domain = raw.rsplit("@", 1)
    for key, suffix in VALID_DOMAINS.items():
        if domain == suffix:
            return AccountSpec(email=raw, domain_key=key)
    return None


def prompt_for_email() -> AccountSpec:
    print(bold("\n  hCaptcha Camoufox Profile Creator\n"))
    print(cyan("  Enter the email address you use for your hCaptcha account:"))
    print()

    while True:
        try:
            raw = input("  email> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(red("\n  Aborted."))
            sys.exit(1)

        if not raw:
            print(yellow("  Please enter an email address."))
            continue

        spec = validate_email(raw)
        if spec is None:
            print(red("  Only @gmail.com and @hotmail.com are accepted."))
            continue

        print(green(f"  ✓ Accepted: {spec.email} ({spec.domain_key})"))
        return spec


# ---------------------------------------------------------------------------
# Browser flow
# ---------------------------------------------------------------------------

def click_provider_button(page, domain_key: str) -> bool:
    if domain_key == "google":
        texts = ["Sign in with Google", "Continue with Google", "Google"]
    else:
        texts = ["Sign in with Microsoft", "Continue with Microsoft", "Microsoft"]

    for text in texts:
        try:
            page.get_by_text(text, exact=False).first.click(timeout=8000)
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def fill_email(page, email: str) -> bool:
    selectors = [
        'input[type="email"]',
        'input[name="identifier"]',
        'input[name="login"]',
        'input[autocomplete="username"]',
        'input[type="text"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
            loc.fill(email)
            return True
        except Exception:
            continue
    return False


def run_authentication_flow(spec: AccountSpec) -> Path:
    os.environ["DISPLAY"] = DISPLAY

    # Create a clean temporary profile directory
    temp_root = Path(tempfile.mkdtemp(prefix="camoufox_profile_"))
    print(cyan(f"  ● Temp profile: {temp_root}"))

    print(yellow("  Launching Camoufox on :1 ..."))
    with Camoufox(
        persistent_context=True,
        user_data_dir=str(temp_root),
        headless=False,
    ) as context:

        page = context.pages[0] if context.pages else context.new_page()

        print(yellow(f"  Navigating to {HCAPTCHA_LOGIN_URL}"))
        page.goto(HCAPTCHA_LOGIN_URL, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        print(green("  ✓ Login page loaded"))

        print(yellow(f"  Clicking {spec.domain_key.title()} button ..."))
        if not click_provider_button(page, spec.domain_key):
            print(red("  Could not find button – please click it manually."))
            input("  Press Enter after clicking the button ... ")
        else:
            print(green("  ✓ Provider button clicked"))

        time.sleep(2)

        print(yellow("  Filling email ..."))
        if fill_email(page, spec.email):
            print(green(f"  ✓ Email filled: {spec.email}"))
        else:
            print(red("  Could not auto-fill email – please type it manually."))
            input("  Press Enter after filling the email ... ")

        print()
        print(bold("  ┌──────────────────────────────────────────────────────────┐"))
        print(bold("  │  Complete the full login in the browser window.        │"))
        print(bold("  │  When you are fully logged into hCaptcha,              │"))
        print(bold("  │  come back here and press Enter.                       │"))
        print(bold("  └──────────────────────────────────────────────────────────┘"))
        print()

        try:
            input("  Press Enter once login is complete → ")
        except (EOFError, KeyboardInterrupt):
            print(red("\n  Aborted."))
            shutil.rmtree(temp_root, ignore_errors=True)
            sys.exit(1)

        print(yellow("  Closing browser and flushing profile ..."))
        # context closes automatically when leaving the `with` block

    time.sleep(1.5)  # let Firefox finish writing files
    return temp_root


# ---------------------------------------------------------------------------
# Save profile
# ---------------------------------------------------------------------------

def copy_profile_clean(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)

    def ignore_transient(directory, files):
        ignore = {"lock", "parent.lock", ".parentlock", "Session Storage"}
        return [f for f in files if f in ignore]

    shutil.copytree(src, dst, ignore=ignore_transient)


def save_profile(spec: AccountSpec, source_dir: Path) -> Path:
    dest = spec.profile_dir
    print(cyan(f"  Saving profile to: {dest}"))

    dest.parent.mkdir(parents=True, exist_ok=True)
    copy_profile_clean(source_dir, dest)

    # Metadata
    meta = dest / ".atlas_profile_meta.json"
    now = datetime.now(timezone.utc).isoformat()
    meta.write_text(
        "{\n"
        f'  "email": "{spec.email}",\n'
        f'  "provider": "{spec.domain_key}",\n'
        f'  "created_at": "{now}",\n'
        f'  "engine": "camoufox",\n'
        f'  "source": "add_captcha_account_camoufox.py"\n'
        "}\n"
    )
    return dest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print()
    print(bold("  ════════════════════════════════════════════════════"))
    print(bold("   Atlas — Camoufox hCaptcha Profile Creator"))
    print(bold("  ════════════════════════════════════════════════════"))
    print()

    spec = prompt_for_email()

    print()
    print(yellow(f"  Provider : {spec.domain_key.title()}"))
    print(yellow(f"  Email    : {spec.email}"))
    print(yellow(f"  Target   : {spec.profile_dir}"))
    print()

    try:
        temp_path = run_authentication_flow(spec)
    except Exception as e:
        print(red(f"  ERROR: {e}"))
        import traceback
        traceback.print_exc()
        return 1

    try:
        saved = save_profile(spec, temp_path)
    except Exception as e:
        print(red(f"  ERROR saving profile: {e}"))
        return 2
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)

    print()
    print(green("  ┌──────────────────────────────────────────────────────────┐"))
    print(green("  │  Camoufox profile saved successfully.                  │"))
    print(green(f"  │  Location: {str(saved)[:46]:<46s} │"))
    print(green("  │  Ready for reuse with Camoufox.                        │"))
    print(green("  └──────────────────────────────────────────────────────────┘"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
