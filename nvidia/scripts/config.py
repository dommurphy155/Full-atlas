"""
Centralized config for nvidia project.
All paths resolved relative to this file's directory.
No hardcoded absolute paths.
"""
from pathlib import Path
import os
from dotenv import load_dotenv

# Project root (repo root, two levels up from scripts/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ROOT_DIR = PROJECT_ROOT / "nvidia"
DATA_DIR = PROJECT_ROOT / "data" / "nvidia_data"

# Load .env from atlas root (shared env file)
load_dotenv(PROJECT_ROOT / ".env")

# Paths
KEYS_FILE = DATA_DIR / "nvda_keys.txt"
STATE_FILE = DATA_DIR / ".agentmail_state.json"
CAPTCHA_COOKIE_FILE = PROJECT_ROOT / "data" / "captcha_cookie.json"

# Scripts
AGENTMAIL_SCRIPT = ROOT_DIR / "scripts" / "agentmail.py"
GET_COOKIE_SCRIPT = ROOT_DIR / "scripts" / "get_cookie.py"
LAUNCH_CDP_SCRIPT = ROOT_DIR / "scripts" / "launch_cdp.sh"
HF_KEYS_SCRIPT = ROOT_DIR / "scripts" / "hf_keys.py"
ENV_FILE = PROJECT_ROOT / ".env"

# CDP defaults
CDP_HOST = "127.0.0.1"
CDP_PORT_FILE = Path("/tmp/cdp_port.txt")

# AgentMail
AGENTMAIL_API_KEY = os.getenv("AGENTMAIL_API_KEY")
AGENTMAIL_BASE = "https://api.agentmail.to/v0"

# NVIDIA
NVIDIA_LOGIN_URL = "https://build.nvidia.com/explore/discover?modal=signin"
NVIDIA_BASE = "https://build.nvidia.com"
NVIDIA_TOKEN_PREFIX = "nvapi-"
NVIDIA_SETTINGS_URL = "https://build.nvidia.com/settings/api-keys"

# Timeouts (seconds)
CDP_TIMEOUT = 60
SIGNUP_TIMEOUT = 60000  # ms for playwright
EMAIL_POLL_TIMEOUT = 180
EMAIL_POLL_INTERVAL = 1
PAGE_LOAD_TIMEOUT = 60000  # ms
ELEMENT_TIMEOUT = 15000  # ms
VERIFY_CODE_TIMEOUT = 120  # seconds to wait for 6-digit code
CAPTCHA_WAIT = 5

# Password (strong, meets NVIDIA quota requirements)
PASSWORD = "Nvidia2024!SecurePass#7"

# hCaptcha
HCAPTCHA_IFRAME_SELECTOR = "iframe[title*='hcaptcha'], iframe[src*='hcaptcha.com'], iframe[src*='hcaptcha']"
HCAPTCHA_CHECKBOX_SELECTOR = "iframe[title*='hcaptcha'] checkbox, #checkbox", "#hcaptcha-checkbox"
