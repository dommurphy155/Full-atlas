#!/usr/bin/env python3
"""
hf_scheduler.py — Loop runner for main.py (HuggingFace signup automation).

Runs main.py repeatedly with a configurable delay between attempts.
If main.py exits non-zero, the scheduler retries until the run limit is
hit or the user interrupts with Ctrl+C.

The scheduler itself NEVER launches browsers — it just spawns main.py
as a subprocess.  Each main.py invocation handles its own CDP launch,
cookie injection, signup, token extraction, and full cleanup.

All run data is persisted to SQLite (hf_runs.db) — survives sessions.
The (success=N fail=M) status line is always read from the DB, not
from in-memory counters.

Usage:
    python3 hf_scheduler.py --runs 5               # 5 runs, 240s delay
    python3 hf_scheduler.py --runs 10 --delay 30   # 10 runs, 30s delay
    python3 hf_scheduler.py --runs 0 --delay 120   # forever, 120s delay
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT = SCRIPT_DIR / "main.py"

sys.path.insert(0, str(SCRIPT_DIR))
from db import init_db, start_session, end_session, start_attempt, end_attempt
from db import _get_conn


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def session_tally(session_id: int) -> tuple[int, int]:
    """Return (successes, failures) for *session_id* — always from the DB."""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT status, COUNT(*) FROM attempts WHERE session_id = ? GROUP BY status",
        (session_id,),
    )
    rows = {row[0]: row[1] for row in cur.fetchall()}
    return rows.get("success", 0), rows.get("fail", 0) + rows.get("timeout", 0) + rows.get("error", 0)


def run_once(attempt_num: int, session_id: int, attempt_id: int) -> dict:
    """Run main.py once. Returns dict with status, exit_code, error, duration_seconds."""
    log(f"=== Run #{attempt_num} starting ===")
    started = time.time()
    status = "fail"
    exit_code = -1
    error = None

    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            timeout=600,  # 10 min max per run
            env={
                **os.environ,
                "HF_SESSION_ID": str(session_id),
                "HF_ATTEMPT_ID": str(attempt_id),
            },
        )
        exit_code = result.returncode
        if result.returncode == 0:
            log(f"Run #{attempt_num} SUCCESS")
            status = "success"
        else:
            log(f"Run #{attempt_num} FAILED (exit code {result.returncode})")
            status = "fail"
    except subprocess.TimeoutExpired:
        log(f"Run #{attempt_num} TIMED OUT after 10 minutes")
        status = "timeout"
        error = "Timeout after 600s"
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log(f"Run #{attempt_num} ERROR: {e}")
        status = "error"
        error = str(e)[:500]

    duration = time.time() - started
    return {"status": status, "exit_code": exit_code, "error": error, "duration_seconds": duration}


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

    # --- DB init ---
    init_db()
    session_id = start_session()

    log(f"Scheduler started — {'∞' if forever else total} run(s), {delay}s delay between each")
    log(f"Session ID: {session_id}")
    log("Press Ctrl+C to stop\n")

    attempt_num = 1

    try:
        while forever or attempt_num <= total:
            # Record attempt start
            attempt_id = start_attempt(session_id, attempt_num)

            result = run_once(attempt_num, session_id, attempt_id)

            # Record attempt end
            end_attempt(
                attempt_id,
                status=result["status"],
                exit_code=result["exit_code"],
                error_message=result["error"],
                duration_seconds=result["duration_seconds"],
            )

            attempt_num += 1
            if not (forever or attempt_num <= total):
                break

            # Pull live tally from DB — not in-memory counters
            s, f = session_tally(session_id)
            log(f"Waiting {delay}s before next run... (success={s} fail={f})\n")
            time.sleep(delay)

    except KeyboardInterrupt:
        log("\nScheduler stopped by user")
        end_session(session_id, 0, 0, status="interrupted")
        s, f = session_tally(session_id)
        log(f"\n=== DONE: {s} succeeded, {f} failed out of {attempt_num - 1} runs ===")
        return

    s, f = session_tally(session_id)
    end_session(session_id, s, f, status="completed")
    log(f"\n=== DONE: {s} succeeded, {f} failed out of {attempt_num - 1} runs ===")


if __name__ == "__main__":
    main()
