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

from .paths import PROJECT_ROOT, DATA_DIR, IS_LINUX, IS_MACOS, atomic_write_600
from .process import _pid_alive
from .service import get_backend, SERVICE_SPECS
from .display import find_chrome

console = Console()

# ---------------------------------------------------------------------------
# Non-TTY resilience: under `curl | bash` (or CI), Python's stdin is the pipe
# and every input() would EOF-crash. Prefer the controlling terminal (/dev/tty);
# fall back to env overrides / safe defaults when no terminal exists at all.
# ---------------------------------------------------------------------------
try:
    _tty_fd = os.open("/dev/tty", os.O_RDONLY)
except OSError:
    _tty_fd = None


def _interactive() -> bool:
    return _tty_fd is not None


def _tty_read(prompt_text: str, hide: bool = False) -> str:
    """Read one line from the controlling terminal. Raises EOFError.

    Prompts go to stdout; input comes from a single raw /dev/tty fd so
    piped input can't be split between competing file descriptions.
    """
    assert _tty_fd is not None
    console.print(prompt_text, end="")
    if hide:
        import getpass
        return getpass.getpass("")  # opens /dev/tty itself, no echo
    buf = b""
    while not buf.endswith(b"\n"):
        ch = os.read(_tty_fd, 1)
        if not ch:
            raise EOFError
        buf += ch
    return buf.decode(errors="replace").strip()


def ask(prompt: str, *, password: bool = False, default: str = "") -> str:
    """Input that survives non-TTY runs: env override -> tty -> default."""
    env_key = "ATLAS_ANSWER_" + "".join(
        c for c in prompt.upper() if c.isalnum()
    )[:24]
    if os.environ.get(env_key):
        return os.environ[env_key]
    suffix = f" [default: {default}]" if default else ""
    if _interactive():
        try:
            val = _tty_read(f"{prompt}{suffix}: ", hide=password)
            return val or default
        except (EOFError, OSError):
            pass
    console.print(f"{prompt} [dim](non-interactive: using default '{default or '<empty>'}')[/dim]")
    return default


def confirm(prompt: str, *, default: bool = True) -> bool:
    if _interactive():
        hint = "Y/n" if default else "y/N"
        try:
            while True:
                val = _tty_read(f"{prompt} [{hint}]: ").lower()
                if not val:
                    return default
                if val in ("y", "yes"):
                    return True
                if val in ("n", "no"):
                    return False
        except (EOFError, OSError):
            pass
    else:
        val = os.environ.get("ATLAS_ANSWER_START_BOTS")
        if val is not None:
            return val.strip().lower() in ("1", "y", "yes", "true", "on")
    console.print(f"{prompt} [dim](non-interactive: {default})[/dim]")
    return default


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


def _total_ram_mb() -> int | None:
    """Total system RAM in MB, or None if undetectable."""
    try:
        if IS_LINUX:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
        elif IS_MACOS:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
            return int(out.stdout.strip()) // (1024 * 1024)
    except Exception:
        pass
    return None


IS_LOW_RAM = (_ram := _total_ram_mb()) is not None and _ram < 2048


def _venv_py() -> Path:
    return PROJECT_ROOT / ".venv" / "bin" / "python"


_REQ_IMPORTS = ("fastapi", "uvicorn", "httpx", "orjson", "dotenv", "rich", "playwright", "patchright")


def deps_satisfied() -> bool:
    """Fast path: every requirement already importable in the venv?"""
    py = _venv_py()
    if not py.exists():
        return False
    probe = ";".join(f"import {m}" for m in _REQ_IMPORTS)
    return subprocess.run([str(py), "-c", probe], capture_output=True).returncode == 0


def start_deps_background() -> int | None:
    """Ensure dependency installation is running in the background.

    Returns the pip PID to wait on later, or None when nothing needs doing
    (already satisfied / already running).
    """
    pip_pid = int(os.environ.get("ATLAS_PIP_PID", "0") or 0)
    if pip_pid and _pid_alive(pip_pid):
        console.print("[dim]⚙ Dependencies installing in the background...[/dim]")
        return pip_pid

    if deps_satisfied():
        console.print("[green]✔ Dependencies already present (cached) — skipping install[/green]")
        return None

    py = _venv_py()
    if not py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(PROJECT_ROOT / ".venv")], check=True)

    log_path = DATA_DIR / "logs" / "pip-install.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    if IS_LOW_RAM:
        # Single-job builds: wheel compilation is the RAM spike on small boxes.
        env["MAKEFLAGS"] = "-j1"
        console.print("[yellow]⚠ Low RAM detected — using conservative install settings[/yellow]")
    lf = open(log_path, "ab")
    proc = subprocess.Popen(
        [str(py), "-m", "pip", "install", "--quiet", "-r",
         str(PROJECT_ROOT / "requirements.txt")],
        stdout=lf, stderr=subprocess.STDOUT, env=env,
        start_new_session=True,
    )
    console.print("[dim]⚙ Dependencies installing in the background (log: data/logs/pip-install.log)[/dim]")
    return proc.pid


def wait_deps(pip_pid: int | None) -> None:
    """Gate: block until the background pip finishes. Only called right
    before we actually need the deps (starting services)."""
    if pip_pid is None:
        return
    ok = False
    status = 0
    log_path = DATA_DIR / "logs" / "pip-install.log"
    with console.status("[cyan]Finishing dependency setup..."):
        while True:
            try:
                pid, status = os.waitpid(pip_pid, os.WNOHANG)
            except ChildProcessError:
                # Not our child (re-invoked wizard) — poll liveness instead.
                while _pid_alive(pip_pid):
                    time.sleep(0.4)
                ok = True
                break
            if pid != 0:
                break
            time.sleep(0.4)
    if ok is False:
        ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    # Belt & braces: verify imports even if pip claimed success.
    # Metadata-only satisfaction (broken/partial installs) fools pip but not
    # an import — retry once with --force-reinstall.
    if ok and not deps_satisfied():
        console.print("[yellow]⚠ Imports broken despite pip success — forcing reinstall...[/yellow]")
        r2 = subprocess.run(
            [str(_venv_py()), "-m", "pip", "install", "--quiet", "--force-reinstall",
             "-r", str(PROJECT_ROOT / "requirements.txt")],
            stdout=open(log_path, "ab"), stderr=subprocess.STDOUT,
            env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        ok = r2.returncode == 0 and deps_satisfied()
    if not ok:
        _fail_with_log("Dependency installation", log_path)
    console.print("[green]✔ Dependencies ready[/green]")


def ensure_playwright_chromium() -> None:
    """Chromium download for the signup bots. Skipped on low-RAM machines
    and never blocks the setup flow."""
    if IS_LOW_RAM:
        console.print(
            "[yellow]⚠ Low RAM: skipping browser runtime download.[/yellow] "
            "[dim]Run '.venv/bin/python -m playwright install chromium' before using the signup bots.[/dim]"
        )
        return

    def _done(proc: subprocess.Popen) -> None:
        if proc.wait() == 0:
            console.print("[green]✔ Browser runtime ready[/green]")

    py = _venv_py()
    proc = subprocess.Popen(
        [str(py), "-m", "playwright", "install", "chromium"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    console.print("[dim]⚙ Browser runtime downloading in the background[/dim]")
    # Watcher thread prints when done; never blocks the flow.
    import threading
    threading.Thread(target=_done, args=(proc,), daemon=True).start()


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
    key = ask("OpenRouter key (sk-or-...) or Enter to skip", password=True, default="")
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
    if confirm("Start the key-farming automations now?", default=True):
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

    # Deps install in the background — the user keeps going while pip works.
    pip_pid = start_deps_background()

    console.print()
    console.print("[bold]Let's get you set up and developing.[/bold]")
    console.print("Which harness do you want to use?")
    opts = list(HARNESSES.items())
    for i, (k, (label, _)) in enumerate(opts, 1):
        console.print(f"  [cyan]{i}[/cyan] {label}")
    console.print("  [cyan]4[/cyan] Set up others later")
    choice = ask("Select", default="1").strip()
    if choice not in ("1", "2", "3", "4"):
        console.print(f"[yellow]Invalid choice '{choice}' — using default (1)[/yellow]")
        choice = "1"

    launch_cmd = None
    if choice != "4":
        key = opts[int(choice) - 1][0]
        label, fn = HARNESSES[key]
        with console.status(f"[cyan]Configuring {label}..."):
            launch_cmd = fn()

    bootstrap_openrouter_key()

    # First hard gate: services need real deps on disk.
    wait_deps(pip_pid)
    ensure_playwright_chromium()
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
