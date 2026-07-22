#!/usr/bin/env python3
"""
2Captcha Worker Dashboard v2 — Multi-account support.
Port 8890. Login-protected. Real-time stats, charts, multi-account settings, withdraw panel.
"""

import asyncio
import aiohttp
import json
import os
import time
import logging
from aiohttp import web
from pathlib import Path

import database
from worker import WorkerBot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("dashboard")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin")
PORT = int(os.environ.get("DASHBOARD_PORT", "8890"))
SESSION_SECRET = f"tcw_{int(time.time())}"

worker_bot = WorkerBot()
worker_task = None
BASE_DIR = Path(__file__).parent

# Auto-load default account if provided
DEFAULT_KEY = os.environ.get("CAPTCHA_KEY", "")
if DEFAULT_KEY:
    worker_bot.add_account(DEFAULT_KEY, "Default Account")


async def auth_check(request):
    token = request.cookies.get("tcw_auth", "")
    return token == SESSION_SECRET


async def require_auth(handler):
    async def wrapper(request):
        if not await auth_check(request):
            if request.path.startswith("/api/"):
                return web.json_response({"error": "unauthorized"}, status=401)
            return web.HTTPFound("/login")
        return await handler(request)
    return wrapper


# ─── Auth Routes ─────────────────────────────────────────────

async def login_page(request):
    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🔐 Worker Login</title><style>
body{background:#0a0e27;color:#fff;font-family:system-ui;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.login{background:#141938;padding:40px;border-radius:16px;width:360px;text-align:center}
.login h1{font-size:1.4rem;margin:0 0 8px}
.login p{color:#888;font-size:0.85rem;margin:0 0 24px}
.login input{width:100%;padding:12px;border:1px solid #2a3158;border-radius:8px;background:#1a1f42;color:#fff;font-size:0.9rem;box-sizing:border-box;margin-bottom:12px}
.login button{width:100%;padding:12px;border:none;border-radius:8px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-weight:600;cursor:pointer;font-size:0.95rem}
.login button:hover{opacity:0.9}
</style></head><body><div class="login"><h1>🤖 2Captcha Worker</h1><p>Enter dashboard password</p>
<form method="POST" action="/login"><input type="password" name="password" placeholder="Password" autofocus><button type="submit">Login →</button></form></div></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def login_submit(request):
    data = await request.post()
    password = data.get("password", "")
    if password == DASHBOARD_PASSWORD:
        resp = web.HTTPFound("/")
        resp.set_cookie("tcw_auth", SESSION_SECRET, httponly=True, max_age=86400)
        return resp
    return web.HTTPFound("/login")


async def logout(request):
    resp = web.HTTPFound("/login")
    resp.del_cookie("tcw_auth")
    return resp


# ─── API Routes ──────────────────────────────────────────────

async def api_stats(request):
    if not await auth_check(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    stats = await database.get_stats()
    agg = worker_bot.get_aggregate_stats()
    stats.update(agg)
    stats["accounts"] = worker_bot.get_accounts_summary()
    return web.json_response(stats)


async def api_balance(request):
    if not await auth_check(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    await worker_bot.refresh_balances()
    return web.json_response(worker_bot.get_accounts_summary())


async def api_start(request):
    global worker_task
    if not await auth_check(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    if not worker_bot.accounts:
        return web.json_response({"error": "No accounts configured"}, status=400)
    if worker_task and not worker_task.done():
        return web.json_response({"status": "already_running"})
    worker_task = asyncio.create_task(worker_bot.start())
    return web.json_response({"status": "started", "accounts": len(worker_bot.accounts)})


async def api_stop(request):
    if not await auth_check(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    await worker_bot.stop()
    return web.json_response({"status": "stopped"})


async def api_add_account(request):
    if not await auth_check(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    data = await request.json()
    thash = data.get("thash", "").strip()
    label = data.get("label", "").strip()
    if not thash:
        return web.json_response({"error": "thash is required"}, status=400)
    if worker_bot.add_account(thash, label):
        # Try to login immediately to verify
        if not worker_bot.session:
            worker_bot.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        info = worker_bot.accounts[thash]
        ok = await worker_bot._login(info)
        if ok:
            await database.save_account(thash, info.label, info.email, info.user_id, info.balance, info.reputation)
            return web.json_response({"status": "ok", "account": {
                "label": info.label, "email": info.email, "balance": info.balance, "reputation": info.reputation
            }})
        else:
            worker_bot.remove_account(thash)
            return web.json_response({"error": "Login failed — invalid thash"}, status=400)
    return web.json_response({"error": "Account already exists"}, status=400)


async def api_remove_account(request):
    if not await auth_check(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    data = await request.json()
    thash = data.get("thash", "")
    if thash == os.environ.get("CAPTCHA_KEY", ""):
        return web.json_response({"error": "Cannot remove default account"}, status=400)
    worker_bot.remove_account(thash)
    await database.remove_account(thash)
    return web.json_response({"status": "removed"})


async def api_accounts(request):
    if not await auth_check(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"accounts": worker_bot.get_accounts_summary()})


async def api_health(request):
    solvers = {
        "universal_ocr": os.environ.get("SOLVER_UNIVERSAL", "http://172.17.0.1:8855"),
        "turnstile": os.environ.get("SOLVER_TURNSTILE", "http://172.17.0.1:8878"),
        "recaptcha": os.environ.get("SOLVER_RECAPTCHA", "http://172.17.0.1:8866"),
    }
    results = {}
    async with aiohttp.ClientSession() as s:
        for name, url in solvers.items():
            try:
                async with s.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    results[name] = {"online": r.status == 200, "url": url}
            except:
                results[name] = {"online": False, "url": url}
    return web.json_response(results)


# ─── Dashboard HTML ──────────────────────────────────────────

async def dashboard_page(request):
    if not await auth_check(request):
        return web.HTTPFound("/login")
    html_path = BASE_DIR / "static" / "index.html"
    if html_path.exists():
        return web.FileResponse(html_path)
    return web.Response(text="<h1>Dashboard file not found</h1>", content_type="text/html")


# ─── App Setup ───────────────────────────────────────────────

async def on_startup(app):
    await database.init_db()
    # Load saved accounts from database
    saved = await database.get_accounts()
    for acc in saved:
        if acc["thash"] not in worker_bot.accounts:
            worker_bot.add_account(acc["thash"], acc.get("label", ""))
    log.info(f"📊 Dashboard starting on port {PORT}, password: {DASHBOARD_PASSWORD}")
    log.info(f"📋 Loaded {len(worker_bot.accounts)} account(s)")


async def on_cleanup(app):
    await worker_bot.stop()
    if worker_bot.session:
        await worker_bot.session.close()


def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_submit)
    app.router.add_get("/logout", logout)
    app.router.add_get("/", dashboard_page)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/balance", api_balance)
    app.router.add_post("/api/start", api_start)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_post("/api/accounts/add", api_add_account)
    app.router.add_post("/api/accounts/remove", api_remove_account)
    app.router.add_get("/api/accounts", api_accounts)
    app.router.add_get("/api/solver-health", api_health)
    app.router.add_static("/static", BASE_DIR / "static")
    return app


if __name__ == "__main__":
    web.run_app(create_app(), port=PORT, host="0.0.0.0")
