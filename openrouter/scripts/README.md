# OpenRouter Signup Automation

Automated OpenRouter account signup with real-time key extraction. Creates fresh
disposable email inboxes per run, navigates the signup flow, solves Cloudflare
challenges, clicks the verification link, and extracts the API key from the DOM.

**Linux-only.** Uses a local Chrome instance over CDP (no remote WebDriver).

## Prerequisites

- Debian/Ubuntu (apt-based) — other distros need manual deps
- Google Chrome or Chromium installed
- `xvfb` for headless display (auto-started in `launch_cdp.sh`)

## Quick Start

```bash
# 1. Install system deps
apt-get update -qq && apt-get install -y -qq xvfb curl psmisc

# 2. Install Python deps + Playwright
pip install -r requirements.txt
python3 -m patchright install chromium

# 3. Configure env vars
cp .env.example .env
# Edit .env — set OPENMAIL_API_KEY (required)

# 4. Run once
python3 run_signup.sh

# Or run in a loop (5 runs, 4-min delay between each)
python3 scheduler.py --runs 5 --delay 240
```

## Files

| File | Purpose |
|---|---|
| `main.py` | Orchestrator — boots CDP, creates email, runs signup, burns inbox |
| `signup_automation.py` | Playwright automation: form fill, Cloudflare, key extraction |
| `open_email.py` | OpenMail API wrapper — create/check/burn inboxes |
| `config.py` | All paths, URLs, timeouts, helper functions |
| `launch_cdp.sh` | Chrome CDP launcher with Xvfb management |
| `run_signup.sh` | Wrapper script for `main.py` with unbuffered output |
| `scheduler.py` | Loop runner with configurable runs + delay |

## Environment Variables (`.env`)

| Var | Required | Description |
|---|---|---|
| `OPENMAIL_API_KEY` | **Yes** | OpenMail API key (get from https://openmail.sh) |
| `OPENROUTER_API_KEY` | No | Optional, used for post-signup verification |
| `CHROME_BIN` | No | Custom path to Chrome binary (auto-detected if unset) |
| `CDP_PORT` | No | CDP port (auto-assigned if unset) |
| `CDP_HOST` | No | CDP host, default `127.0.0.1` |

## Output

- API keys are appended to `data/openroute_keys.txt` (one per line)
- Logs: `data/logs/` (`orchestrator.log`, `signup.log`, `openmail.log`)
- Debug screenshots saved to `data/` on failures

## Architecture

```
main.py
  ├── launch_cdp.sh        # Chrome over CDP (random port, Xvfb on :1)
  ├── open_email.py create # Burn old inbox → create fresh inbox
  ├── signup_automation.py # Playwright: fill → Cloudflare → verify → copy key
  └── open_email.py burn   # Burn the inbox
```

## Cloudflare Handling

`signup_automation.py` has a multi-layered Cloudflare bypass:
1. Click checkbox via `frame_locator` on Cloudflare iframe
2. Fallback to `page.evaluate()` with JS selector scan (checks all iframes)
3. After form submit, polls URL for redirect away from `/sign-up`
4. On retry: clears cookies, reloads, re-fills form, re-clicks Continue

## License

MIT
