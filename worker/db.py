"""db.py — SQLite-backed persistence for the 2captcha worker + dashboard.

A single `stats.db` file holds:
    • settings       — key/value table for all dashboard-configurable options
    • captchas      — one row per captcha the worker accepted (solved, failed,
                       timed-out, etc.)  — drives the activity feed + charts
    • earnings      — one row per day aggregate (daily totals)
    • balance_snapshots — periodic account balance polls (for the chart)
    • worker_events — structured log lines for the activity feed
    • withdraw_requests — payout requests made from the dashboard

The schema is intentionally simple & extensible: every column is TEXT/REAL
(other than PKs and `dt`/`ts` columns) so the dashboard can render anything
without schema migrations.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_DB_PATH = os.environ.get(
    "TWO_CAPTCHA_DB", "/app/data/stats.db"
)
if not os.path.exists(os.path.dirname(DEFAULT_DB_PATH) or "."):
    DEFAULT_DB_PATH = os.path.join(os.getcwd(), "data", "stats.db")
os.makedirs(os.path.dirname(DEFAULT_DB_PATH) or ".", exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captchas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captcha_id    TEXT NOT NULL,            -- 2captcha's id
    type          TEXT NOT NULL,            -- recaptcha_v2 / turnstile / image ...
    site_url      TEXT DEFAULT '',
    sitekey       TEXT DEFAULT '',
    solver        TEXT DEFAULT '',           -- which of our solvers handled it
    status        TEXT NOT NULL,             -- received/solving/solved/failed/timeout/skipped
    answer        TEXT DEFAULT '',
    error         TEXT DEFAULT '',
    reward_usd    REAL DEFAULT 0,            -- actual payout per 1000 / 1000
    received_at   REAL NOT NULL,             -- unix epoch
    solved_at     REAL DEFAULT 0,
    solve_ms      REAL DEFAULT 0,
    raw           TEXT DEFAULT ''            -- full raw payload (json or b64 trailer)
);
CREATE INDEX IF NOT EXISTS idx_captchas_received ON captchas(received_at);
CREATE INDEX IF NOT EXISTS idx_captchas_type    ON captchas(type);
CREATE INDEX IF NOT EXISTS idx_captchas_status   ON captchas(status);

CREATE TABLE IF NOT EXISTS earnings (
    day           TEXT PRIMARY KEY,          -- YYYY-MM-DD UTC
    total_solved  INTEGER DEFAULT 0,
    total_failed  INTEGER DEFAULT 0,
    total_skipped INTEGER DEFAULT 0,
    profit_usd    REAL DEFAULT 0,
    avg_solve_ms  REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,
    balance REAL NOT NULL,
    source TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bal_ts ON balance_snapshots(ts);

CREATE TABLE IF NOT EXISTS worker_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    level   TEXT DEFAULT 'info',             -- info/success/warn/error/debug
    kind    TEXT DEFAULT '',                  -- captcha-received/solved/failed/poll/login/withdraw/setting/etc.
    message TEXT NOT NULL,
    captcha_id TEXT DEFAULT '',
    extra   TEXT DEFAULT ''                   -- json blob
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON worker_events(ts);

CREATE TABLE IF NOT EXISTS withdraw_requests (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    amount  REAL NOT NULL,
    method  TEXT DEFAULT '',
    status  TEXT DEFAULT 'requested',
    message TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_withdraw_ts ON withdraw_requests(ts);
"""


# ─── Connection management ───────────────────────────────────────────

class Database:
    """Thread-safe SQLite wrapper. aiohttp runs on a single event loop,
    but the worker uses `asyncio.to_thread`, so we use a per-call connection
    plus a global re-entrant lock for migrations — not a single shared
    connection (that would break under concurrent threads)."""

    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._migrated = False
        with self._connect() as c:
            c.executescript(SCHEMA)
            c.commit()
        self._migrated = True

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False because calls come from the worker's
        # asyncio.to_thread contexts; the RLock serialises writes.
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ─── Settings ───
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as c:
            r = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
        with self._lock:
            pass  # commit already happened

    def get_settings(self) -> Dict[str, str]:
        with self._connect() as c:
            return {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings").fetchall()}

    def set_settings(self, kvs: Dict[str, str]) -> None:
        with self._connect() as c:
            c.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                list(kvs.items()),
            )

    # ─── Captchas ───
    def insert_captcha(self, **fields: Any) -> int:
        cols = ("captcha_id","type","site_url","sitekey","solver","status",
                "answer","error","reward_usd","received_at","solved_at",
                "solve_ms","raw")
        values = {k: fields.get(k, "") for k in cols}
        if not values.get("received_at"):
            values["received_at"] = time.time()
        keys = list(values.keys())
        with self._connect() as c:
            cur = c.execute(
                f"INSERT INTO captchas ({','.join(keys)}) VALUES ({','.join(['?']*len(keys))})",
                [values[k] for k in keys],
            )
            return cur.lastrowid

    def update_captcha(self, captcha_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [captcha_id]
        with self._connect() as c:
            c.execute(f"UPDATE captchas SET {assignments} WHERE captcha_id=?", params)

    def update_captcha_by_pk(self, pk: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [pk]
        with self._connect() as c:
            c.execute(f"UPDATE captchas SET {assignments} WHERE id=?", params)

    def list_captchas(self, limit: int = 50, offset: int = 0) -> List[dict]:
        with self._connect() as c:
            rs = c.execute(
                "SELECT * FROM captchas ORDER BY received_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rs]

    def recent_events(self, limit: int = 60) -> List[dict]:
        with self._connect() as c:
            rs = c.execute(
                "SELECT * FROM worker_events ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rs]

    def add_event(self, level: str, kind: str, message: str,
                  captcha_id: str = "", extra: Any = None) -> int:
        extra_s = json.dumps(extra) if isinstance(extra, (dict, list)) else (
            extra or "")
        with self._connect() as c:
            cur = c.execute(
                "INSERT INTO worker_events (ts, level, kind, message, captcha_id, extra) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), level, kind, message, captcha_id, extra_s),
            )
            return cur.lastrowid

    # ─── Earnings aggregation ───
    def _day(self, ts: Optional[float] = None) -> str:
        return (datetime.utcnow() if ts is None else datetime.utcfromtimestamp(ts)).strftime("%Y-%m-%d")

    def bump_earnings(self, ts: float, status: str, type_: str,
                      profit: float, solve_ms: float) -> None:
        day = self._day(ts)
        with self._connect() as c:
            c.execute(
                "INSERT INTO earnings(day) VALUES(?) ON CONFLICT(day) DO NOTHING",
                (day,),
            )
            col_map = {
                "solved":  ("total_solved",  "profit_usd"),
                "failed":  ("total_failed", None),
                "skipped":  ("total_skipped", None),
                "timeout":  ("total_failed", None),
            }
            bump_col, profit_col = col_map.get(status, ("total_solved", "profit_usd"))
            c.execute(
                f"UPDATE earnings SET {bump_col}={bump_col}+1 "
                + (f", {profit_col}={profit_col}+? " if profit_col else "")
                + "WHERE day=?",
                ([profit] if profit_col else []) + [day],
            )
            # running average solve_time across solved today
            if status == "solved" and solve_ms > 0:
                c.execute(
                    "UPDATE earnings SET avg_solve_ms=("
                    " SELECT AVG(solve_ms) FROM captchas "
                    " WHERE status='solved' AND strftime('%Y-%m-%d', "
                    " datetime(received_at,'unixepoch')) = ?"
                    ") WHERE day=?",
                    (day, day),
                )

    def earnings_range(self, days: int = 30) -> List[dict]:
        with self._connect() as c:
            cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
            rs = c.execute(
                "SELECT * FROM earnings WHERE day >= ? ORDER BY day ASC",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rs]

    # ─── Balances ───
    def add_balance(self, balance: float, source: str = "") -> None:
        with self._connect() as c:
            c.execute(
                "INSERT INTO balance_snapshots (ts, balance, source) VALUES (?, ?, ?)",
                (time.time(), float(balance), source),
            )

    def balances_range(self, since_ts: Optional[float] = None) -> List[dict]:
        with self._connect() as c:
            if since_ts is None:
                rs = c.execute(
                    "SELECT ts, balance, source FROM balance_snapshots "
                    " ORDER BY ts DESC LIMIT 500"
                ).fetchall()
                # ascending for chart
                return [dict(r) for r in reversed(rs)]
            rs = c.execute(
                "SELECT ts, balance, source FROM balance_snapshots WHERE ts >= ? "
                "ORDER BY ts ASC", (since_ts,)
            ).fetchall()
            return [dict(r) for r in rs]

    # ─── Withdraw ───
    def add_withdraw(self, amount: float, method: str = "", status: str = "requested",
                     message: str = "") -> int:
        with self._connect() as c:
            cur = c.execute(
                "INSERT INTO withdraw_requests (ts, amount, method, status, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), float(amount), method, status, message),
            )
            return cur.lastrowid

    def list_withdraws(self, limit: int = 30) -> List[dict]:
        with self._connect() as c:
            rs = c.execute(
                "SELECT * FROM withdraw_requests ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rs]

    # ─── Aggregated summary ───
    def summary(self) -> Dict[str, Any]:
        """All numbers the dashboard needs in one shot."""
        with self._connect() as c:
            caps = c.execute(
                "SELECT "
                " COUNT(*) AS total, "
                " SUM(status='solved') AS solved, "
                " SUM(status='failed') AS failed, "
                " SUM(status='timeout') AS timeout, "
                " SUM(status='skipped') AS skipped, "
                " SUM(status='solving') AS solving, "
                " SUM(reward_usd) AS total_reward, "
                " AVG(CASE WHEN status='solved' THEN solve_ms END) AS avg_ms "
                "FROM captchas"
            ).fetchone()
            today = self._day()
            day = c.execute(
                "SELECT "
                " COUNT(*) AS total, "
                " SUM(status='solved') AS solved, "
                " SUM(reward_usd) AS total_reward, "
                " AVG(CASE WHEN status='solved' THEN solve_ms END) AS avg_ms "
                "FROM captchas WHERE strftime('%Y-%m-%d', datetime(received_at,'unixepoch')) = ?",
                (today,),
            ).fetchone()
            by_type = c.execute(
                "SELECT type, COUNT(*) AS n, SUM(status='solved') AS solved, "
                " SUM(reward_usd) AS reward, AVG(CASE WHEN status='solved' THEN solve_ms END) AS avg_ms "
                "FROM captchas GROUP BY type"
            ).fetchall()
            last_balance = c.execute(
                "SELECT balance, ts FROM balance_snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return {
                "total":      dict(caps),
                "today":      dict(day),
                "by_type":    [dict(r) for r in by_type],
                "last_balance": dict(last_balance) if last_balance else None,
                "server_time": time.time(),
            }


# Async-friendly facade ------------------------------------------------

class AsyncDatabase:
    """Thin async wrapper around `Database`.

    aiohttp handlers get `await db_async.summary()` etc.; the underlying
    blocking SQLite call is dispatched to the default `ThreadPoolExecutor`
    via `asyncio.to_thread`. The worker too uses this wrapper so it never
    blocks the event loop on a disk write.
    """
    def __init__(self, path: str = DEFAULT_DB_PATH):
        self._db = Database(path)

    @property
    def sync(self) -> Database:
        return self._db

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return await asyncio.to_thread(self._db.get_setting, key, default)

    async def set_setting(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._db.set_setting, key, value)

    async def get_settings(self) -> Dict[str, str]:
        return await asyncio.to_thread(self._db.get_settings)

    async def set_settings(self, kvs: Dict[str, str]) -> None:
        await asyncio.to_thread(self._db.set_settings, kvs)

    async def insert_captcha(self, **fields: Any) -> int:
        return await asyncio.to_thread(self._db.insert_captcha, **fields)

    async def update_captcha(self, captcha_id: str, **fields: Any) -> None:
        await asyncio.to_thread(self._db.update_captcha, captcha_id, **fields)

    async def update_captcha_by_pk(self, pk: int, **fields: Any) -> None:
        await asyncio.to_thread(self._db.update_captcha_by_pk, pk, **fields)

    async def list_captchas(self, limit: int = 50, offset: int = 0) -> List[dict]:
        return await asyncio.to_thread(self._db.list_captchas, limit, offset)

    async def recent_events(self, limit: int = 60) -> List[dict]:
        return await asyncio.to_thread(self._db.recent_events, limit)

    async def add_event(self, level: str, kind: str, message: str,
                        captcha_id: str = "", extra: Any = None) -> int:
        return await asyncio.to_thread(self._db.add_event, level, kind, message,
                                       captcha_id, extra)

    async def bump_earnings(self, ts: float, status: str, type_: str,
                            profit: float, solve_ms: float) -> None:
        await asyncio.to_thread(self._db.bump_earnings, ts, status, type_, profit, solve_ms)

    async def earnings_range(self, days: int = 30) -> List[dict]:
        return await asyncio.to_thread(self._db.earnings_range, days)

    async def add_balance(self, balance: float, source: str = "") -> None:
        await asyncio.to_thread(self._db.add_balance, balance, source)

    async def balances_range(self, since_ts: Optional[float] = None) -> List[dict]:
        return await asyncio.to_thread(self._db.balances_range, since_ts)

    async def add_withdraw(self, amount: float, method: str = "", status: str = "requested",
                           message: str = "") -> int:
        return await asyncio.to_thread(self._db.add_withdraw, amount, method, status, message)

    async def list_withdraws(self, limit: int = 30) -> List[dict]:
        return await asyncio.to_thread(self._db.list_withdraws, limit)

    async def summary(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self._db.summary)


# ─── Module-level singleton helper ───────────────────────────────────

DB: Optional[AsyncDatabase] = None


def get_db() -> AsyncDatabase:
    global DB
    if DB is None:
        DB = AsyncDatabase()
    return DB
