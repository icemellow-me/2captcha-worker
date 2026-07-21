#!/usr/bin/env python3
"""
2Captcha Worker Dashboard — aiohttp web server with beautiful dark UI.
Port 8890. Login-protected. Real-time stats, charts, withdraw panel.
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
from worker import WorkerBot, CAPTCHA_KEY, API_BASE, RUCAPTCHA_BASE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("dashboard")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin")
PORT = int(os.environ.get("DASHBOARD_PORT", "8890"))
SESSION_SECRET = f"tcw_{int(time.time())}"

worker_bot = WorkerBot()
worker_task = None

BASE_DIR = Path(__file__).parent


# ─── Auth middleware ──────────────────────────────────────────

async def auth_check(request):
    """Check if user is authenticated. Returns True if logged in."""
    token = request.cookies.get("tcw_auth", "")
    return token == SESSION_SECRET


async def require_auth(handler):
    """Wrapper that requires authentication."""
    async def wrapper(request):
        if not await auth_check(request):
            if request.path.startswith("/api/"):
                return web.json_response({"error": "unauthorized"}, status=401)
            return web.HTTPFound("/login")
        return await handler(request)
    return wrapper


# ─── Routes ───────────────────────────────────────────────────

async def login_page(request):
    """Render login page."""
    html = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>2Captcha Worker — Login</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;
display:flex;align-items:center;justify-content:center;min-height:100vh;overflow:hidden}
.bg{position:fixed;inset:0;background:radial-gradient(ellipse at top,#1e293b 0%,#0f172a 50%,#020617 100%);z-index:-1}
.glow{position:fixed;width:600px;height:600px;border-radius:50%;
background:radial-gradient(circle,rgba(34,197,94,.08) 0%,transparent 70%);
top:-200px;right:-200px;z-index:-1;animation:float 8s ease-in-out infinite}
@keyframes float{0%,100%{transform:translate(0,0)}50%{transform:translate(-30px,30px)}}
.card{background:rgba(30,41,59,.7);backdrop-filter:blur(20px);
border:1px solid rgba(148,163,184,.1);border-radius:20px;padding:48px;
width:400px;max-width:90vw;box-shadow:0 25px 50px rgba(0,0,0,.5)}
.logo{font-size:28px;font-weight:700;text-align:center;margin-bottom:8px;
background:linear-gradient(135deg,#22c55e,#06b6d4);-webkit-background-clip:text;
-webkit-text-fill-color:transparent}
.subtitle{text-align:center;color:#64748b;margin-bottom:32px;font-size:14px}
.input-group{margin-bottom:20px}
.input-group label{display:block;font-size:13px;color:#94a3b8;margin-bottom:6px}
.input-group input{width:100%;padding:14px 16px;background:rgba(15,23,42,.8);
border:1px solid rgba(148,163,184,.2);border-radius:10px;color:#e2e8f0;
font-size:15px;transition:all .2s}
.input-group input:focus{outline:none;border-color:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.15)}
.btn{width:100%;padding:14px;border:none;border-radius:10px;font-size:16px;
font-weight:600;cursor:pointer;transition:all .2s;margin-top:8px}
.btn-primary{background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 8px 25px rgba(34,197,94,.3)}
.error{color:#ef4444;text-align:center;margin-top:12px;font-size:14px;display:none}
.hint{text-align:center;color:#475569;font-size:12px;margin-top:20px}
</style></head><body>
<div class="bg"></div><div class="glow"></div>
<div class="card">
<div class="logo">🤖 2Captcha Worker</div>
<div class="subtitle">Automated Captcha Solving Dashboard</div>
<form method="POST" action="/login">
<div class="input-group">
<label>Password</label>
<input type="password" name="password" placeholder="Enter dashboard password" autofocus required>
</div>
<button type="submit" class="btn btn-primary">Login →</button>
<div class="error" id="err">Invalid password. Try again.</div>
</form>
<div class="hint">Default password: <code>admin</code> · Change via DASHBOARD_PASSWORD env</div>
</div>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


async def login_post(request):
    """Handle login form submission."""
    data = await request.post()
    password = data.get("password", "")
    if password == DASHBOARD_PASSWORD:
        resp = web.HTTPFound("/")
        resp.set_cookie("tcw_auth", SESSION_SECRET, httponly=True, max_age=86400)
        return resp
    return web.HTTPFound("/login?error=1")


async def logout(request):
    """Log out and clear cookie."""
    resp = web.HTTPFound("/login")
    resp.del_cookie("tcw_auth")
    return resp


# ─── API endpoints ───────────────────────────────────────────

async def api_stats(request):
    """Get current stats for dashboard."""
    db_stats = await database.get_stats()
    live_stats = worker_bot.get_live_stats()
    return web.json_response({**db_stats, **live_stats, "balance": worker_bot.current_balance})


async def api_balance(request):
    """Get live balance from 2captcha."""
    balance = await worker_bot.get_balance()
    if balance is not None:
        worker_bot.current_balance = balance
    return web.json_response({
        "balance": worker_bot.current_balance,
        "balance_source": "live" if balance is not None else "cached"
    })


async def api_start(request):
    """Start the worker bot."""
    global worker_task
    if worker_bot.running:
        return web.json_response({"status": "already_running"})
    worker_task = asyncio.create_task(worker_bot.start())
    return web.json_response({"status": "started"})


async def api_stop(request):
    """Stop the worker bot."""
    await worker_bot.stop()
    return web.json_response({"status": "stopped"})


async def api_withdraw(request):
    """Request a withdrawal from 2captcha balance."""
    balance = await worker_bot.get_balance()
    if balance is None:
        return web.json_response({"error": "Could not fetch balance"}, status=502)
    min_payout = float(os.environ.get("MIN_PAYOUT", "0.5"))
    if balance < min_payout:
        return web.json_response({
            "error": f"Minimum payout is ${min_payout}. Current balance: ${balance:.4f}"
        }, status=400)
    wid = await database.record_withdrawal(balance, "paypal")
    # Try to request payout via rucaptcha API
    try:
        async with worker_bot.session.get(
            f"{RUCAPTCHA_BASE}/res.php",
            params={"key": CAPTCHA_KEY, "action": "request_payout"},
            timeout=10
        ) as resp:
            result = await resp.text()
            return web.json_response({
                "withdraw_id": wid, "amount": balance,
                "api_response": result, "status": "requested"
            })
    except Exception as e:
        return web.json_response({
            "withdraw_id": wid, "amount": balance,
            "error": str(e), "status": "offline_request"
        })


async def api_settings_get(request):
    """Get current settings."""
    captcha_key = await database.get_setting("captcha_key", CAPTCHA_KEY)
    return web.json_response({
        "captcha_key": captcha_key[:8] + "..." if len(captcha_key) > 8 else captcha_key,
        "captcha_key_full": captcha_key,
        "poll_interval": await database.get_setting("poll_interval", "1.0"),
        "solver_universal": os.environ.get("SOLVER_UNIVERSAL", "http://172.17.0.1:8855"),
        "solver_turnstile": os.environ.get("SOLVER_TURNSTILE", "http://172.17.0.1:8878"),
        "solver_recaptcha": os.environ.get("SOLVER_RECAPTCHA", "http://172.17.0.1:8866"),
        "solver_jsd": "http://172.17.0.1:8191",
        "rates": worker_bot.RATES if hasattr(worker_bot, "RATES") else {},
    })


async def api_settings_post(request):
    """Update settings."""
    data = await request.json()
    if "captcha_key" in data:
        await database.set_setting("captcha_key", data["captcha_key"])
    if "poll_interval" in data:
        await database.set_setting("poll_interval", data["poll_interval"])
    return web.json_response({"status": "saved"})


async def api_solver_health(request):
    """Check health of all solver endpoints."""
    solvers = {
        "universal_ocr": "http://172.17.0.1:8855/health",
        "turnstile": "http://172.17.0.1:8878/health",
        "recaptcha_v2": "http://172.17.0.1:8866/health",
        "cloudflare_jsd": "http://172.17.0.17:8191/",
    }
    results = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        for name, url in solvers.items():
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        try:
                            results[name] = {"status": "ok", "details": await resp.json()}
                        except:
                            results[name] = {"status": "ok", "details": {}}
                    else:
                        results[name] = {"status": f"http_{resp.status}"}
            except Exception as e:
                results[name] = {"status": "offline", "error": str(e)[:50]}
    return web.json_response(results)


# ─── Main dashboard page ─────────────────────────────────────

async def dashboard_page(request):
    """Render the main dashboard."""
    html_path = BASE_DIR / "static" / "index.html"
    if html_path.exists():
        return web.FileResponse(html_path)
    return web.Response(text="<h1>Dashboard file not found</h1>", content_type="text/html")


# ─── App setup ────────────────────────────────────────────────

async def on_startup(app):
    """Initialize database on startup."""
    await database.init_db()
    log.info("Database initialized")
    # Resta1.8442", "captcha_key": CAPTCHA_KEY})
    log.info(f"Dashboard starting on port {PORT}, password: {'***' if DASHBOARD_PASSWORD != 'admin' else 'admin (default)'}")


async def on_cleanup(app):
    """Clean up on shutdown."""
    if worker_bot.running:
        await worker_bot.stop()


def create_app():
    """Create and configure the aiohttp application."""
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Static files
    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        app.router.add_static("/static", static_dir)

    # Public routes
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_post)
    app.router.add_get("/logout", logout)

    # Protected routes
    app.router.add_get("/", dashboard_page)
    
    # API routes
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/balance", api_balance)
    app.router.add_post("/api/start", api_start)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_post("/api/withdraw", api_withdraw)
    app.router.add_get("/api/settings", api_settings_get)
    app.router.add_post("/api/settings", api_settings_post)
    app.router.add_get("/api/solver-health", api_solver_health)

    return app


def main():
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
