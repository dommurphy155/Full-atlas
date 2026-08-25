# Atlas

One command to manage a monetization proxy, automated key generation, and a prompt-pack harness — unified.

Atlas provides:

- **Atlas Proxy** — a FastAPI reverse proxy that translates OpenAI / Anthropic API calls to OpenRouter, with key rotation, cooldown management, and SSE streaming.
- **Provider automation** — headless browser bots that automatically sign up for free API keys from OpenRouter, NVIDIA, and HuggingFace using AgentMail for email verification.
- **Atlas CLI** — a single `atlas` command to start/stop services, manage keys, run diagnostics, and launch provider automation.
- **Prompt Pack** — 15 project briefs for AI coding harnesses.

---

## Quick Start

```bash
atlas status        # Check proxy + automation status
atlas start         # Start the proxy (systemd)
atlas logs          # Follow proxy logs
atlas doctor        # Run diagnostics
atlas keys          # Show key counts per provider
```

---

## Requirements

- Python 3.10+ (3.12 tested)
- Google Chrome / Chromium (only needed for the signup automation)
- Linux or macOS. Windows is not supported (use WSL2).
- AgentMail API key (optional — only for automated account creation)
- systemd optional (Linux) — Atlas runs services as supervised daemons by default

---

## Installation

One command:

```bash
curl -fsSL <host>/install.sh | bash
```

Or from a downloaded/cloned copy of this repository:

```bash
./install.sh
```

The installer:
1. Detects your OS (Windows is rejected cleanly)
2. Creates the virtualenv and installs dependencies in the background
3. Asks which harness you use — Claude Code, Codex, or Hermes — and wires it to the proxy (`ANTHROPIC_BASE_URL`, `~/.codex/config.toml`, or `hermes config set` respectively)
4. Recommends pasting one OpenRouter API key from your main account so the proxy works even if the signup bots hit issues
5. Starts the proxy as a background daemon and health-checks it
6. Drops you straight into your harness, ready to send a prompt

Manual setup (if you prefer):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # add AGENTMAIL_API_KEY if using the signup bots
.venv/bin/python -m atlas_core.wizard
```

**systemd service (Linux, opt-in):**
   ```bash
   cp atlas/systemd/atlas-proxy.service /etc/systemd/system/
   # Edit placeholders (__ATLAS_USER__, __PROJECT_DIR__, __VENV_PYTHON__, etc.)
   systemctl daemon-reload
   systemctl enable --now atlas-proxy.service
   ```

---

## CLI

The CLI is a standalone executable at `atlas/bin/atlas` (or available on `PATH` after installation).

### Top-Level Commands

| Command              | Description                                                                                   |
| -------------------- | --------------------------------------------------------------------------------------------- |
| `atlas install`      | Interactive install wizard — choose Full / Proxy Only / provider automation / Prompt Pack     |
| `atlas doctor`       | Run diagnostics (Python, project files, venv, systemd, proxy, data dirs)                      |
| `atlas update`       | `git pull` then re-run `atlas install`                                                        |
| `atlas shell`        | Open a shell with Atlas environment variables (`ANTHROPIC_BASE_URL`, `ATLAS_PROXY_URL`, etc.) |
| `atlas status`       | Show status of proxy + all automation processes + key files                                   |
| `atlas logs`         | Show log file locations for all services                                                      |
| `atlas restart`      | Restart proxy only                                                                            |
| `atlas stop`         | Stop proxy only                                                                               |
| `atlas start`        | Start proxy only                                                                              |
| `atlas keys`         | Show key counts for all three providers                                                       |
| `atlas help`         | Print the full help screen                                                                    |
| `atlas test [SUITE]` | Run pytest on a test suite (see Testing)                                                      |

### Component Commands

Each component supports `start | stop | restart | logs | status`, plus provider-specific subcommands:

```
atlas proxy <start|stop|restart|logs|status>
atlas openrouter <start|stop|restart|logs|status|keys|create|import>
atlas nvidia <start|stop|restart|logs|status|keys|create|import>
atlas huggingface <start|stop|restart|logs|status|keys|create|import>
```

- `start` — launches the automation process in the background (`.venv/bin/python scripts/scheduler.py` or `main.py`); or starts the systemd service if installed.
- `stop` — kills the process via `pkill` and/or stops the systemd service.
- `logs` — follows journalctl (if systemd) or `tail -f` the log file.
- `status` — checks if the process is running via `pgrep`, and reports key counts.
- `keys` — shows key count and first 40 chars of each key.
- `create` — runs the signup automation in-process (with a 600s timeout).
- `import` — interactively prompts for a key and appends it to the provider's key file.

### Global "all" Variants

```
atlas start all   # Start proxy + all three automations
atlas stop all
atlas restart all
atlas status all
atlas logs all
```

### Environment Variables (CLI)

| Variable          | Default                 | Description                                     |
| ----------------- | ----------------------- | ----------------------------------------------- |
| `ATLAS_PROXY_URL` | `http://127.0.0.1:8788` | Base URL for health checks and status display   |
| `ATLAS_SERVICE`   | `atlas-proxy.service`   | Default systemd service name for proxy commands |
| `ATLAS_ROOT`      | (auto-detected)         | Project root, set in `atlas shell`              |

---

## Atlas Proxy

### Overview

The proxy (`proxy/`) is a FastAPI application that sits between an AI coding harness (Claude Code, Codex, etc.) and OpenRouter. It accepts both OpenAI-compatible and Anthropic-compatible request shapes and forwards them to OpenRouter's API using automatically-rotated API keys.

### Running

```bash
# Direct (development)
.venv/bin/python -m proxy.main

# systemd (production)
systemctl start atlas-proxy
```

Listen address defaults to `0.0.0.0:8788` (see Configuration below).

### Endpoints

| Method | Path                   | Description                                                             |
| ------ | ---------------------- | ----------------------------------------------------------------------- |
| `GET`  | `/`                    | Service info, version, loaded key count, endpoint list                  |
| `GET`  | `/health`              | Health check — key pool stats, listen address                           |
| `GET`  | `/health/keys`         | Detailed per-key stats (state, latency, cooldown, errors)               |
| `GET`  | `/stats`               | Legacy key summary (total/healthy/cooling/suspended)                    |
| `GET`  | `/v1/models`           | Anthropic-shaped model list (stub models for Claude Code UI)            |
| `POST` | `/v1/chat/completions` | OpenAI chat completions → OpenRouter chat                               |
| `POST` | `/v1/messages`         | Anthropic messages → OpenRouter messages                                |
| `POST` | `/v1/responses`        | OpenAI Responses API → OpenRouter chat (with SSE streaming translation) |
| `WS`   | `/v1/responses`        | WebSocket tunnel for Responses API                                      |

### Key Pool & Rotation

- Keys are loaded from `data/openrouter_data/openroute_keys.txt` (one `sk-or-…` key per line). A fallback file can be set via `FALLBACK_KEY_FILE`.
- The key pool (`proxy/keypool.py`) uses partial-sticky round-robin with per-key concurrency caps.
- On error (429, 503, etc.), a key enters an exponential backoff cooldown (45s base → 300s max). After 8 consecutive errors the key is suspended for 600s.
- Keys are hot-reloaded from disk every 5 seconds without a restart.
- Streaming responses include SSE keepalive frames to prevent client timeouts.

### Protocol Translation

The proxy normalizes between APIs:

- **Anthropic ↔ OpenAI messages**: converts between Anthropic content blocks and OpenAI message arrays (tool_use, tool_result, thinking blocks, image blocks).
- **Responses API ↔ Chat Completions**: translates OpenAI Responses API requests into Chat Completions payloads, and translates the SSE response stream back into Responses events (`response.output_text.delta`, `response.function_call_arguments.delta`, etc.).
- **Tool call promotion**: free-tier models that emit tool calls as plain text are parsed and promoted to native `tool_calls`.
- **Model forcing**: `FORCE_DEFAULT_MODEL` always overrides the requested model to the configured default (`pools/base` free model).

### System Prompt Override

A text file at `data/proxy_data/prompt_override.txt` is injected as the first system message in every request (for Claude Code). Editing this file takes effect within 5 seconds without a restart.

---

## Providers

Each provider lives in its own directory with a `scripts/` subdirectory containing:

- `main.py` — main orchestrator (signup flow)
- `agentmail.py` — AgentMail API wrapper (`create`, `check`, `burn`)
- `config.py` — centralized configuration (paths, timeouts, API URLs)
- `get_cookie.py` — hCaptcha cookie harvester (runs once offline)
- `launch_cdp.sh` — Chrome CDP launcher with Xvfb + stealth extension
- `scheduler.py` — runs `main.py` in a loop (`--runs 0` = forever)
- `hf_keys.py` — standalone signup script (HuggingFace/NVIDIA only; legacy)
- `run_signup.sh` — Bash master orchestrator (OpenRouter only)

### Shared Infrastructure

- **AgentMail API** — Disposable email service for sign-up verification. API key stored in `.env` as `AGENTMAIL_API_KEY`.
- **hCaptcha cookies** — Stored at `data/captcha_cookie.json`. Generated by `get_cookie.py` or `add_captcha_account.py`, then injected into the browser context during automation.
- **Browser profiles** — Persisted Firefox/Chrome profiles under `data/firefox_profiles/` for hCaptcha cookie harvesting.
- **Stealth extension** — A Chrome extension at `data/stealth-extension/` that hides automation fingerprints. Loaded into every Chrome instance via `--load-extension`.
- **CDP** — Chrome DevTools Protocol over `DISPLAY=:1`. Port written to `/tmp/cdp_port.txt`.

### OpenRouter

| Detail       | Value                                                                  |
| ------------ | ---------------------------------------------------------------------- |
| Directory    | `openrouter/`                                                          |
| Keys file    | `data/openrouter_data/openroute_keys.txt`                              |
| Key prefix   | `sk-or-`                                                               |
| Email script | `openrouter/scripts/open_email.py` (OpenMail API)                      |
| Scheduler    | `openrouter/scripts/scheduler.py` → `main.py` → `signup_automation.py` |
| Service      | `openrouter-signup.service`                                            |
| Signup flow  | OpenRouter sign-up form → email verification → API key creation        |

### NVIDIA

| Detail       | Value                                                                             |
| ------------ | --------------------------------------------------------------------------------- |
| Directory    | `nvidia/`                                                                         |
| Keys file    | `data/nvidia_data/nvda_keys.txt`                                                  |
| Key prefix   | `nvapi-`                                                                          |
| Email script | `nvidia/scripts/agentmail.py` (AgentMail API)                                     |
| Scheduler    | `nvidia/scripts/main.py` (runs directly)                                          |
| Service      | `nvidia-automation.service`                                                       |
| Signup flow  | build.nvidia.com → 6-digit email verification → org creation → API key generation |

### HuggingFace

| Detail       | Value                                                     |
| ------------ | --------------------------------------------------------- |
| Directory    | `huggingface/`                                            |
| Keys file    | `data/huggingface_data/hf_keys.txt`                       |
| Key prefix   | `hf_`                                                     |
| Email script | `huggingface/scripts/agentmail.py` (AgentMail API)        |
| Scheduler    | `huggingface/scripts/hf_scheduler.py` → `main.py`         |
| Service      | `huggingface-automation.service`                          |
| Signup flow  | huggingface.co/join → email verification → token creation |

---

## Data & Runtime Files

All runtime data is centralized under `data/`:

```
data/
├── captcha_cookie.json        # hCaptcha session cookies (JSON)
├── firefox_profiles/          # Persisted browser profiles for cookie harvesting
│   └── firefox/               # Firefox profile directories
├── stealth-extension/         # Chrome extension loaded by all provider automation
│   ├── manifest.json
│   ├── background.js
│   └── content.js
├── openrouter_data/
│   ├── openroute_keys.txt     # OpenRouter API keys (sk-or-…)
│   ├── .agentmail_state.json  # AgentMail/OpenMail inbox state
│   ├── orchestrator.log       # Automation log
│   ├── agentmail.log          # Email API log
│   └── signup.log             # Signup automation log
├── nvidia_data/
│   ├── nvda_keys.txt          # NVIDIA API keys (nvapi-…)
│   └── .agentmail_state.json  # AgentMail inbox state
├── huggingface_data/
│   ├── hf_keys.txt            # HuggingFace API keys (hf_…)
│   ├── .agentmail_state.json  # AgentMail inbox state
│   ├── orchestrator.log       # Automation log
│   ├── agentmail.log          # Email API log
│   └── signup.log             # Signup automation log
└── proxy_data/
    └── prompt_override.txt    # System prompt override (injected into every request)
```

Key files are created with mode `0600`. The `captcha_cookie.json` file is the shared cookie snapshot injected by `get_cookie.py` and consumed by all three provider automations.

---

## systemd Services

Service templates live in `atlas/systemd/`. They use `__VAR__` placeholders that are substituted at install time:

| File                             | Service name                     | ExecStart                                                                                  |
| -------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------ |
| `atlas-proxy.service`            | `atlas-proxy.service`            | `__VENV_PYTHON__ -m proxy.main`                                                            |
| `openrouter-signup.service`      | `openrouter-signup.service`      | `__VENV_PYTHON__ __PROJECT_DIR__/openrouter/scripts/scheduler.py --runs 0 --delay 240`     |
| `nvidia-automation.service`      | `nvidia-automation.service`      | `__VENV_PYTHON__ __PROJECT_DIR__/nvidia/scripts/main.py`                                   |
| `huggingface-automation.service` | `huggingface-automation.service` | `__VENV_PYTHON__ __PROJECT_DIR__/huggingface/scripts/hf_scheduler.py --runs 0 --delay 240` |

All automation services run on `DISPLAY=:1`, use the shared venv, and have resource limits (2GB RAM, 200% CPU). They auto-restart on failure.

---

## Configuration

### Environment Variables

**Root `.env`** (shared across all components):

| Variable            | Description                                                           |
| ------------------- | --------------------------------------------------------------------- |
| `AGENTMAIL_API_KEY` | AgentMail API key for email verification (used by NVIDIA/HuggingFace) |
| `ATLAS_PROXY_URL`   | Proxy base URL — used by the CLI for health checks                    |

**Proxy (`proxy/config.py`)** — all env-overridable:

| Variable                                                        | Default                                          | Description                                     |
| --------------------------------------------------------------- | ------------------------------------------------ | ----------------------------------------------- |
| `LISTEN_HOST`                                                   | `0.0.0.0`                                        | Proxy listen address                            |
| `LISTEN_PORT`                                                   | `8788`                                           | Proxy listen port                               |
| `ATLAS_OPENROUTER_BASE_URL` / `OPENROUTER_BASE_URL`             | `https://openrouter.ai/api/v1`                   | OpenRouter API base                             |
| `ATLAS_OPENROUTER_MODEL` / `OPENROUTER_MODEL`                   | `pools/nvidia/nv-embed-qa-e5-v5:free` (see note) | Default model injected into all requests        |
| `FORCE_DEFAULT_MODEL`                                           | `false`                                          | If true, always override client-specified model |
| `ATLAS_OPENROUTER_KEYS_FILE` / `KEY_FILE` / `FALLBACK_KEY_FILE` | `data/openrouter_data/openroute_keys.txt`        | Key file path                                   |
| `COOLDOWN_BASE_SECONDS` / `ATLAS_PROXY_COOLDOWN_SECONDS`        | `45`                                             | Base cooldown after error                       |
| `COOLDOWN_MAX_SECONDS`                                          | `300`                                            | Max cooldown                                    |
| `MAX_CONSECUTIVE_ERRORS` / `ATLAS_PROXY_MAX_ERRORS`             | `8`                                              | Errors before key suspension                    |
| `SUSPEND_SECONDS` / `ATLAS_PROXY_SUSPEND_SECONDS`               | `600`                                            | Suspension duration                             |
| `MAX_RETRIES` / `ATLAS_PROXY_MAX_RETRIES`                       | `5`                                              | Upstream retry count                            |
| `READ_TIMEOUT` / `ATLAS_PROXY_READ_TIMEOUT`                     | `600`                                            | Read timeout (long agent turns)                 |
| `SYSTEM_PROMPT_OVERRIDE_ENABLED`                                | `true`                                           | Enable system prompt override                   |
| `SYSTEM_PROMPT_OVERRIDE_FILE`                                   | `data/proxy_data/prompt_override.txt`            | Path to override file                           |
| `CORS_ORIGINS`                                                  | `*`                                              | Comma-separated CORS origins                    |
| `LOG_LEVEL`                                                     | `INFO`                                           | Logging level                                   |
| `LOG_JSON`                                                      | `false`                                          | JSON log format                                 |

**Provider configs** (`*/scripts/config.py`):

- Each provider loads `PROJECT_ROOT/.env` (or `openrouter/.env` for OpenRouter).
- AgentMail API endpoint: `https://api.agentmail.to/v0` (NVIDIA/HuggingFace).
- OpenMail API endpoint: `https://api.openmail.sh` (OpenRouter — transitional, see Notes).

---

## Prompt Pack

15 project briefs across three difficulty tiers, located in `prompt_pack/`:

- `beginner/` (5) — personal dashboard, markdown knowledge base, weather dashboard, AI notes app, personal portfolio
- `intermediate/` (5) — Discord bot, email assistant, finance dashboard, document search, image organiser
- `professional/` (5) — AI SaaS starter kit, multi-provider LLM gateway, team knowledge platform, workflow automation engine, AI research agent

Each brief is a self-contained markdown document with objectives, features, technical suggestions, stretch goals, learning outcomes, and AI instructions.

---

## Testing

The CLI exposes `atlas test [suite]` with the following suites configured in the `TEST_SUITES` dictionary:

| Suite             | Path                       |
| ----------------- | -------------------------- |
| `atlas` / `proxy` | `tests/proxy_tests/`       |
| `openrouter`      | `tests/openrouter_tests/`  |
| `nvidia`          | `tests/nvidia_tests/`      |
| `huggingface`     | `tests/huggingface_tests/` |
| `installer`       | `tests/installer/`         |
| `integration`     | `tests/integration/`       |
| `setup`           | `tests/setup/`             |

Run all: `atlas test all` (or just `atlas test`).

> **Note:** The `tests/` directory does not currently exist in the repository. The test command is wired in the CLI but no test files are present yet.

---

## Troubleshooting

### Proxy won't start

```bash
atlas doctor                # Check Python, venv, systemd, proxy, data dirs
atlas proxy status          # Health check on http://127.0.0.1:8788/health
```

Common causes:

- No `.venv` — run `atlas install`
- No keys in `data/openrouter_data/openroute_keys.txt` — the proxy starts but returns 503 on all requests
- Port 8788 in use — override with `LISTEN_PORT`

### Provider automation won't start

- Ensure `DISPLAY=:1` is running (Xvfb): `xdpyinfo -display :1`
- Check the stealth extension exists at `data/stealth-extension/`
- Verify `hCaptcha` cookies at `data/captcha_cookie.json`
- Check logs: `journalctl -u nvidia-automation -f` (or openrouter-signup / huggingface-automation)

### hCaptcha cookie missing

```bash
.venv/bin/python -m setup.setup_all.add_captcha_account
```

This launches an interactive Chrome session for you to authenticate to hCaptcha. The resulting profile is saved to `data/firefox_profiles/firefox/`.

---

## Repository Structure

```
atlas/
├── atlas/
│   ├── bin/
│   │   └── atlas                  # Unified CLI (v2.0.0, standalone executable)
│   └── systemd/
│       ├── atlas-proxy.service
│       ├── huggingface-automation.service
│       ├── nvidia-automation.service
│       └── openrouter-signup.service
├── proxy/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app + uvicorn entrypoint
│   ├── config.py                  # All env-overridable configuration
│   ├── proxy.py                   # ProxyCore — HTTP forwarding, SSE streaming
│   ├── routes.py                  # Route handlers + protocol translation
│   ├── keypool.py                 # KeyPool — round-robin, cooldown, suspension
│   ├── translation.py             # OpenAI ↔ Anthropic ↔ OpenRouter
│   ├── system_prompt.py           # System prompt override injection
│   ├── logger.py                  # Structured logging with request IDs
│   └── utils.py                   # JSON helpers, request ID extraction
├── huggingface/
│   └── scripts/
│       ├── agentmail.py           # AgentMail API wrapper
│       ├── config.py              # HF config + paths
│       ├── get_cookie.py          # hCaptcha cookie harvester
│       ├── hf_keys.py             # Standalone signup (legacy)
│       ├── hf_scheduler.py        # Scheduler loop
│       ├── launch_cdp.sh          # Chrome CDP launcher
│       └── main.py                # Signup orchestrator
├── nvidia/
│   └── scripts/
│       ├── agentmail.py           # AgentMail API wrapper
│       ├── config.py              # NVIDIA config + paths
│       ├── get_cookie.py          # hCaptcha cookie harvester
│       ├── hf_keys.py             # (copy from HuggingFace — legacy)
│       ├── launch_cdp.sh          # Chrome CDP launcher
│       └── main.py                # Signup orchestrator
├── openrouter/
│   ├── .env                       # AGENTMAIL_API_KEY (shared root key)
│   └── scripts/
│       ├── agentmail.py           # AgentMail API wrapper (legacy/transitional)
│       ├── config.py              # OpenRouter config + paths
│       ├── launch_cdp.sh          # Chrome CDP launcher
│       ├── main.py                # Signup orchestrator
│       ├── open_email.py          # OpenMail API wrapper (primary)
│       ├── run_signup.sh          # Bash master orchestrator
│       ├── scheduler.py          # Scheduler loop
│       └── signup_automation.py   # Signup flow
├── setup/
│   └── setup_all/
│       └── add_captcha_account.py # Interactive hCaptcha profile creator
├── prompt_pack/
│   ├── README.md
│   ├── beginner/                  # 5 project briefs
│   ├── intermediate/              # 5 project briefs
│   └── professional/              # 5 project briefs
├── data/
│   ├── captcha_cookie.json        # hCaptcha session cookies
│   ├── firefox_profiles/          # Browser profiles
│   ├── stealth-extension/         # Chrome extension (manifest, background.js, content.js)
│   ├── openrouter_data/           # Keys, state, logs
│   ├── nvidia_data/               # Keys, state
│   ├── huggingface_data/          # Keys, state, logs
│   └── proxy_data/                # Prompt override
├── .env                           # AGENTMAIL_API_KEY, ATLAS_PROXY_URL
├── .venv/                         # Shared Python virtualenv
└── README.md
```

---

## Design Principles

- **Single entry point**: `atlas` is the one command; everything flows through it.
- **Shared virtualenv**: one `.venv` at the repo root for all components.
- **Portable paths**: all paths are resolved relative to the project root at runtime — no hardcoded `/root/` or absolute paths in source.
- **Centralized data**: all runtime data (keys, cookies, profiles, logs) lives under `data/`.
- **Shared stealth extension**: one Chrome extension at `data/stealth-extension/` loaded by every provider's CDP launch.
- **Template systemd units**: `__VAR__` placeholders are substituted at install time.
