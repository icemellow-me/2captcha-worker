#!/usr/bin/env python3
"""
2Captcha Worker Bot — Database Layer v2
Added: accounts table for multi-account support.
"""

import aiosqlite
import asyncio
import json
import time
from pathlib import Path
from datetime import datetime, date

DB_PATH = Path(__file__).parent / "data" / "worker.db"


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS captchas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captcha_id TEXT UNIQUE,
                account_label TEXT DEFAULT '',
                captcha_type TEXT DEFAULT 'unknown',
                status TEXT DEFAULT 'pending',
                answer TEXT DEFAULT '',
                reward REAL DEFAULT 0.0,
                solve_time_ms INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0,
                solved_at REAL DEFAULT 0,
                error TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS accounts (
                thash TEXT PRIMARY KEY,
                label TEXT DEFAULT '',
                email TEXT DEFAULT '',
                user_id INTEGER DEFAULT 0,
                balance REAL DEFAULT 0.0,
                reputation REAL DEFAULT 0.0,
                solved INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                earnings REAL DEFAULT 0.0,
                running INTEGER DEFAULT 0,
                paused INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL,
                account TEXT,
                method TEXT,
                status TEXT DEFAULT 'pending',
                created_at REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS worker_state (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT '',
                updated_at REAL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_captchas_type ON captchas(captcha_type);
            CREATE INDEX IF NOT EXISTS idx_captchas_status ON captchas(status);
            CREATE INDEX IF NOT EXISTS idx_captchas_created ON captchas(created_at);
            CREATE INDEX IF NOT EXISTS idx_captchas_account ON captchas(account_label);
        """)
        # Add paused column to existing accounts table if missing
        try:
            await db.execute("ALTER TABLE accounts ADD COLUMN paused INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists
        await db.commit()


# ─── Worker State Persistence ────────────────────────────────

async def set_worker_state(key: str, value: str):
    """Persist a key-value state (e.g. worker_running=true)."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO worker_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
            (key, value, time.time(), value, time.time())
        )
        await db.commit()


async def get_worker_state(key: str, default: str = "") -> str:
    """Read a persisted state value."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT value FROM worker_state WHERE key=?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_account_paused(thash: str, paused: bool):
    """Persist pause state for an account."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("UPDATE accounts SET paused=? WHERE thash=?", (1 if paused else 0, thash))
        await db.commit()


async def get_account_paused(thash: str) -> bool:
    """Check if an account is paused (from DB)."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT paused FROM accounts WHERE thash=?", (thash,))
        row = await cursor.fetchone()
        return bool(row[0]) if row else False


async def save_account(thash, label="", email="", user_id=0, balance=0.0, reputation=0.0):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("""
            INSERT INTO accounts (thash, label, email, user_id, balance, reputation, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thash) DO UPDATE SET label=?, email=?, user_id=?, balance=?, reputation=?
        """, (thash, label, email, user_id, balance, reputation, time.time(),
              label, email, user_id, balance, reputation))
        await db.commit()


async def remove_account(thash):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM accounts WHERE thash=?", (thash,))
        await db.commit()


async def get_accounts():
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT * FROM accounts")
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]


async def record_captcha(captcha_id, captcha_type, account_label=""):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT OR IGNORE INTO captchas (captcha_id, account_label, captcha_type, status, created_at) VALUES (?, ?, ?, 'processing', ?)",
            (captcha_id, account_label, captcha_type, time.time())
        )
        await db.commit()


async def update_captcha_result(captcha_id, status, answer="", reward=0.0, solve_ms=0, error=""):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE captchas SET status=?, answer=?, reward=?, solve_time_ms=?, solved_at=?, error=? WHERE captcha_id=?",
            (status, answer, reward, solve_ms, time.time(), error, captcha_id)
        )
        await db.commit()


async def get_stats():
    async with aiosqlite.connect(str(DB_PATH)) as db:
        stats = {"by_type": {}, "by_account": {}}

        cursor = await db.execute("SELECT COUNT(*) FROM captchas WHERE status='solved'")
        stats["total_solved"] = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM captchas WHERE status!='solved' AND status!='processing'")
        stats["total_failed"] = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COALESCE(SUM(reward), 0) FROM captchas WHERE status='solved'")
        stats["total_earnings"] = (await cursor.fetchone())[0]

        today_start = datetime.combine(date.today(), datetime.min.time()).timestamp()
        cursor = await db.execute("SELECT COUNT(*) FROM captchas WHERE status='solved' AND solved_at >= ?", (today_start,))
        stats["solved_today"] = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COALESCE(SUM(reward), 0) FROM captchas WHERE status='solved' AND solved_at >= ?", (today_start,))
        stats["earnings_today"] = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT captcha_type, COUNT(*), COALESCE(SUM(reward),0), COALESCE(AVG(solve_time_ms),0) FROM captchas WHERE status='solved' GROUP BY captcha_type"
        )
        for row in await cursor.fetchall():
            stats["by_type"][row[0]] = {"count": row[1], "earnings": row[2], "avg_solve_ms": int(row[3] or 0)}

        cursor = await db.execute(
            "SELECT account_label, COUNT(*), COALESCE(SUM(reward),0) FROM captchas WHERE status='solved' AND account_label != '' GROUP BY account_label"
        )
        for row in await cursor.fetchall():
            stats["by_account"][row[0]] = {"count": row[1], "earnings": row[2]}

        cursor = await db.execute(
            "SELECT captcha_id, account_label, captcha_type, status, answer, reward, solve_time_ms, solved_at FROM captchas ORDER BY solved_at DESC LIMIT 20"
        )
        cols = [d[0] for d in cursor.description]
        stats["recent"] = [dict(zip(cols, row)) for row in await cursor.fetchall()]

        return stats
