"""atlas_core.wizard — interactive first-run installer.

Run via install.sh. Flow:
  welcome banner -> wait for background deps -> harness choice ->
  harness config -> recommended OpenRouter key bootstrap ->
  start proxy + automations (background daemons) -> health check ->
  final status + exec into the chosen harness.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from .paths import PROJECT_ROOT, DATA_DIR, atomic_write_600
from .process import _pid_alive
from .service import get_backend, SERVICE_SPECS
from .display import find_chrome

console = Console()
BANNER = r"""
 █████╗ ████████╗██╗      █████╗ ███████╗
██╔══██╗╚══██╔══╝██║     ██╔══██╗██╔════╝
███████║   ██║   ██║     ███████║███████╗
██╔══██║   ██║   ██║     ██╔══██║╚════██║
██║  ██║   ██║   ███████╗██║  ██║███████║
╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝╚══════╝
"""

WELCOME = (
    "Welcome to Atlas.\n\n"
    "Run any AI coding agent — Claude Code, Codex, Hermes — against\n"
    "frontier models with [bold cyan]zero API bills[/bold cyan].\n"
    "A local proxy pools and rotates free-tier API keys automatically,\n"
    "and signup bots keep the pool topped up while you work."
)


def _fail_with_log(step: str, log_path: Path) -> None:
    console.print(f"[red]✘ {step} failed.[/red] Log tail:")
    if log_path.exists():
        lines = log_path.read_text(errors="replace").splitlines()
        console.print("\n".join(lines[-25:]))
        console.print(f"[dim]Full log: {log_path}[/dim]")
    sys.exit(1)


def wait_deps() -> None:
    """Wait on the background pip started by install.sh."""
    pip_pid = int(os.environ.get("ATLAS_PIP_PID", "0") or 0)
    if not pip_pid:
        # invoked directly without install.sh — run deps synchronously
        venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
        reqs = PROJECT_ROOT / "requirements.txt"
        log_path = DATA_DIR / "logs" / "pip-install.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "wb") as lf:
            r = subprocess.run(
                [str(venv_py), "-m", "pip", "install", "-r", str(reqs)],
                stdout=lf, stderr=subprocess.STDOUT,
            )
        if r.returncode != 0:
            _fail_with_log("Dependency installation", log_path)
        return

    ok = False
    status = 0
    log_path = DATA_DIR / "logs" / "pip-install.log"
    with console.status("[cyan]Installing dependencies..."):
        while True:
            try:
                pid, status = os.waitpid(pip_pid, os.WNOHANG)
            except ChildProcessError:
                # Not our child (e.g. wizard re-invoked) — fall back to polling.
                while _pid_alive(pip_pid):
                    time.sleep(0.4)
                ok = True  # exited without our supervision; assume success
                break
            if pid != 0:
                break
            time.sleep(0.4)
    if ok is False:
        ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    if not ok:
        _fail_with_log("Dependency installation", log_path)
    console.print("[green]✔ Dependencies installed[/green]")


def ensure_playwright_chromium() -> None:
    """Needed only for the signup bots; skip quietly if it's already there."""
    with console.status("[cyan]Ensuring Chromium for automation (may download)..."):
        r = subprocess.run(
            [str(PROJECT_ROOT / ".venv" / "bin" / "python"), "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True,
        )
    if r.returncode == 0:
        console.print("[green]✔ Browser runtime ready[/green]")
    else:
        console.print("[yellow]⚠ Browser runtime setup had issues — signup bots may need attention later[/yellow]")


# ---------------------------------------------------------------------------
# Harness configuration
# ---------------------------------------------------------------------------

def configure_claude_code() -> str | None:
    """Set ANTHROPIC_BASE_URL to the proxy. Returns the launch command."""
    base = "http://127.0.0.1:8788"
    installed = bool(subprocess.run(["bash", "-lc", "command -v claude"], capture_output=True).returncode == 0)

    # Persist for future shells: append to profile unless already present.
    marker = "# atlas-proxy-env"
    exports = [f"export ANTHROPIC_BASE_URL={base}", "export ANTHROPIC_AUTH_TOKEN=atlas-local"]
    profiles = [Path.home() / ".zshrc", Path.home() / ".bashrc"]
    for prof in profiles:
        if prof.exists():
            txt = prof.read_text()
            if marker not in txt:
                prof.write_text(txt + f"\n{marker}\n" + "\n".join(exports) + "\n")
            break

    if not installed:
        has_npm = bool(subprocess.run(["bash", "-lc", "command -v npm"], capture_output=True).returncode == 0)
        if has_npm:
            console.print("  Installing Claude Code in the background...")
            subprocess.Popen(
                ["bash", "-lc", "npm install -g @anthropic-ai/claude-code"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            console.print(
                "  [yellow]Node.js/npm not found — needed to install Claude Code.[/yellow]\n"
                "    macOS:   brew install node\n"
                "    Linux:   see https://nodejs.org (or nvm)\n"
                "    Then:    npm install -g @anthropic-ai/claude-code\n"
                "  Atlas is configured for it already — just install and run 'claude'."
            )
    console.print(f"  ANTHROPIC_BASE_URL -> {base}")
    return "claude"


def configure_codex() -> str | None:
    """Write ~/.codex/config.toml provider block pointing at the proxy."""
    codex_dir = Path.home() / ".codex"
    cfg = codex_dir / "config.toml"
    codex_dir.mkdir(exist_ok=True)

    block = """
# --- begin atlas provider ---
model_provider = "atlas"

[model_providers.atlas]
name = "Atlas Proxy"
base_url = "http://127.0.0.1:8788/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 4
# --- end atlas provider ---
"""
    existing = cfg.read_text() if cfg.exists() else ""
    if "# --- begin atlas provider ---" in existing:
        console.print("  Codex already configured for Atlas")
    elif existing.strip():
        # preserve user content, replace any prior atlas block, append ours
        console.print("  Appending Atlas provider to existing Codex config")
        cfg.write_text(existing.rstrip() + "\n" + block)
    else:
        cfg.write_text(block.lstrip())
    console.print("  ~/.codex/config.toml -> Atlas proxy (Responses API)")
    return "codex"


def configure_hermes() -> str | None:
    """Point Hermes at the proxy via its documented custom-endpoint keys."""
    hermes_bin = subprocess.run(
        ["bash", "-lc", "command -v hermes"], capture_output=True, text=True
    ).stdout.strip()
    if not hermes_bin:
        console.print(
            "  [yellow]Hermes isn't installed yet.[/yellow] Install it with:\n"
            "    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash\n"
            "  Then re-run 'atlas install' (or 'hermes config set model.provider custom'\n"
            "  + base_url http://127.0.0.1:8788/v1) to wire it to Atlas."
        )
        return None

    def hset(key: str, val: str) -> bool:
        return subprocess.run(
            [hermes_bin, "config", "set", key, val], capture_output=True
        ).returncode == 0

    ok = all([
        hset("model.default", "atlas-auto"),
        hset("model.provider", "custom"),
        hset("model.base_url", "http://127.0.0.1:8788/v1"),
        hset("model.api_key", "local-dummy"),
        hset("model.context_length", "131072"),
    ])
    if ok:
        console.print("  ~/.hermes/config.yaml -> model.provider=custom, base_url=.../v1")
        return "hermes"
    console.print("  [yellow]Hermes CLI not usable yet — run 'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash' then re-run 'atlas install'[/yellow]")
    return None


HARNESSES = {
    "claude code": ("Claude Code", configure_claude_code),
    "codex": ("Codex", configure_codex),
    "hermes": ("Hermes", configure_hermes),
}


# ---------------------------------------------------------------------------
# Key bootstrap
# ---------------------------------------------------------------------------

def bootstrap_openrouter_key() -> None:
    console.print(Panel.fit(
        "[bold]One thing we strongly recommend[/bold]\n\n"
        "The signup bots farm free keys automatically — but they can fail\n"
        "(IP blocks, captcha changes, provider updates).\n\n"
        "[bold]Paste one OpenRouter API key from your main account[/bold]\n"
        "(https://openrouter.ai/settings/keys — free to create).\n"
        "That guarantees Atlas always has a working path, so if anything\n"
        "goes wrong you can ask an agent to help fix it.",
        border_style="cyan", width=64,
    ))
    key = Prompt.ask("OpenRouter key (sk-or-...) or Enter to skip", password=True, default="")
    key = key.strip()
    if not key:
        console.print("[yellow]Skipped — bots-only mode. You can add one later: atlas openrouter import[/yellow]")
        return
    kf = DATA_DIR / "openrouter_data" / "openroute_keys.txt"
    existing = set(kf.read_text().split()) if kf.exists() else set()
    if key not in existing:
        kf.parent.mkdir(parents=True, exist_ok=True)
        with open(kf, "a") as f:
            f.write(key + "\n")
        try:
            os.chmod(kf, 0o600)
        except OSError:
            pass
    console.print("[green]✔ Key added — the proxy hot-reloads it within 5 seconds[/green]")


def mention_email_keys() -> None:
    console.print(Panel.fit(
        "[bold]Optional: throwaway-email keys for the signup bots[/bold]\n\n"
        "To let the bots create accounts themselves you'll also need:\n"
        "  • an [bold]AgentMail[/bold] API key  (https://agentmail.to)\n"
        "  • an [bold]OpenMail[/bold] API key   (used by the OpenRouter bot)\n\n"
        "Add them anytime:\n"
        "  echo 'AGENTMAIL_API_KEY=...' >> " + str(PROJECT_ROOT / ".env") + "\n"
        "The proxy works fine without them — they only power account farming.",
        border_style="yellow", width=64,
    ))


# ---------------------------------------------------------------------------
# Startup + final handoff
# ---------------------------------------------------------------------------

SERVICES_TO_START = ["proxy"]


def start_services() -> None:
    backend = get_backend()
    for name in SERVICES_TO_START:
        if backend.is_running(name):
            console.print(f"[green]✔ {name} already running[/green]")
            continue
        backend.start(name)
        console.print(f"[green]✔ {name} started (log: data/logs/{name}.log)[/green]")

    # health check
    import urllib.request
    deadline = time.time() + 20
    with console.status("[cyan]Waiting for proxy health check..."):
        while time.time() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8788/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            console.print("[red]✘ Proxy did not become healthy — check data/logs/proxy.log[/red]")
            sys.exit(1)
    console.print("[green]✔ Proxy healthy at http://127.0.0.1:8788[/green]")


def maybe_start_bots() -> None:
    if Confirm.ask("Start the key-farming automations now?", default=True):
        backend = get_backend()
        for name in ("openrouter", "nvidia", "huggingface"):
            if not backend.is_running(name):
                backend.start(name)
        console.print("[green]✔ Automations running in the background[/green]")
    else:
        console.print("[dim]Skipped — start them anytime with: atlas start all[/dim]")


def launch_harness(cmd: str) -> None:
    console.print()
    console.print(Panel.fit(
        f"[bold green]Everything is set up.[/bold green]\n\n"
        f"Dropping you into [bold]{cmd}[/bold] — send your first prompt.",
        border_style="green", width=52,
    ))
    time.sleep(1)
    argv = [cmd]
    try:
        # resolve via login shell PATH so nvm/homebrew installs are found
        resolved = subprocess.run(
            ["bash", "-lc", f"command -v {cmd}"], capture_output=True, text=True
        ).stdout.strip()
        if resolved:
            argv = [resolved]
        os.execvp(argv[0], argv)
    except (FileNotFoundError, OSError):
        console.print(f"[yellow]'{cmd}' isn't on PATH yet (its install may still be running in the background).[/yellow]")
        console.print(f"Open a new terminal and run: [bold]{cmd}[/bold]")


def main() -> None:
    from .paths import assert_supported
    assert_supported()

    console.clear()
    console.print(BANNER, style="bold cyan", justify="center")
    console.print()
    console.print(Panel(WELCOME, border_style="cyan", width=70))
    console.print()

    wait_deps()
    ensure_playwright_chromium()

    console.print()
    console.print("[bold]Let's get you set up and developing.[/bold]")
    console.print("Which harness do you want to use?")
    opts = list(HARNESSES.items())
    for i, (k, (label, _)) in enumerate(opts, 1):
        console.print(f"  [cyan]{i}[/cyan] {label}")
    console.print("  [cyan]4[/cyan] Set up others later")
    choice = Prompt.ask("Select", choices=["1", "2", "3", "4"], default="1")

    launch_cmd = None
    if choice != "4":
        key = opts[int(choice) - 1][0]
        label, fn = HARNESSES[key]
        with console.status(f"[cyan]Configuring {label}..."):
            launch_cmd = fn()

    bootstrap_openrouter_key()
    start_services()
    maybe_start_bots()
    mention_email_keys()

    if launch_cmd:
        launch_harness(launch_cmd)
    else:
        console.print("\n[green]Setup complete.[/green] Start your harness anytime:")
        console.print("  claude | codex | hermes")


if __name__ == "__main__":
    main()
