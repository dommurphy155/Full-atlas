#!/bin/bash
#
# launch_cdp.sh — Launch a headless Chrome instance with remote debugging.
#
# This script is designed to be robust on a clean Linux machine:
#   - Picks a unique random port if CDP_PORT is not set
#   - Creates a unique temp profile dir per run
#   - Starts Xvfb if no X display is available
#   - Cleans up Chrome + Xvfb + temp profile on ANY exit (trap)
#   - Writes the CDP port to /tmp/huggingface_cdp_port.txt for Python to read
#
# Usage:
#   CDP_PORT=9333 DISPLAY=:1 ./launch_cdp.sh   # explicit port
#   ./launch_cdp.sh                            # auto-random port + Xvfb
#
# The CDP endpoint URL will be:
#   http://127.0.0.1:<port>
#
# Exit codes:
#   0  — CDP is ready and Chrome is running
#   1  — Chrome failed to start or CDP never became ready

set -euo pipefail

# --------------------------------------------------------------------------- #
# Helpers                                                                        #
# --------------------------------------------------------------------------- #

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STEALTH_DIR="${PROJECT_ROOT}/data/stealth-extension"
LOG_FILE="${PROJECT_ROOT}/data/huggingface_data/launch_cdp.log"

# Create data dir + log file, never crash
mkdir -p "${PROJECT_ROOT}/data/huggingface_data"
touch "${LOG_FILE}" 2>/dev/null || true

log() {
    echo "[$(date '+%H:%M:%S')] $*" | tee -a "${LOG_FILE}"
}

# --------------------------------------------------------------------------- #
# Port selection                                                                  #
# --------------------------------------------------------------------------- #

if [[ -n "${CDP_PORT:-}" ]]; then
    PORT="${CDP_PORT}"
    log "Using CDP_PORT from environment: ${PORT}"
else
    # Pick a random free port
    PORT=$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('', 0))
print(s.getsockname()[1])
s.close()
" 2>/dev/null)
    if [[ -z "${PORT}" ]]; then
        PORT=9333
    fi
    log "Auto-selected CDP port: ${PORT}"
fi

export CDP_PORT
echo "${PORT}" > /tmp/huggingface_cdp_port.txt

# --------------------------------------------------------------------------- #
# Profile directory — unique per invocation                            #
# --------------------------------------------------------------------------- #

RUN_ID=$(python3 -c "import uuid; print(uuid.uuid4().hex[:8])")
if [[ -n "${HF_PROFILE_SUFFIX:-}" ]]; then
    PROFILE_DIR="/tmp/cdp_browser_profile_${PORT}_${HF_PROFILE_SUFFIX}"
else
    PROFILE_DIR="/tmp/cdp_browser_profile_${PORT}_${RUN_ID}"
fi
mkdir -p "${PROFILE_DIR}"

# Write PID file for external cleanup scripts
PID_FILE="/tmp/cdp_${PORT}_pids.txt"
echo "" > "${PID_FILE}"

# --------------------------------------------------------------------------- #
# Cleanup function — runs on ANY exit                          #
# --------------------------------------------------------------------------- #

CHROME_PID=""
XVFB_PID=""
CLEANUP_DONE=0

cleanup() {
    # Guard against double-cleanup
    if [[ "${CLEANUP_DONE}" -eq 1 ]]; then
        return
    fi
    CLEANUP_DONE=1

    log "Cleaning up..."

    # Kill Chrome by PID
    if [[ -n "${CHROME_PID}" ]]; then
        if kill -0 "${CHROME_PID}" 2>/dev/null; then
            log "Terminating Chrome (PID ${CHROME_PID})"
            kill "${CHROME_PID}" 2>/dev/null || true
            sleep 2
            if kill -0 "${CHROME_PID}" 2>/dev/null; then
                log "Force killing Chrome (PID ${CHROME_PID})"
                kill -9 "${CHROME_PID}" 2>/dev/null || true
            fi
        fi
    fi

    # Kill any remaining chrome processes with our profile dir
    pkill -f "user-data-dir=${PROFILE_DIR}" 2>/dev/null || true
    # Kill chrome with our remote-debugging-port
    pkill -f "remote-debugging-port=${PORT}" 2>/dev/null || true

    # Kill Xvfb if we started it
    if [[ -n "${XVFB_PID}" ]]; then
        if kill -0 "${XVFB_PID}" 2>/dev/null; then
            log "Terminating Xvfb (PID ${XVFB_PID})"
            kill "${XVFB_PID}" 2>/dev/null || true
            sleep 1
            if kill -0 "${XVFB_PID}" 2>/dev/null; then
                kill -9 "${XVFB_PID}" 2>/dev/null || true
            fi
        fi
    fi

    # Remove temp profile dir
    if [[ -d "${PROFILE_DIR}" ]]; then
        rm -rf "${PROFILE_DIR}" 2>/dev/null || true
    fi

    # Remove PID and port files
    rm -f "${PID_FILE}" 2>/dev/null || true
    rm -f /tmp/huggingface_cdp_port.txt 2>/dev/null || true

    log "Cleanup complete."
}

trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------- #
# Kill stale processes on this port                         #
# --------------------------------------------------------------------------- #

log "Pre-flight: clearing stale processes on port ${PORT}"
pkill -f "remote-debugging-port=${PORT}" 2>/dev/null || true
sleep 1

# --------------------------------------------------------------------------- #
# X display setup                                                     #
# --------------------------------------------------------------------------- #

DISPLAY_NUM=1
DISPLAY=":${DISPLAY_NUM}"

if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    log "Using existing X display ${DISPLAY}"
else
    log "Starting Xvfb on ${DISPLAY}"
    Xvfb "${DISPLAY}" -screen 0 1920x1080x24 -ac +extension GLX +render -noreset >/dev/null 2>&1 &
    XVFB_PID=$!
    sleep 2
    if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
        log "ERROR: Xvfb failed to start"
        exit 1
    fi
fi
export DISPLAY

# --------------------------------------------------------------------------- #
# Build Chrome flags                                                            #
# --------------------------------------------------------------------------- #

CHROME="/usr/bin/google-chrome"
if [[ ! -x "${CHROME}" ]]; then
    CHROME="/usr/bin/chromium"
fi
if [[ ! -x "${CHROME}" ]]; then
    CHROME="/usr/bin/chromium-browser"
fi
if [[ ! -x "${CHROME}" ]]; then
    log "ERROR: No Chrome/Chromium binary found"
    exit 1
fi

CHROME_FLAGS=(
    --remote-debugging-port="${PORT}"
    --remote-debugging-address=127.0.0.1
    --remote-allow-origins=*
    --user-data-dir="${PROFILE_DIR}"
    --no-sandbox
    --disable-dev-shm-usage
    --no-first-run
    --no-default-browser-check
    --disable-blink-features=AutomationControlled
    --password-store=basic
    --use-mock-keychain
    --ignore-gpu-blocklist
    --enable-unsafe-swiftshader
    --enable-webgl
    --enable-gpu-rasterization
    --auto-dark-mode=Disabled
    --unsafely-treat-insecure-origin-as-secure=http://127.0.0.1
    --window-size=1920,1080
    --start-maximized
    --lang=en-GB
    --allow-clipboard-apis
    --clipboard-backend=sync
)

# Attach stealth extension if it exists
if [[ -d "${STEALTH_DIR}" ]]; then
    CHROME_FLAGS+=(
        --disable-extensions-except="${STEALTH_DIR}"
        --load-extension="${STEALTH_DIR}"
    )
fi

CHROME_FLAGS+=(about:blank)

# --------------------------------------------------------------------------- #
# Launch Chrome                                                     #
# --------------------------------------------------------------------------- #

log "Launching Chrome..."
log "  Port:   ${PORT}"
log "  Display: ${DISPLAY}"
log "  Profile: ${PROFILE_DIR}"

"${CHROME}" "${CHROME_FLAGS[@]}" >/dev/null 2>&1 &
CHROME_PID=$!

# Save PIDs immediately
echo "$(if [[ -n "${XVFB_PID}" ]]; then echo "${XVFB_PID}"; fi) ${CHROME_PID}" > "${PID_FILE}"

# --------------------------------------------------------------------------- #
# Wait for CDP to be ready                                                 #
# --------------------------------------------------------------------------- #

log "Waiting for CDP to become ready..."
READY=0
for i in $(seq 1 60); do
    if curl -fs "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
        READY=1
        break
    fi
    # Check if Chrome died
    if ! kill -0 "${CHROME_PID}" 2>/dev/null; then
        log "ERROR: Chrome process died before CDP was ready"
        exit 1
    fi
    sleep 0.5
done

if [[ "${READY}" -ne 1 ]]; then
    log "ERROR: CDP did not become ready within 30 seconds"
    exit 1
fi

log "==================================="
log "CDP Ready"
log "  Endpoint:  http://127.0.0.1:${PORT}"
log "  Display:   ${DISPLAY}"
log "  Profile:   ${PROFILE_DIR}"
log "  Chrome PID: ${CHROME_PID}"
if [[ -n "${XVFB_PID}" ]]; then
    log "  Xvfb PID:  ${XVFB_PID}"
fi
log "==================================="

log "CDP ready. Keeping Chrome alive..."

# Keep the script alive — Chrome runs as a child process.
# When the parent script receives SIGTERM/SIGINT, the trap fires and cleanup runs.
while kill -0 "${CHROME_PID}" 2>/dev/null; do
    sleep 5
done

log "Chrome process exited."
exit 1
