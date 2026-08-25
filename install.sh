#!/usr/bin/env bash
# Atlas — one-shot installer (Linux / macOS).
#
#   curl -fsSL <host>/install.sh | bash     (when hosted)
#   ./install.sh                            (from the bundle)
#
# Flow: OS gate -> venv -> deps (background) -> interactive wizard ->
#       proxy + automations up -> drops the user into their harness.
set -euo pipefail

# --------------------------------------------------------------------------
# OS gate — Windows is not supported, full stop.
# --------------------------------------------------------------------------
case "$(uname -s)" in
    Linux*) OS=linux ;;
    Darwin*) OS=macos ;;
    *) echo "Atlas supports Linux and macOS only." >&2
       case "$(uname -s)" in
           MINGW*|MSYS*|CYGWIN*)
               echo "On Windows, use WSL2: https://learn.microsoft.com/en-us/windows/wsl/install" >&2 ;;
       esac
       exit 1 ;;
esac

# Resolve project root. Piped via curl (no repo on disk)? Clone first.
REPO_URL="${ATLAS_REPO_URL:-https://github.com/dommurphy155/Full-atlas.git}"
if [ -n "${ATLAS_INSTALL_SRC:-}" ]; then
    PROJECT_ROOT="$ATLAS_INSTALL_SRC"
else
    SCRIPT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || SCRIPT_SRC=""
    if [ -n "$SCRIPT_SRC" ] && [ -f "$SCRIPT_SRC/proxy/main.py" ]; then
        PROJECT_ROOT="$SCRIPT_SRC"          # running from inside the repo
    elif [ -f "./proxy/main.py" ] && [ -f "./install.sh" ]; then
        PROJECT_ROOT="$(pwd)"               # running as ./install.sh in the repo
    else
        # Piped via curl | bash — fetch the repo into ~/atlas
        PROJECT_ROOT="$HOME/atlas"
        echo "==> Fetching Atlas into $PROJECT_ROOT..."
        if [ -d "$PROJECT_ROOT/.git" ]; then
            git -C "$PROJECT_ROOT" pull --ff-only || true
        elif command -v git >/dev/null 2>&1 && [ "${ATLAS_NO_CLONE:-}" != "1" ]; then
            git clone --depth 1 "$REPO_URL" "$PROJECT_ROOT" || {
                echo "ERR: could not clone $REPO_URL" >&2
                echo "If the repo is private, download+unzip it and run ./install.sh instead." >&2
                exit 1
            }
        else
            echo "git is required to fetch Atlas. Install it first:" >&2
            [ "$OS" = macos ] && echo "  brew install git" || echo "  apt-get install -y git" >&2
            exit 1
        fi
    fi
fi
cd "$PROJECT_ROOT"

command -v python3 >/dev/null 2>&1 || {
    echo "python3 not found. Install Python 3.10+ first:" 
    [ "$OS" = macos ] && echo "  brew install python" || echo "  apt-get install -y python3 python3-venv"
    exit 1
}

echo "==> Creating virtualenv..."
[ -d .venv ] || python3 -m venv .venv

PIP_LOG="$PROJECT_ROOT/data/logs"
mkdir -p "$PIP_LOG"

echo "==> Installing dependencies in the background (log: data/logs/pip-install.log)..."
# rich is needed by the wizard itself — install it synchronously (tiny).
./.venv/bin/pip install --quiet --upgrade pip >"$PIP_LOG/pip-install.log" 2>&1
./.venv/bin/pip install --quiet rich >>"$PIP_LOG/pip-install.log" 2>&1
# Everything else goes to the background; the wizard waits on it.
./.venv/bin/pip install --quiet -r requirements.txt >>"$PIP_LOG/pip-install.log" 2>&1 &
PIP_PID=$!

# Deps finish inside the wizard (it waits on this PID and shows the log on
# failure). Hand the PID over so state survives across the exec boundary.
export ATLAS_PIP_PID="$PIP_PID"
export ATLAS_PROJECT_ROOT="$PROJECT_ROOT"
export ATLAS_OS="$OS"

exec ./.venv/bin/python -m atlas_core.wizard
