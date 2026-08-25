#!/usr/bin/env python3
"""
Scheduler for main.py — runs the OpenRouter signup automation in loops.
Usage:
    python3 scheduler.py --runs 5               # run 5 times, 240s delay between each
    python3 scheduler.py --runs 10 --delay 30   # run 10 times, 30s delay
    python3 scheduler.py --runs 0 --delay 120   # run forever, 120s between each

Linux-only, portable paths from config.py.
"""
import argparse
import subprocess
import time
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from config import PROJECT_ROOT, ensure_dirs

SCRIPT = SCRIPT_DIR / "main.py"

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_once(attempt: int) -> bool:
    log(f"=== Run #{attempt} starting ===")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            timeout=600,  # 10 min max per run
            cwd=str(PROJECT_ROOT),
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


def main():
    ensure_dirs()
    parser = argparse.ArgumentParser(description="Signup scheduler")
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

            if forever or attempt <= total:
                log(f"Waiting {delay}s before next run... (success={successes} fail={failures})\n")
                time.sleep(delay)

    except KeyboardInterrupt:
        log("\nScheduler stopped by user")

    log(f"\n=== DONE: {successes} succeeded, {failures} failed out of {attempt - 1} runs ===")


if __name__ == "__main__":
    main()
