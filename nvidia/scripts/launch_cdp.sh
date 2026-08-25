#!/bin/bash
# Managed Chrome CDP launcher (ephemeral profile)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PROFILE_DIR="$(mktemp -d /tmp/cdp_browser_profile_9333_XXXXXX)"
CDP_HOST="127.0.0.1"

# Use CDP_PORT env var if set (from main.py), otherwise find a free port
if [[ -n "${CDP_PORT:-}" ]]; then
    echo "Using CDP_PORT from environment: $CDP_PORT"
else
    # Find a free port, preferring 9333 then 9444, then any free one
    find_free_port() {
        for port in 9333 9444 9555 9666; do
            if ! fuser "${port}/tcp" >/dev/null 2>&1; then
                echo "$port"
                return
            fi
        done
        # Last resort: let the OS pick
        python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()"
    }
    CDP_PORT=$(find_free_port)
fi
DISPLAY_NUM=1
DISPLAY=":$DISPLAY_NUM"

CHROME="/usr/bin/google-chrome"

XVFB_PID=""
CHROME_PID=""

cleanup() {
    echo
    echo "Stopping Chrome..."

    if [[ -n "${CHROME_PID:-}" ]] && kill -0 "$CHROME_PID" 2>/dev/null; then
        kill "$CHROME_PID" 2>/dev/null || true
        wait "$CHROME_PID" 2>/dev/null || true
    fi

    if [[ -n "${XVFB_PID:-}" ]] && kill -0 "$XVFB_PID" 2>/dev/null; then
        kill "$XVFB_PID" 2>/dev/null || true
    fi

    rm -f /tmp/cdp_9333_pids.txt /tmp/cdp_port.txt

    if [[ -d "$PROFILE_DIR" ]]; then
        echo "Removing temporary profile: $PROFILE_DIR"
        rm -rf "$PROFILE_DIR"
    fi
}

trap cleanup EXIT INT TERM

echo "=== Starting Chrome CDP ==="

export DISPLAY="$DISPLAY"

# Kill anything already listening on the CDP port
fuser -k ${CDP_PORT}/tcp >/dev/null 2>&1 || true
pkill -f "remote-debugging-port=${CDP_PORT}" >/dev/null 2>&1 || true
sleep 1

# Attach to existing Xvfb or start a new one
if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    echo "Using existing X display $DISPLAY"
else
    echo "Starting Xvfb on $DISPLAY..."
    Xvfb "$DISPLAY" -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
    XVFB_PID=$!
    sleep 2
fi

CHROME_FLAGS=(
    --remote-debugging-port=$CDP_PORT
    --remote-debugging-address=$CDP_HOST

    --user-data-dir="$PROFILE_DIR"

    --no-sandbox
    --disable-dev-shm-usage

    --no-first-run
    --no-default-browser-check

    --disable-blink-features=AutomationControlled

    --enable-features=ClipboardAPI

    --password-store=basic
    --use-mock-keychain

    --ignore-gpu-blocklist
    --enable-unsafe-swiftshader
    --enable-webgl
    --enable-gpu-rasterization

    --disable-extensions-except="$PROJECT_ROOT/data/stealth-extension"
    --load-extension="$PROJECT_ROOT/data/stealth-extension"

    --window-size=1920,1080
    --start-maximized

    --lang=en-GB

    about:blank
)

echo "Launching Chrome..."
echo "Temporary profile: $PROFILE_DIR"

"$CHROME" "${CHROME_FLAGS[@]}" >/dev/null 2>&1 &
CHROME_PID=$!

echo "$XVFB_PID $CHROME_PID" >/tmp/cdp_9333_pids.txt
echo "$CDP_PORT" >/tmp/cdp_port.txt

echo "Waiting for CDP..."

for ((i=0;i<60;i++)); do
    if curl -fs "http://${CDP_HOST}:${CDP_PORT}/json/version" >/dev/null 2>&1; then
        echo
        echo "=================================="
        echo "CDP Ready"
        echo "Endpoint : http://${CDP_HOST}:${CDP_PORT}"
        echo "Display  : ${DISPLAY}"
        echo "Profile  : ${PROFILE_DIR}"
        echo "Chrome   : ${CHROME_PID}"
        [[ -n "$XVFB_PID" ]] && echo "Xvfb     : ${XVFB_PID}"
        echo "=================================="

        wait "$CHROME_PID"
        exit 0
    fi

    sleep 0.5
done

echo "Chrome failed to expose CDP."
exit 1
