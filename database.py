#!/usr/bin/env python3
"""
2Captcha Worker Bot — Database Layer
SQLite storage for stats, earnings, and captcha history.
"""

import aiosqlite
import asyncio
import json
import time
from pathlib import Path
from datetime import datetime, date

DB_PATH = Path(__file__).parent / "data" / "worker.db"


async def init_db():
    """Initialize the SQLite database with all required tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS captchas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captcha_id TEXT UNIQUE,
                captcha_type TEXT DEFAULT 'unknown',
                status TEXT DEFAULT 'pending',
                answer TEXT DEFAULT '',
                reward REAL DEFAULT 0.0,
                solve_time_ms INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0,
                solved_at REAL DEFAULT 0,
                error TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL,
                method TEXT,
                status TEXT DEFAULT 'pending',
                created_at REAL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_captchas_type ON captchas(captcha_type);
            CREATE INDEX IF NOT EXISTS idx_captchas_status ON captchas(status);
            CREATE INDEX IF NOT EXISTS idx_captchas_created ON captchas(created_at);
        """)
        await db.commit()


async def record_captcha(captcha_id: str, captcha_type: str):
    """Record a new captcha being picked up."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT OR IGNORE INTO captchas (captcha_id, captcha_type, status, created_at) VALUES (?, ?, 'processing', ?)",
            (captcha_id, captcha_type, time.time())
        )
        await db.commit()


async def update_captcha_result(captcha_id: str, status: str, answer: str = "",
                                  reward: float = 0.0, solve_ms: int = 0, error: str = ""):
    """Update captcha result after solving."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """UPDATE captchas SET status=?, answer=?, reward=?, solve_time_ms=?, 
               solved_at=?, error=? WHERE captcha_id=?""",
            (status, answer, reward, solve_ms, time.time(), error, captcha_id)
        )
        await db.commit()


async def get_stats() -> dict:
    """Get aggregated stats for dashboard."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # Total counts
        stats = {}

        # Total solved
        cursor = await db.execute("SELECT COUNT(*) FROM captchas WHERE status='solved'")
        stats["total_solved"] = (await cursor.fetchone())[0]

        # Total failed
        cursor = await db.execute("SELECT COUNT(*) FROM captchas WHERE status='failed'")
        stats["total_failed"] = (await cursor.fetchone())[0]

        # Total earnings
        cursor = await db.execute("SELECT COALESCE(SUM(reward), 0) FROM captchas WHERE status='solved'")
        stats["total_earnings"] = (await cursor.fetchone())[0]

        # Today's stats
        today_start = datetime.combine(date.today(), datetime.min.time()).timestamp()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM captchas WHERE status='solved' AND solved_at >= ?",
            (today_start,)
        )
        stats["solved_today"] = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COALESCE(SUM(reward), 0) FROM captchas WHERE status='solved' AND solved_at >= ?",
            (today_start,)
        )
        stats["earnings_today"] = (await cursor.fetchone())[0]

        # By type
        cursor = await db.execute(
            "SELECT captcha_type, COUNT(*), COALESCE(SUM(reward), 0), COALESCE(AVG(solve_time_ms), 0) "
            "FROM captchas WHERE status='solved' GROUP BY captcha_type"
        )
        type_stats = {}
        for row in await cursor.fetchall():
            type_stats[row[0]] = {
                "count": row[1],
                "earnings": row[2],
                "avg_solve_ms": round(row[3])
            }
        stats["by_type"] = type_stats

        # Recent activity (last 20)
        cursor = await db.execute(
            "SELECT captcha_id, captcha_type, status, reward, solve_time_ms, "
            "datetime(solved_at, 'unixepoch') as solved_at_str, error "
            "FROM captchas ORDER BY created_at DESC LIMIT 20"
        )
        stats["recent"] = [dict(zip(
            ["captcha_id", "captcha_type", "status", "reward", "solve_ms", "solved_at", "error"],
            row
        )) for row in await cursor.fetchall()]

        # Solve time stats
        cursor = await db.execute(
            "SELECT COALESCE(AVG(solve_time_ms), 0), COALESCE(MAX(solve_time_ms), 0), COALESCE(MIN(solve_time_ms), 0) "
            "FROM captchas WHERE status='solved' AND solve_time_ms > 0"
        )
        row = await cursor.fetchone()
        stats["avg_solve_ms"] = round(row[0])
        stats["max_solve_ms"] = row[1]
        stats["min_solve_ms"] = row[2]

        # Success rate
        total = stats["total_solved"] + stats["total_failed"]
        stats["success_rate"] = round(stats["total_solved"] / total * 100, 1) if total > 0 else 0

        # Hourly earnings chart (last 24 hours)
        cursor = await db.execute(
            """SELECT strftime('%H', datetime(solved_at, 'unixepoch')) as hour,
                      COUNT(*) as count,
                      COALESCE(SUM(reward), 0) as earnings
               FROM captchas WHERE status='solved' 
               AND solved_at >= ? 
               GROUP BY hour ORDER BY hour""",
            (time.time() - 86400,)
        )
        stats["hourly"] = [{"hour": row[0], "count": row[1], "earnings": row[2]}
                          for row in await cursor.fetchall()]

        return stats


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT value FROM stats WHERE key=?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT OR REPLACE INTO stats (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()


async def record_withdrawal(amount: float, method: str) -> int:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO withdrawals (amount, method, status, created_at) VALUES (?, ?, 'pending', ?)",
            (amount, method, time.time())
        )
        await db.commit()
        return cursor.lastrowid
