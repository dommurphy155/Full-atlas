#!/usr/bin/env python3
"""
AgentMail API wrapper for HuggingFace email verification.
Usage:
  python3 agentmail.py create  -> Nukes all existing inboxes, creates fresh one, prints email.
  python3 agentmail.py check   -> Polls the last created inbox for HuggingFace verification URL.
  python3 agentmail.py burn    -> Deletes the inbox and clears state file.
"""

import asyncio
import re
import time
import os
import sys
import json
import importlib.util
from pathlib import Path
from typing import Optional
import html

# Import config for paths and shared dependency helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import ROOT_DIR, DATA_DIR, STATE_FILE, ENV_FILE, AGENTMAIL_API_KEY, AGENTMAIL_BASE, is_py_module_available, install_py_module

# --- Python package dependency checks (system-wide via importlib.util.find_spec) ---
for module_name, pip_name in [("dotenv", "python-dotenv"), ("httpx", "httpx")]:
    if not is_py_module_available(module_name):
        print(f"Error: {pip_name} not installed. Installing now ...", file=sys.stderr)
        install_py_module(module_name, pip_name)
    if not is_py_module_available(module_name):
        print(f"Error: {pip_name} not installed. Run: pip install {pip_name}", file=sys.stderr)
        sys.exit(1)

from dotenv import load_dotenv
import httpx


class AgentMailManager:
    BASE = AGENTMAIL_BASE

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.active: list = []

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def list_inboxes(self) -> list:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.BASE}/inboxes", headers=self._headers(), timeout=30)
                data = r.json()
                return data if isinstance(data, list) else data.get("inboxes", [])
        except Exception as e:
            print(f"[AgentMail] list_inboxes error: {e}", file=sys.stderr)
            return []

    async def nuke_all(self):
        try:
            async with httpx.AsyncClient() as client:
                inboxes = await self.list_inboxes()
                for inbox in inboxes:
                    iid = inbox.get("inbox_id") or inbox.get("id")
                    if iid:
                        await client.delete(f"{self.BASE}/inboxes/{iid}", headers=self._headers(), timeout=30)
                print(f"[AgentMail] Nuked {len(inboxes)} inbox(es)", file=sys.stderr)
        except Exception as e:
            print(f"[AgentMail] Nuke warning: {e}", file=sys.stderr)
        self.active = []

    async def create_inbox(self) -> dict:
        for attempt in range(6):
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.post(f"{self.BASE}/inboxes", headers=self._headers(), timeout=30)
                    r.raise_for_status()
                    data = r.json()
                    self.active.append(data)
                    return data
            except Exception as e:
                print(f"[AgentMail] create_inbox attempt {attempt+1} failed: {e}", file=sys.stderr)
                if "403" in str(e) or "Forbidden" in str(e):
                    print("[AgentMail] 403 hit — nuking all inboxes and waiting 10s…", file=sys.stderr)
                    await self.nuke_all()
                    await asyncio.sleep(10)
                else:
                    await asyncio.sleep(5)
        raise RuntimeError("AgentMail: could not create inbox after 6 attempts")

    async def get_verification_url(self, inbox_id: str, timeout: int = 180) -> Optional[str]:
        start = time.time()
        async with httpx.AsyncClient() as client:
            while time.time() - start < timeout:
                try:
                    r = await client.get(f"{self.BASE}/inboxes/{inbox_id}/messages", headers=self._headers(), timeout=30)
                    data = r.json()
                    msgs = data if isinstance(data, list) else data.get("messages", [])
                    for msg in msgs:
                        msg_id = (msg.get("id") or msg.get("message_id") or msg.get("uid") or msg.get("messageId"))
                        if not msg_id:
                            continue

                        mr = await client.get(f"{self.BASE}/inboxes/{inbox_id}/messages/{msg_id}", headers=self._headers(), timeout=30)
                        body = mr.json().get("body", "") or mr.json().get("html", "") or str(mr.json())
                        body = html.unescape(body)

                        # Look for huggingface.co email confirmation link
                        m = re.search(r'https://huggingface[.]co/email_confirmation/[^\s"\'<>]+', body)
                        if m:
                            return m.group()

                        # Also check for huggingface.co links in href/src attributes
                        m = re.search(r'(?:href|src)=["\']([^"\']*huggingface[.]co[^"\']*)["\']', body, re.I)
                        if m:
                            return m.group(1)
                except Exception:
                    pass
                await asyncio.sleep(6)
        return None

    async def delete_inbox(self, inbox_id: str):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.delete(f"{self.BASE}/inboxes/{inbox_id}", headers=self._headers(), timeout=30)
                print(f"[AgentMail] Burned inbox {inbox_id} (status: {r.status_code})", file=sys.stderr)
        except Exception as e:
            print(f"[AgentMail] Burn error: {e}", file=sys.stderr)


async def cmd_create():
    load_dotenv(ENV_FILE)
    api_key = os.getenv("AGENTMAIL_API_KEY")
    if not api_key:
        print("Error: AGENTMAIL_API_KEY not found in .env", file=sys.stderr)
        sys.exit(1)

    mgr = AgentMailManager(api_key)
    inboxes = await mgr.list_inboxes()
    print(f"[AgentMail] Found {len(inboxes)} existing inbox(es)", file=sys.stderr)

    if len(inboxes) >= 1:
        print("[AgentMail] Nuking all existing inboxes before creating fresh...", file=sys.stderr)
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
            print(f"[AgentMail] ERROR: Cannot find local part in response: {inbox}", file=sys.stderr)
            sys.exit(1)
        address = f"{local}@{domain}"

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"inbox_id": inbox_id, "address": address}) + "\n", encoding="utf-8")

    print(address)


async def cmd_check():
    if not STATE_FILE.exists():
        print("Error: No state file. Run 'create' first.", file=sys.stderr)
        sys.exit(1)

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    inbox_id = state["inbox_id"]
    load_dotenv(ENV_FILE)
    api_key = os.getenv("AGENTMAIL_API_KEY")
    mgr = AgentMailManager(api_key)

    print(f"[AgentMail] Polling inbox {inbox_id} for HuggingFace verification URL...", file=sys.stderr)
    url = await mgr.get_verification_url(inbox_id)
    if url:
        print(url)
    else:
        print("Error: Timed out waiting for verification URL.", file=sys.stderr)
        sys.exit(1)


async def cmd_burn():
    if not STATE_FILE.exists():
        print("[AgentMail] No state file to burn.", file=sys.stderr)
        return

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    load_dotenv(ENV_FILE)
    api_key = os.getenv("AGENTMAIL_API_KEY")
    if not api_key:
        print("Error: AGENTMAIL_API_KEY not found in .env", file=sys.stderr)
        sys.exit(1)

    mgr = AgentMailManager(api_key)
    inbox_id = state.get("inbox_id")
    if inbox_id:
        await mgr.delete_inbox(inbox_id)

    try:
        STATE_FILE.unlink()
        print("[AgentMail] State file removed.", file=sys.stderr)
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: agentmail.py [create|check|burn]", file=sys.stderr)
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
