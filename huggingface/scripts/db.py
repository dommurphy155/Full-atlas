#!/usr/bin/env python3
"""
db.py — SQLite persistence for HF signup runs, attempts, and keys.
Survives across scheduler sessions. Import-safe, idempotent init.

Tables:
  sessions  — one row per scheduler invocation
  attempts  — one row per main.py spawn
  keys      — key inventory (hash only, never stores raw key)
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "atlas" / "data" / "huggingface_data" / "hf_runs.db"

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH))
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    """Create tables and indexes if they don't exist. Idempotent."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT,
            total_runs INTEGER DEFAULT 0,
            successes INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running'
        );

        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            attempt_num INTEGER NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT,
            status TEXT DEFAULT 'running',
            exit_code INTEGER,
            error_message TEXT,
            email TEXT,
            username TEXT,
            duration_seconds REAL,
            UNIQUE(session_id, attempt_num)
        );

        CREATE TABLE IF NOT EXISTS keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT UNIQUE NOT NULL,
            key_masked TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            attempt_id INTEGER REFERENCES attempts(id),
            session_id INTEGER REFERENCES sessions(id),
            status TEXT DEFAULT 'unknown',
            last_checked_at TEXT,
            account_name TEXT,
            account_type TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id);
        CREATE INDEX IF NOT EXISTS idx_keys_hash ON keys(key_hash);
        CREATE INDEX IF NOT EXISTS idx_keys_status ON keys(status);
    """)
    conn.commit()


# --- helpers ---

def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.strip().encode()).hexdigest()


def _mask_key(raw: str) -> str:
    k = raw.strip()
    if len(k) <= 14:
        return k[:4] + "***"
    return k[:4] + "***" + k[-4:]


# --- sessions ---

def start_session() -> int:
    """Begin a new scheduler session. Returns session_id."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO sessions (started_at, status) VALUES (datetime('now'), 'running')"
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def end_session(session_id: int, successes: int, failures: int,
                status: str = "completed") -> None:
    """Close a scheduler session with final tallies."""
    conn = _get_conn()
    conn.execute(
        """UPDATE sessions
           SET ended_at = datetime('now'),
               total_runs = ?,
               successes = ?,
               failures = ?,
               status = ?
           WHERE id = ?""",
        (successes + failures, successes, failures, status, session_id),
    )
    conn.commit()


# --- attempts ---

def start_attempt(session_id: int, attempt_num: int) -> int:
    """Record that an attempt is starting. Returns attempt_id."""
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO attempts (session_id, attempt_num, started_at, status)
           VALUES (?, ?, datetime('now'), 'running')""",
        (session_id, attempt_num),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def end_attempt(attempt_id: int, status: str, exit_code: int | None = None,
                error_message: str | None = None,
                duration_seconds: float | None = None) -> None:
    """Mark an attempt as finished with outcome."""
    conn = _get_conn()
    fields = ["ended_at = datetime('now')", "status = ?"]
    params: list = [status]
    if exit_code is not None:
        fields.append("exit_code = ?")
        params.append(exit_code)
    if error_message is not None:
        fields.append("error_message = ?")
        params.append(error_message[:500] if len(error_message) > 500 else error_message)
    if duration_seconds is not None:
        fields.append("duration_seconds = ?")
        params.append(duration_seconds)
    params.append(attempt_id)
    conn.execute(f"UPDATE attempts SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()


# --- keys ---

def insert_key(raw_key: str, attempt_id: int | None = None,
               session_id: int | None = None) -> bool:
    """Record a newly created key. Raw key is hashed; never stored plaintext.
    Returns True if inserted, False if already known."""
    conn = _get_conn()
    kh = _hash_key(raw_key)
    km = _mask_key(raw_key)
    try:
        conn.execute(
            """INSERT INTO keys (key_hash, key_masked, created_at,
                                 attempt_id, session_id, status)
               VALUES (?, ?, datetime('now'), ?, ?, 'unknown')""",
            (kh, km, attempt_id, session_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def update_key_status(raw_key: str, status: str,
                      account_name: str | None = None,
                      account_type: str | None = None) -> bool:
    """Update a key's validation status. Returns True if key was found."""
    conn = _get_conn()
    kh = _hash_key(raw_key)
    cur = conn.execute(
        """UPDATE keys
           SET status = ?, last_checked_at = datetime('now'),
               account_name = COALESCE(?, account_name),
               account_type = COALESCE(?, account_type)
           WHERE key_hash = ?""",
        (status, account_name, account_type, kh),
    )
    conn.commit()
    return cur.rowcount > 0


# --- queries ---

def get_key_count(status: str | None = None) -> int:
    conn = _get_conn()
    if status:
        cur = conn.execute("SELECT COUNT(*) FROM keys WHERE status = ?", (status,))
    else:
        cur = conn.execute("SELECT COUNT(*) FROM keys")
    return cur.fetchone()[0]


def get_session_summary(session_id: int | None = None,
                        limit: int = 10) -> list[dict]:
    conn = _get_conn()
    if session_id:
        cur = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    else:
        cur = conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        )
    return [dict(row) for row in cur.fetchall()]


def get_recent_attempts(session_id: int | None = None,
                        limit: int = 20) -> list[dict]:
    conn = _get_conn()
    if session_id:
        cur = conn.execute(
            "SELECT * FROM attempts WHERE session_id = ? "
            "ORDER BY attempt_num DESC LIMIT ?",
            (session_id, limit),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM attempts ORDER BY started_at DESC LIMIT ?", (limit,)
        )
    return [dict(row) for row in cur.fetchall()]
