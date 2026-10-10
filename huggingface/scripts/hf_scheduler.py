#!/usr/bin/env python3
"""
hf_scheduler.py — Loop runner for main.py (HuggingFace signup automation).

Runs main.py repeatedly with a configurable delay between attempts.
If main.py exits non-zero, the scheduler retries until the run limit is
hit or the user interrupts with Ctrl+C.

The scheduler itself NEVER launches browsers — it just spawns main.py
as a subprocess.  Each main.py invocation handles its own CDP launch,
cookie injection, signup, token extraction, and full cleanup.

Usage:
    python3 hf_scheduler.py --runs 5               # 5 runs, 240s delay
    python3 hf_scheduler.py --runs 10 --delay 30   # 10 runs, 30s delay
    python3 hf_scheduler.py --runs 0 --delay 120   # forever, 120s delay
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT = SCRIPT_DIR / "main.py"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_once(attempt: int) -> bool:
    """Run main.py once.  Returns True on success (exit 0)."""
    log(f"=== Run #{attempt} starting ===")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            timeout=600,  # 10 min max per run
        )
        if result.returncode == 0:
            log(f"Run #{attempt} SUCCESS")
            return True
        else:
            log(f"Run #{attempt} FAILED (exit code {result.returncode})")
            return False
    except subprocess.TimeoutExpired:
        log(f"Run #{attempt} TIMED OUT after 10 minutes")
        return False
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log(f"Run #{attempt} ERROR: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="HF Signup scheduler")
    parser.add_argument("--runs", type=int, default=0,
                        help="Number of runs (0 = run forever)")
    parser.add_argument("--delay", type=int, default=240,
                        help="Seconds to wait between runs")
    args = parser.parse_args()

    forever = args.runs == 0
    total = args.runs
    delay = args.delay

    log(f"Scheduler started — {'∞' if forever else total} run(s), {delay}s delay between each")
    log("Press Ctrl+C to stop\n")

    successes = 0
    failures = 0
    attempt = 1

    try:
        while forever or attempt <= total:
            success = run_once(attempt)
            if success:
                successes += 1
            else:
                failures += 1

            attempt += 1
            if not (forever or attempt <= total):
                break

            log(f"Waiting {delay}s before next run... (success={successes} fail={failures})\n")
            time.sleep(delay)

    except KeyboardInterrupt:
        log("\nScheduler stopped by user")

    log(f"\n=== DONE: {successes} succeeded, {failures} failed out of {attempt - 1} runs ===")


if __name__ == "__main__":
    main()
