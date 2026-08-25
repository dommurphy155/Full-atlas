# HuggingFace Signup Automation

Automated creation of HuggingFace accounts and API key generation.

## Quick Start

```bash
cd ~/atlas/huggingface/scripts
python3 main.py
```

First run will auto-install dependencies, set up browsers, and create `.env.example`.
Edit `~/atlas/.env` to add your AgentMail API key, then run again.

## Files

| File | Purpose |
|------|---------|
| `config.py` | Centralized config — all paths, timeouts, constants |
| `main.py` | **Entry point** — orchestrates the full signup flow with 3 retries |
| `agentmail.py` | AgentMail API wrapper (create/check/burn email inboxes) |
| `get_cookie.py` | Extracts hCaptcha cookie via camoufox + Firefox profile |
| `hf_keys.py` | Standalone signup flow (connects to existing CDP) |
| `hf_scheduler.py` | Loop runner — runs main.py repeatedly |
| `launch_cdp.sh` | Launches Chrome with remote debugging on a random port |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for environment variables |

## Requirements

- Ubuntu 24.04 (or similar Linux)
- Python 3.11+
- Google Chrome / Chromium
- Xvfb (auto-started by launch_cdp.sh)
- `fuser` and `pkill` (for process cleanup)

## First Run Setup

On first run, the script will:
1. Create `~/atlas/data/huggingface_data/` directory
2. Install pip dependencies (httpx, python-dotenv, patchright, camoufox, playwright)
3. Install Chromium browser
4. Create `.env.example` template

You need to provide `AGENTMAIL_API_KEY` in `~/atlas/.env`.

## Architecture

```
main.py (entry point)
  ├── first_run_setup()     # install deps, browsers
  ├── full_cleanup()        # nuclear cleanup of stale processes
  ├── launch_cdp()          # spawn Chrome CDP on random port
  ├── run_get_cookie()      # obtain hCaptcha cookie (if needed)
  ├── create_agentmail_email()  # get fresh disposable email
  ├── run_full_signup()     # fill form, verify email, create token
  │     ├── _fill_and_verify()   # form filling
  │     ├── _click_and_verify()  # button clicking
  │     ├── poll_verification_email()  # AgentMail polling
  │     └── _extract_token()     # clipboard + DOM fallback
  └── cleanup (atexit + signal)
```

## Cleanup Guarantees

Every exit path (success, exception, SIGINT, SIGTERM) triggers:
- Kill all Chrome processes by CDP port
- Kill launch_cdp.sh subprocess
- Stop Playwright
- Delete all `/tmp/cdp_browser_profile_*` directories
- Remove all `/tmp/huggingface_cdp_port.txt` and PID files

## Running in Loops

```bash
# Run once
python3 main.py

# Run every 4 minutes forever
python3 hf_scheduler.py --runs 0 --delay 240

# Run 10 times with 30s delay
python3 hf_scheduler.py --runs 10 --delay 30
```
