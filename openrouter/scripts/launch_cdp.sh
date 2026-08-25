#!/bin/bash
# Managed Chrome CDP launcher (ephemeral profile) — Linux only
# Uses xvfb-run for clean Xvfb management.
# All paths are project-local (no /tmp for PID/port files).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
RUN_DIR="$PROJECT_ROOT/run"
DATA_DIR="$PROJECT_ROOT/data"
STEALTH_DIR="$PROJECT_ROOT/stealth-extension"

mkdir -p "$RUN_DIR" "$DATA_DIR"

# Every browser launch gets its own disposable profile.
PROFILE_DIR="$(mktemp -d "$RUN_DIR/chrome-profile.XXXXXX")"

cleanup() {
    rm -rf -- "$PROFILE_DIR" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

# CDP port: use env var or find a free one
if [[ -n "${CDP_PORT:-}" ]]; then
    echo "Using CDP_PORT from environment: $CDP_PORT"
else
    CDP_PORT=9333
fi

CDP_HOST="${CDP_HOST:-127.0.0.1}"
CDP_PORT_FILE="$RUN_DIR/cdp_port.txt"
CDP_PID_FILE="$RUN_DIR/cdp_pids.txt"

# Find Chrome binary
CHROME="${CHROME_BIN:-}"
if [[ -z "$CHROME" ]]; then
    for candidate in /usr/bin/google-chrome /usr/bin/chromium /usr/bin/chromium-browser; do
        if [[ -x "$candidate" ]]; then
            CHROME="$candidate"
            break
        fi
    done
fi
if [[ -z "$CHROME" ]]; then
    echo "ERROR: Chrome/Chromium not found. Install google-chrome-stable or set CHROME_BIN." >&2
    exit 1
fi

echo "=== Starting Chrome CDP ==="
echo "Chrome: $CHROME"

# Write port file so signup_automation.py can find us
echo "$CDP_PORT" > "$CDP_PORT_FILE"
chmod 600 "$CDP_PORT_FILE"

# Display policy:
#   CDP_HEADLESS=1       -> always headless
#   existing DISPLAY=:1  -> headed, visible through noVNC if attached
#   no usable X display  -> headless
#
# noVNC is deliberately NOT required or managed by this script.
CDP_HEADLESS="${CDP_HEADLESS:-}"
CDP_DISPLAY="${CDP_DISPLAY:-:1}"
USE_HEADLESS=false

if [[ "$CDP_HEADLESS" =~ ^(1|true|yes|on)$ ]]; then
    USE_HEADLESS=true
elif [[ -n "$CDP_DISPLAY" ]] && command -v xdpyinfo >/dev/null 2>&1; then
    if DISPLAY="$CDP_DISPLAY" xdpyinfo >/dev/null 2>&1; then
        export DISPLAY="$CDP_DISPLAY"
        echo "X display available: $DISPLAY"
        echo "Chrome will run headed (noVNC may be used as an optional viewer)"
    else
        USE_HEADLESS=true
    fi
else
    USE_HEADLESS=true
fi

if "$USE_HEADLESS"; then
    unset DISPLAY
    echo "No usable X display — Chrome will run headless"
else
    echo "Using headed Chrome on DISPLAY=$DISPLAY"
fi

# Build Chrome flags
CHROME_FLAGS=(
    --remote-debugging-port="$CDP_PORT"
    --remote-debugging-address="$CDP_HOST"
    --user-data-dir="$PROFILE_DIR"
    --no-sandbox
    --disable-dev-shm-usage
    --no-first-run
    --no-default-browser-check
    
    # === STEALTH & ANTI-BOT ===
    --disable-blink-features=AutomationControlled
    --enable-features=ClipboardAPI
    --password-store=basic
    --use-mock-keychain
    --ignore-gpu-blocklist
    --enable-unsafe-swiftshader
    --enable-webgl
    --enable-gpu-rasterization
    
    # Prevent detection
    --disable-web-resources
    --disable-extensions-except="$STEALTH_DIR"
    --load-extension="$STEALTH_DIR"
    --disable-plugins
    --disable-images
    --disable-component-extensions-with-background-pages
    
    # Spoof user agent and platform
    --user-agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    
    # Disable WebDriver detection
    --disable-component-update
    --disable-default-apps
    --no-default-browser-check
    --no-pings
    
    # Turnstile & hCaptcha friendly settings
    --enable-features=IsolatedSandbox
    --disable-sync
    --disable-translate
    --disable-breakpad
    --disable-client-side-phishing-detection
    --disable-component-update
    --disable-hang-monitor
    --disable-popup-blocking
    --disable-prompt-on-repost
    --no-service-autorun
    
    # Display settings
    --window-size=1920,1080
    --start-maximized
    --lang=en-GB
    --timezone=Europe/London
    
    # Start page
    about:blank
)

if "$USE_HEADLESS"; then
    CHROME_FLAGS+=(--headless=new)
fi

# Attach stealth extension if present
if [[ -d "$STEALTH_DIR" ]]; then
    CHROME_FLAGS+=(--disable-extensions-except="$STEALTH_DIR" --load-extension="$STEALTH_DIR")
fi

# Kill anything already on the CDP port
fuser -k "$CDP_PORT/tcp" >/dev/null 2>&1 || true
sleep 1

# Launch Chrome.
# We NEVER create, own, or kill Xvfb here.
# If :1 exists, Chrome uses it. Otherwise Chrome is headless.
"$CHROME" "${CHROME_FLAGS[@]}" >/dev/null 2>&1 &
CHROME_PID=$!

echo "$CHROME_PID" > "$CDP_PID_FILE"
chmod 600 "$CDP_PID_FILE"

echo "Waiting for CDP..."

for ((i=0; i<60; i++)); do
    if curl -fs "http://${CDP_HOST}:${CDP_PORT}/json/version" >/dev/null 2>&1; then
        echo
        echo "=================================="
        echo "CDP Ready"
        echo "Endpoint : http://${CDP_HOST}:${CDP_PORT}"
        echo "Profile  : $PROFILE_DIR (disposable)"
        echo "Chrome   : $CHROME_PID"
        if "$USE_HEADLESS"; then
            echo "Mode     : headless"
        else
            echo "Mode     : headed ($DISPLAY)"
        fi
        echo "=================================="

        wait "$CHROME_PID" 2>/dev/null || true
        exit 0
    fi

    if ! kill -0 "$CHROME_PID" 2>/dev/null; then
        echo "ERROR: Chrome exited before CDP became ready." >&2
        exit 1
    fi

    sleep 0.5
done

echo "Chrome failed to expose CDP." >&2
exit 1
