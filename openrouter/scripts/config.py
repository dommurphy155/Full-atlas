#!/usr/bin/env python3
"""
Configuration for OpenRouter signup automation.
"""

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("/root/atlas/data/openrouter_data")
STATE_FILE = DATA_DIR / ".agentmail_state.json"
ENV_FILE = Path("/root/atlas/.env")
KEYS_FILE = DATA_DIR / "openroute_keys.txt"

AGENTMAIL_API_KEY = os.getenv("AGENTMAIL_API_KEY")
AGENTMAIL_BASE = "https://api.agentmail.to/v0"

VIRTUAL_DISPLAY = ":1"
HEADLESS = False
BROWSER_TIMEOUT = 60000
NAVIGATION_TIMEOUT = 60000

SIGNUP_URL = "https://openrouter.ai/sign-up"


def is_py_module_available(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


def install_py_module(module_name: str, pip_name: str = None):
    pip_name = pip_name or module_name
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])


def ensure_dependencies():
    for module_name, pip_name in [
        ("dotenv", "python-dotenv"),
        ("httpx", "httpx"),
        ("patchright", "patchright"),
        ("rich", "rich"),
    ]:
        if not is_py_module_available(module_name):
            print(f"[Config] Installing {pip_name}...", file=sys.stderr)
            install_py_module(module_name, pip_name)