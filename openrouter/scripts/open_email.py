#!/usr/bin/env python3
"""
OpenMail API wrapper for OpenRouter email verification.
Usage:
  python3 open_email.py create  -> Nukes all existing inboxes, creates fresh one, prints email.
  python3 open_email.py check   -> Polls the last created inbox for OpenRouter verification URL.
  python3 open_email.py burn    -> Deletes the inbox and clears state file.

Linux-only, portable paths from config.py.
"""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import re
import time
import os
import sys
import json
from pathlib import Path
from typing import Optional
import html

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    PROJECT_ROOT, DATA_DIR, RUN_DIR, STATE_FILE, ENV_FILE, LOG_DIR,
    OPENMAIL_BASE, ensure_dirs, get_openmail_api_keys,
)

ensure_dirs()


# Setup logging — flush immediately for real-time output
_logger = logging.getLogger("openmail")
_logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
_fh = RotatingFileHandler(str(LOG_DIR / "openmail.log"), maxBytes=5*1024*1024, backupCount=3)
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stderr)  # stderr so stdout stays clean for data
_sh.setFormatter(_fmt)
_logger.addHandler(_fh)
_logger.addHandler(_sh)
_logger.propagate = False
logger = _logger


def load_env():
    """Load .env file if it exists."""
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


class OpenMailManager:
    BASE = OPENMAIL_BASE or "https://api.openmail.sh"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.active: list = []

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _test_api_key(self) -> bool:
        """Test if this API key works by listing inboxes."""
        try:
            await self.list_inboxes()
            return True
        except Exception:
            return False

    async def list_inboxes(self) -> list:
        try:
            async with __import__("httpx").AsyncClient() as client:
                r = await client.get(
                    f"{self.BASE}/v1/inboxes",
                    headers=self._headers(),
                    timeout=15,
                )
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list):
                    return data
                return data.get("data", []) or data.get("inboxes", [])
        except __import__("httpx").HTTPError as e:
            logger.error(f"list_inboxes HTTP error: {e}")
            raise
        except Exception as e:
            logger.error(f"list_inboxes UNEXPECTED ERROR: {e}")
            raise

    async def nuke_all(self):
        """Delete every existing inbox and verify deletion.

        OpenMail deletion is asynchronous. This function:
        1. Issues DELETE requests for all inboxes
        2. Waits 2 seconds for API processing
        3. Polls with quick retries (up to 4 attempts, ~12 seconds max)
        """
        try:
            inboxes = await self.list_inboxes()

            if not inboxes:
                logger.info("No inboxes to nuke.")
                self.active = []
                return True

            deleted = 0
            failed = []

            async with __import__("httpx").AsyncClient() as client:
                for inbox in inboxes:
                    iid = inbox.get("id") or inbox.get("inbox_id")
                    if not iid:
                        logger.warning(f"Skipping inbox with no ID: {inbox}")
                        continue

                    try:
                        r = await client.request(
                            "DELETE",
                            f"{self.BASE}/v1/inboxes/{iid}",
                            headers=self._headers(),
                            timeout=15,
                        )

                        # OpenMail returns 204 No Content on success
                        if r.status_code == 204 or r.is_success:
                            deleted += 1
                            logger.info(
                                f"Deleted inbox {iid} (HTTP {r.status_code})"
                            )
                        else:
                            logger.warning(
                                f"Failed to delete inbox {iid} "
                                f"(HTTP {r.status_code}): {r.text[:500]}"
                            )
                            failed.append(iid)

                    except __import__("httpx").HTTPError as e:
                        logger.warning(
                            f"DELETE failed for inbox {iid}: {e}"
                        )
                        failed.append(iid)

            logger.info(
                f"Delete phase complete: {deleted}/{len(inboxes)} inbox(es) deleted"
            )

            if failed:
                logger.warning(f"Failed to delete {len(failed)} inbox(es): {failed}")

            # OpenMail deletion is async. Short initial wait.
            logger.info("Waiting 2s for OpenMail to process deletions...")
            await asyncio.sleep(2)

            # Verify the server-side state with quick retries.
            # Total wait: ~12s (2+2+4+4)
            max_attempts = 4
            for attempt in range(1, max_attempts + 1):
                remaining = await self.list_inboxes()

                if not remaining:
                    logger.info(
                        f"✓ Verified account has 0 inboxes after "
                        f"{attempt} verification check(s)"
                    )
                    self.active = []
                    return True

                logger.warning(
                    f"Delete verification {attempt}/{max_attempts}: "
                    f"{len(remaining)} inbox(es) still on API after deletion"
                )

                wait_time = 2 if attempt <= 2 else 4
                logger.info(f"  Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)

            # Final check with verbose output for debugging
            final_check = await self.list_inboxes()
            logger.error(
                f"OpenMail still reports {len(final_check)} inbox(es) after {max_attempts} quick retries (~12s). "
                f"Last DELETE response codes were logged above. "
                "This means deletion is not completing — check the OpenMail API directly."
            )
            self.active = []
            return False

        except Exception as e:
            logger.error(f"nuke_all UNEXPECTED ERROR: {e}")
            raise

    async def create_inbox(self) -> dict:
        """Create a fresh inbox."""
        for attempt in range(1, 4):
            try:
                async with __import__("httpx").AsyncClient() as client:
                    r = await client.post(
                        f"{self.BASE}/v1/inboxes",
                        headers=self._headers(),
                        json={},
                        timeout=15,
                    )

                    if r.is_success:
                        data = r.json()
                        self.active.append(data)
                        logger.info(
                            f"Created inbox successfully on attempt {attempt}"
                        )
                        return data

                    status = r.status_code
                    body = r.text[:500]

                    logger.warning(
                        f"create_inbox attempt {attempt} failed "
                        f"(HTTP {status}): body={body}"
                    )

                    if status == 422 and "inbox_limit_reached" in body:
                        raise RuntimeError(
                            "OpenMail inbox limit reached. The API still "
                            "reports existing inboxes after deletion. "
                            "Run 'create' again after the quota clears, "
                            "or inspect the OpenMail account/API state."
                        )

                    if status == 403:
                        raise RuntimeError(
                            "OpenMail returned HTTP 403 Forbidden while "
                            "creating an inbox."
                        )

                    if status == 429:
                        # Check if it's a hard daily rate limit (won't reset soon)
                        if "rate_limit_exceeded" in body and "Max" in body:
                            raise RuntimeError(
                                f"OpenMail DAILY RATE LIMIT EXCEEDED: {body}. "
                                "Wait 24 hours before trying again, or use a different account."
                            )

                        ra = r.headers.get("Retry-After")
                        retry_after = int(ra) if (ra and ra.isdigit()) else 10
                        # Cap at 60s — don't block for hours on Retry-After
                        retry_after = min(retry_after, 60)

                        logger.warning(
                            f"Rate limited — waiting {retry_after}s (capped, full: {ra})..."
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    # Other 4xx/5xx errors may be transient.
                    if attempt < 3:
                        await asyncio.sleep(5)

            except (__import__("httpx").TimeoutException, __import__("httpx").ConnectError) as e:
                logger.warning(
                    f"create_inbox attempt {attempt} network error: {e}"
                )

                if attempt < 3:
                    await asyncio.sleep(5)

            except RuntimeError:
                raise

            except Exception as e:
                logger.error(
                    f"create_inbox UNEXPECTED ERROR "
                    f"(attempt {attempt}): {e}"
                )
                raise

        raise RuntimeError(
            "OpenMail: could not create inbox after 3 attempts"
        )

    async def get_verification_url(self, inbox_id: str, timeout: int = 180) -> Optional[str]:
        """Poll OpenMail until a message containing an OpenRouter URL arrives."""
        start = time.time()
        seen_ids = set()

        async with __import__("httpx").AsyncClient() as client:
            while time.time() - start < timeout:
                try:
                    r = await client.get(
                        f"{self.BASE}/v1/inboxes/{inbox_id}/messages",
                        headers=self._headers(),
                        timeout=30,
                    )
                    r.raise_for_status()

                    data = r.json()

                    if isinstance(data, list):
                        msgs = data
                    elif isinstance(data, dict):
                        msgs = data.get("data") or data.get("messages") or []
                    else:
                        msgs = []

                    if not isinstance(msgs, list):
                        msgs = []

                    logger.info(
                        f"OpenMail poll: {len(msgs)} message(s) in inbox"
                    )

                    for msg in msgs:
                        if not isinstance(msg, dict):
                            continue

                        msg_id = msg.get("id")
                        if msg_id and msg_id in seen_ids:
                            continue

                        if msg_id:
                            seen_ids.add(msg_id)

                        sender = msg.get("fromAddr", "unknown")
                        subject = msg.get("subject", "(no subject)")
                        status = msg.get("status", "unknown")

                        logger.info(
                            f"Received email: from={sender} "
                            f"subject={subject!r} status={status}"
                        )

                        values = [
                            msg.get("bodyText") or "",
                            msg.get("bodyHtml") or "",
                            msg.get("body") or "",
                            msg.get("html") or "",
                            msg.get("text") or "",
                        ]

                        try:
                            values.append(json.dumps(msg, ensure_ascii=False))
                        except Exception:
                            pass

                        combined = "\n".join(str(v) for v in values if v)
                        combined = html.unescape(combined)
                        combined = combined.replace("\\/", "/")

                        # Normal URL.
                        matches = re.findall(
                            r'https?://[^\s"\'<>]+',
                            combined,
                            re.I,
                        )

                        for url in matches:
                            url = url.rstrip(").,;]}>'\"")
                            if "openrouter.ai" in url.lower():
                                logger.info(
                                    f"Found OpenRouter URL in received email"
                                )
                                print(url)
                                return url

                        # HTML href/src attributes.
                        html_links = re.findall(
                            r'(?:href|src)\s*=\s*["\']([^"\']+)["\']',
                            combined,
                            re.I,
                        )

                        for url in html_links:
                            url = html.unescape(url).replace("\\/", "/")
                            if "openrouter.ai" in url.lower():
                                logger.info(
                                    f"Found OpenRouter HTML link in received email"
                                )
                                print(url)
                                return url

                        logger.info(
                            "Email received, but no OpenRouter URL found "
                            "in this message"
                        )

                except __import__("httpx").HTTPError as e:
                    logger.warning(f"Polling error: {e}")

                except Exception as e:
                    logger.error(
                        f"Polling UNEXPECTED ERROR: {e}"
                    )

                await asyncio.sleep(2)

        return None

    async def delete_inbox(self, inbox_id: str):
        try:
            async with __import__("httpx").AsyncClient() as client:
                r = await client.request(
                    "DELETE",
                    f"{self.BASE}/v1/inboxes/{inbox_id}",
                    headers=self._headers(),
                    timeout=15,
                )

                if r.status_code == 204 or r.is_success:
                    logger.info(
                        f"Burned inbox {inbox_id} (status: {r.status_code})"
                    )
                else:
                    logger.warning(
                        f"Failed to burn inbox {inbox_id} "
                        f"(HTTP {r.status_code}): {r.text[:500]}"
                    )
        except __import__("httpx").HTTPError as e:
            logger.warning(f"Burn HTTP error: {e}")
        except Exception as e:
            logger.error(f"Burn UNEXPECTED ERROR: {e}")
            raise


async def cmd_create():
    load_env()
    keys = get_openmail_api_keys()

    if not keys:
        print("Error: No OPENMAIL_API_KEY found (checked OPENMAIL_API_KEY and OPENMAIL_API_KEY_1..5).", file=sys.stderr)
        sys.exit(1)

    # Try each API key until we find one that can create an inbox
    for i, api_key in enumerate(keys, 1):
        logger.info(f"Trying API key #{i} for inbox creation...")
        mgr = OpenMailManager(api_key)

        try:
            inboxes = await mgr.list_inboxes()
            logger.info(f"Key #{i}: Found {len(inboxes)} existing inbox(es)")

            if len(inboxes) >= 1:
                logger.info("Nuking all inboxes on this account before creating fresh...")

                cleared = await mgr.nuke_all()

                if not cleared:
                    logger.warning(
                        "Could not verify all existing inboxes were deleted. "
                        "Trying next API key..."
                    )
                    continue

                logger.info("Inbox quota cleared — creating fresh inbox...")

            inbox = await mgr.create_inbox()
            inbox_id = inbox.get("id") or inbox.get("inbox_id")

            address = inbox.get("address") or inbox.get("email")
            if not address:
                local = (
                    inbox.get("local_part")
                    or inbox.get("username")
                    or inbox.get("local")
                    or inbox.get("name")
                    or inbox.get("mailboxName")
                )
                domain = inbox.get("domain") or "openmail.sh"
                if not local:
                    logger.error(f"Cannot find local part in response: {inbox}")
                    continue
                address = f"{local}@{domain}"

            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps({"inbox_id": inbox_id, "address": address, "key_index": i}) + "\n",
                encoding="utf-8",
            )
            os.chmod(STATE_FILE, 0o600)

            logger.info(f"Inbox created on key #{i}: {address}")
            print(address)
            return

        except RuntimeError as e:
            if "rate_limit_exceeded" in str(e) or "DAILY RATE LIMIT" in str(e):
                logger.warning(f"Key #{i} is rate limited (daily quota). Trying next key...")
                continue
            else:
                raise
        except Exception as e:
            logger.warning(f"Key #{i} failed: {e}. Trying next key...")
            continue

    logger.error("All API keys exhausted — could not create inbox on any account.")
    sys.exit(1)


async def cmd_check():
    if not STATE_FILE.exists():
        print("Error: No state file. Run 'create' first.", file=sys.stderr)
        sys.exit(1)

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    inbox_id = state["inbox_id"]
    key_index = state.get("key_index")  # Which key was used for this inbox
    load_env()
    keys = get_openmail_api_keys()

    if not keys:
        print("Error: No OPENMAIL_API_KEY found.", file=sys.stderr)
        sys.exit(1)

    poll_timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    # Build key list: try the one that created this inbox first, then others
    ordered_keys = []
    if key_index and 1 <= key_index <= len(keys):
        ordered_keys.append((key_index, keys[key_index - 1]))
    for i, key in enumerate(keys, 1):
        if i != key_index:
            ordered_keys.append((i, key))

    url = None
    for idx, api_key in ordered_keys:
        logger.info(f"Trying key #{idx} for inbox {inbox_id}...")
        mgr = OpenMailManager(api_key)
        try:
            url = await mgr.get_verification_url(inbox_id, timeout=poll_timeout)
            if url:
                print(url)
                return
        except Exception as e:
            logger.warning(f"Key #{idx} failed for inbox check: {e}")
            continue

    print("Error: Timed out waiting for verification URL on all keys.", file=sys.stderr)
    sys.exit(1)


async def _get_working_key_or_exit():
    """Try all available API keys, return first working one."""
    load_env()
    keys = get_openmail_api_keys()

    if not keys:
        logger.error("No OPENMAIL_API_KEY found (checked OPENMAIL_API_KEY and OPENMAIL_API_KEY_1..5).")
        sys.exit(1)

    for i, key in enumerate(keys, 1):
        logger.info(f"Trying API key #{i}...")
        mgr = OpenMailManager(key)
        inboxes = await mgr.list_inboxes()
        logger.info(f"Key #{i} works — {len(inboxes)} inbox(es) on account.")
        return key

    logger.error("None of the configured OpenMail API keys are valid.")
    sys.exit(1)


async def cmd_burn():
    api_key = await _get_working_key_or_exit()

    # Still clean up local state file even if no working key
    if STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
            logger.info("State file removed.")
        except Exception:
            pass

    mgr = OpenMailManager(api_key)

    # First, nuke ALL existing inboxes on the account (not just state file's)
    logger.info("Nuking all inboxes on the account...")
    try:
        cleared = await mgr.nuke_all()
    except Exception as e:
        logger.error(f"Burn failed — could not communicate with OpenMail API: {e}")
        return  # Don't exit — main.py needs to know burn "completed"

    if not cleared:
        logger.error(
            "Could not verify all inboxes were deleted. "
            "Aborting burn — inbox creation would fail due to quota."
        )
        sys.exit(1)

    logger.info("All inboxes deleted successfully.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: open_email.py [create|check|burn]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "create":
        asyncio.run(cmd_create())
    elif cmd == "check":
        asyncio.run(cmd_check())
    elif cmd == "burn":
        asyncio.run(cmd_burn())
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
