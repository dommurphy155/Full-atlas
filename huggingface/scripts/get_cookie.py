#!/usr/bin/env python3
"""
get_cookie.py — Extract hc_accessibility cookie from hCaptcha.

Navigation strategy:
    1. Navigate to the login page.
    2. Wait for DOMContentLoaded only.
    3. Wait up to 30 seconds for the provider Sign-in button.
    4. If found, click it immediately.
    5. If NOT found after the full 30 seconds, reload once and repeat.
    6. Never refresh while the browser is still loading/rendering the page.

The browser is intentionally not forced to wait for networkidle because
hCaptcha pages can keep background network connections alive indefinitely.
"""

from __future__ import annotations

import atexit
import glob
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from config import (
    CAPTCHA_COOKIE_FILE,
    FIREFOX_PROFILES_DIR,
    TARGET_COOKIE_NAME,
    TARGET_URL,
    SUCCESS_TEXT,
    LIMIT_TEXT,
    DISPLAY_NUM,
    TIMEOUT_MS,
    PAGE_LOAD_TIMEOUT_MS,
    setup_logging,
    first_run_setup,
    is_camoufox_browser_installed,
    ensure_camoufox_browsers,
)


log = setup_logging("get_cookie")


# ============================================================================
# Tunables
# ============================================================================

# How long to wait for the provider button after DOMContentLoaded.
SIGNIN_WAIT_MS = 60_000

# Maximum number of complete navigation attempts per profile.
MAX_NAVIGATION_ATTEMPTS = 2

# How long to wait for the post-login redirect.
REDIRECT_TIMEOUT_MS = 120_000

# Keep this short. networkidle is deliberately avoided.
DOM_LOAD_TIMEOUT_MS = max(
    10_000,
    min(PAGE_LOAD_TIMEOUT_MS, 45_000),
)

# Button interaction timeouts.
CLICK_TIMEOUT_MS = 30_000
FORCE_CLICK_TIMEOUT_MS = 15_000
SCROLL_TIMEOUT_MS = 10_000

# Cookie polling.
COOKIE_POLL_INTERVAL_MS = 2_000
COOKIE_POLL_ATTEMPTS = 30


# ============================================================================
# Cleanup
# ============================================================================

_TEMP_DIR: Path | None = None


def _cleanup() -> None:
    """Remove temporary browser profiles and runtime leftovers."""
    global _TEMP_DIR

    if _TEMP_DIR and _TEMP_DIR.exists():
        shutil.rmtree(_TEMP_DIR, ignore_errors=True)

    for pattern in (
        "/tmp/cdp_browser_profile_*",
        "/tmp/camoufox_tmp_*",
        "/tmp/hf_profile_*",
    ):
        for directory in glob.glob(pattern):
            if os.path.isdir(directory):
                shutil.rmtree(directory, ignore_errors=True)


atexit.register(_cleanup)


def _handle_signal(signum: int, _frame) -> None:
    """
    Handle Ctrl-C/termination cleanly.

    Do not call sys.exit() directly from a signal lambda while Playwright is
    inside an API call. That can interrupt Playwright's internal dispatcher
    and produce misleading TargetClosedError/CancelledError tracebacks.
    """
    name = signal.Signals(signum).name

    try:
        log.warning(f"Received {name} — shutting down cleanly...")
    except Exception:
        pass

    raise KeyboardInterrupt


for _sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(_sig, _handle_signal)
    except (ValueError, OSError):
        pass


# ============================================================================
# Helpers
# ============================================================================

def get_signin_button_text(provider: str) -> str:
    """Return the expected provider sign-in button text."""
    return (
        "Sign in with Google"
        if provider == "google"
        else "Sign in with Microsoft"
    )


def extract_cookie(context) -> dict | None:
    """Extract the target cookie from the browser context."""
    try:
        for cookie in context.cookies():
            if cookie.get("name") == TARGET_COOKIE_NAME:
                return {
                    "name": cookie.get("name"),
                    "value": cookie.get("value"),
                    "domain": cookie.get("domain"),
                    "path": cookie.get("path"),
                    "expires": cookie.get("expires"),
                    "httpOnly": cookie.get("httpOnly"),
                    "secure": cookie.get("secure"),
                    "sameSite": cookie.get("sameSite"),
                }
    except Exception:
        pass

    return None


def save_cookie(cookie: dict) -> None:
    """Overwrite the cookie file with a single fresh cookie."""
    if not cookie or not cookie.get("value"):
        log.error("Invalid cookie: missing value")
        return

    try:
        CAPTCHA_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CAPTCHA_COOKIE_FILE.write_text(
            json.dumps([cookie], indent=2) + "\n"
        )
    except Exception as exc:
        log.error(f"Failed to save cookie: {exc}")


def dismiss_consent_banners(page) -> None:
    """Best-effort dismissal of common consent banners."""
    for banner_text in (
        "Accept all",
        "Accept",
        "I agree",
        "Got it",
    ):
        try:
            banner_btn = page.get_by_text(
                banner_text,
                exact=False,
            ).first

            if banner_btn.is_visible(timeout=1_000):
                banner_btn.click(timeout=1_500)
                log.info(f"Dismissed banner: {banner_text}")
                page.wait_for_timeout(250)

        except Exception:
            # Consent banners are optional and must never block the flow.
            pass


def wait_for_signin_button(page, signin_text: str):
    """
    Wait up to SIGNIN_WAIT_MS for the provider button.

    This is intentionally one continuous wait. We DO NOT reload the page
    while waiting because the browser may simply be slow to render the DOM.
    """
    log.info(
        f"Waiting up to {SIGNIN_WAIT_MS // 1000}s "
        f"for '{signin_text}'..."
    )

    button = page.get_by_text(
        signin_text,
        exact=False,
    ).first

    # Playwright handles the polling internally. This avoids repeatedly
    # querying the DOM and, importantly, avoids refreshing the page.
    button.wait_for(
        state="visible",
        timeout=SIGNIN_WAIT_MS,
    )

    return button


def click_signin_button(page, button, signin_text: str) -> None:
    """Scroll to and click the provider sign-in button."""
    try:
        button.scroll_into_view_if_needed(
            timeout=SCROLL_TIMEOUT_MS
        )
    except Exception:
        # Scrolling is cosmetic. Do not make it a failure condition.
        pass

    try:
        button.click(timeout=CLICK_TIMEOUT_MS)
    except Exception as normal_error:
        log.warning(
            f"Normal click failed, trying force click: {normal_error}"
        )
        button.click(
            timeout=FORCE_CLICK_TIMEOUT_MS,
            force=True,
        )

    # Do NOT require the current page URL to change here.
    # OAuth providers may open a popup/new tab or handle navigation
    # asynchronously. A successful Playwright click is enough.
    log.info(f"Clicked: {signin_text}")


def navigate_to_signin(page, provider: str) -> bool:
    """
    Navigate to the hCaptcha login page and locate the provider button.

    Important behaviour:
        - DOMContentLoaded is enough.
        - No networkidle wait.
        - Full 30-second wait for the button.
        - Reload only after that 30-second wait expires.
    """
    signin_text = get_signin_button_text(provider)

    for navigation_attempt in range(1, MAX_NAVIGATION_ATTEMPTS + 1):
        log.info(
            f"Goto {TARGET_URL} "
            f"(attempt {navigation_attempt}/{MAX_NAVIGATION_ATTEMPTS})"
        )

        try:
            page.goto(
                TARGET_URL,
                timeout=DOM_LOAD_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            log.warning(
                f"Navigation did not complete cleanly: {exc}"
            )

        # Do not wait for networkidle here.
        #
        # The browser can continue loading/rendering after DOMContentLoaded,
        # and the sign-in button wait below gives it the time it needs.
        dismiss_consent_banners(page)

        try:
            button = wait_for_signin_button(
                page,
                signin_text,
            )

            log.info(f"Found: {signin_text}")

            click_signin_button(
                page,
                button,
                signin_text,
            )

            return True

        except KeyboardInterrupt:
            raise

        except Exception as exc:
            log.warning(
                f"'{signin_text}' was not usable after "
                f"{SIGNIN_WAIT_MS // 1000}s: {exc}"
            )

            if navigation_attempt >= MAX_NAVIGATION_ATTEMPTS:
                log.error(
                    f"Signin button unavailable after "
                    f"{MAX_NAVIGATION_ATTEMPTS} navigation attempts"
                )
                return False

            # ONLY NOW do we refresh.
            log.info(
                "30-second signin wait expired — "
                "reloading page for another attempt..."
            )

            try:
                page.reload(
                    timeout=DOM_LOAD_TIMEOUT_MS,
                    wait_until="domcontentloaded",
                )
            except Exception as reload_error:
                log.warning(
                    f"Reload did not complete cleanly: {reload_error}"
                )

    return False


# ============================================================================
# Profile
# ============================================================================

def try_profile(
    profile_path: Path,
    max_retries: int = 2,
) -> bool:
    """Try to extract the cookie from a single browser profile."""
    provider = (
        "microsoft"
        if any(
            x in profile_path.name.lower()
            for x in ("hotmail", "outlook", "live", "msn")
        )
        else "google"
    )

    log.info(
        f"Profile: {profile_path.name} ({provider})"
    )

    global _TEMP_DIR

    _TEMP_DIR = Path(
        tempfile.mkdtemp(
            prefix="camoufox_tmp_",
            dir="/tmp/",
        )
    )

    try:
        def _ignore_locks(directory, files):
            return [
                filename
                for filename in files
                if filename in (
                    "lock",
                    "parent.lock",
                    ".parentlock",
                )
            ]

        shutil.copytree(
            profile_path,
            _TEMP_DIR,
            dirs_exist_ok=True,
            ignore=_ignore_locks,
        )

    except Exception as exc:
        log.error(f"Failed to copy profile: {exc}")
        return False

    os.environ["DISPLAY"] = DISPLAY_NUM

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        log.error("camoufox not installed")
        return False

    if not is_camoufox_browser_installed():
        log.warning(
            "Camoufox package present but browser binaries "
            "not found — attempting install"
        )
        ensure_camoufox_browsers()

    for attempt in range(1, max_retries + 1):
        try:
            with Camoufox(
                persistent_context=True,
                user_data_dir=str(_TEMP_DIR),
                headless=False,
            ) as context:

                page = (
                    context.pages[0]
                    if context.pages
                    else context.new_page()
                )

                # Keep generic Playwright operations responsive.
                # Individual important waits below use explicit timeouts.
                try:
                    page.set_default_timeout(5_000)
                except Exception:
                    pass

                # ----------------------------------------------------------
                # STEP 1: Login page + provider button
                # ----------------------------------------------------------

                signin_success = navigate_to_signin(
                    page,
                    provider,
                )

                if not signin_success:
                    log.error("Signin button unreachable")

                    if attempt < max_retries:
                        log.info(
                            f"Retrying profile "
                            f"(attempt {attempt + 1}/{max_retries})..."
                        )
                        continue

                    return False

                # ----------------------------------------------------------
                # STEP 2: Wait for accessibility redirect
                # ----------------------------------------------------------

                redirect_success = False

                try:
                    page.wait_for_url(
                        "**/welcome_accessibility**",
                        timeout=REDIRECT_TIMEOUT_MS,
                    )

                    # DOM is all we need here.
                    try:
                        page.wait_for_load_state(
                            "domcontentloaded",
                            timeout=60_000,
                        )
                    except Exception:
                        pass

                    redirect_success = True

                    log.info(
                        f"Landed on: {page.url}"
                    )

                except Exception as exc:
                    log.warning(
                        f"Redirect timeout: {exc} "
                        f"(current url: {page.url})"
                    )

                if not redirect_success:
                    if attempt < max_retries:
                        log.info("Retrying profile...")
                        continue

                    log.error(
                        "Redirect failed after retries"
                    )
                    return False

                # ----------------------------------------------------------
                # STEP 3: Click Set Cookie
                # ----------------------------------------------------------

                setcookie_success = False

                for retry in range(2):
                    try:
                        try:
                            page.wait_for_load_state(
                                "domcontentloaded",
                                timeout=60_000,
                            )
                        except Exception:
                            pass

                        btn = page.get_by_text(
                            SUCCESS_TEXT,
                            exact=False,
                        ).first

                        btn.wait_for(
                            state="visible",
                            timeout=60_000,
                        )

                        try:
                            btn.scroll_into_view_if_needed(
                                timeout=SCROLL_TIMEOUT_MS
                            )
                        except Exception:
                            pass

                        try:
                            btn.click(
                                timeout=6_000
                            )
                        except Exception:
                            log.warning(
                                "Normal click failed, "
                                "trying force click"
                            )
                            btn.click(
                                timeout=3_000,
                                force=True,
                            )

                        log.info("Clicked: Set Cookie")
                        setcookie_success = True
                        break

                    except KeyboardInterrupt:
                        raise

                    except Exception as exc:
                        log.warning(
                            f"Set Cookie click failed "
                            f"(retry {retry + 1}): {exc}"
                        )

                        if retry == 0:
                            try:
                                page.reload(
                                    timeout=DOM_LOAD_TIMEOUT_MS,
                                    wait_until="domcontentloaded",
                                )
                            except Exception as reload_error:
                                log.warning(
                                    f"Set Cookie reload failed: "
                                    f"{reload_error}"
                                )

                if not setcookie_success:
                    log.error(
                        "Set Cookie button unreachable"
                    )

                    if attempt < max_retries:
                        continue

                    return False

                # ----------------------------------------------------------
                # STEP 4: Check daily limit
                # ----------------------------------------------------------

                try:
                    page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=5_000,
                    )

                    if (
                        page.get_by_text(
                            LIMIT_TEXT,
                            exact=False,
                        ).count()
                        > 0
                    ):
                        log.warning(
                            "Daily limit reached"
                        )
                        return False

                except Exception:
                    pass

                # ----------------------------------------------------------
                # STEP 5: Poll for cookie
                # ----------------------------------------------------------

                cookie = None

                for poll in range(
                    COOKIE_POLL_ATTEMPTS
                ):
                    cookie = extract_cookie(context)

                    if cookie:
                        break

                    if poll < COOKIE_POLL_ATTEMPTS - 1:
                        page.wait_for_timeout(
                            COOKIE_POLL_INTERVAL_MS
                        )

                if not cookie:
                    log.error(
                        "Cookie not found after polling"
                    )

                    if attempt < max_retries:
                        continue

                    return False

                # ----------------------------------------------------------
                # STEP 6: Verify + save
                # ----------------------------------------------------------

                if (
                    cookie.get("name") != TARGET_COOKIE_NAME
                    or not cookie.get("value")
                ):
                    log.error(
                        "Extracted cookie failed verification"
                    )

                    if attempt < max_retries:
                        continue

                    return False

                save_cookie(cookie)

                log.info(
                    f"Verified '{TARGET_COOKIE_NAME}' cookie "
                    f"— saved to {CAPTCHA_COOKIE_FILE}"
                )

                return True

        except KeyboardInterrupt:
            log.warning(
                "Interrupted — closing browser cleanly..."
            )
            return False

        except Exception as exc:
            log.error(
                f"Attempt {attempt} failed: {exc}"
            )

            if attempt < max_retries:
                log.info(
                    f"Retrying profile "
                    f"(attempt {attempt + 1}/{max_retries})..."
                )

    return False


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    """Program entry point."""
    first_run_setup()
    _cleanup()

    if not FIREFOX_PROFILES_DIR.is_dir():
        log.error(
            f"No profiles found under {FIREFOX_PROFILES_DIR}"
        )
        sys.exit(1)

    profiles = sorted(
        path
        for path in FIREFOX_PROFILES_DIR.iterdir()
        if path.is_dir()
    )

    if not profiles:
        log.error(
            f"No profiles found under {FIREFOX_PROFILES_DIR}"
        )
        sys.exit(1)

    log.info(
        f"Trying {len(profiles)} profile(s)"
    )

    for idx, profile in enumerate(profiles, 1):
        log.info(
            f"[{idx}/{len(profiles)}] "
            f"Attempting {profile.name}..."
        )

        try:
            if try_profile(profile):
                log.info(
                    "✓ Done – cookie extracted successfully"
                )
                sys.exit(0)

        except KeyboardInterrupt:
            log.warning(
                "Interrupted by user"
            )
            sys.exit(130)

        log.info(
            f"✗ Profile {profile.name} failed, "
            f"trying next..."
        )

        _cleanup()

    log.error(
        f"❌ No working profile found after "
        f"{len(profiles)} attempts"
    )

    sys.exit(1)


if __name__ == "__main__":
    main()
