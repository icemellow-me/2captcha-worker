# 🤖 2Captcha Worker Bot — Automated Captcha Solving Dashboard

Automated captcha worker for 2captcha.com that polls for captchas, routes them to **our self-hosted solver fleet**, and earns income passively — with a beautiful real-time dashboard and an OpenBullet2 config alternative.

---

## 📋 Table of Contents

1. [What It Does](#what-it-does)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Quick Start — Docker (recommended)](#quick-start--docker-recommended)
5. [Deploying the Solver Fleet](#deploying-the-solver-fleet)
6. [Dashboard Walkthrough](#dashboard-walkthrough)
7. [OpenBullet2 Alternative](#openbullet2-alternative)
8. [Environment Variables](#environment-variables)
9. [API Reference](#api-reference)
10. [Worker Rates & Income Projections](#worker-rates--income-projections)
11. [Troubleshooting](#troubleshooting)
12. [File Structure](#file-structure)

---

## What It Does

This system turns your 2captcha worker account into a **fully automated money-making machine**. Here's the flow:

1. **Polls 2captcha** for available captchas using your account key
2. **Routes each captcha** to the fastest appropriate solver in our fleet:
   - Image/base64 captchas → Universal OCR solver (ddddocr + Tesseract) — **~1.1s**
   - reCAPTCHA v2 → Playwright + CaptchaPlugin solver — **~2.2s**
   - Cloudflare Turnstile → nodriver + camoufox solver — **~8.2s**
   - Cloudflare JSD → Go/Fiber silent solver — **~65ms**
   - hCaptcha → hcaptcha-challenger + VLM
   - xCaptcha → VLM-based solver
3. **Submits answers** back to 2captcha automatically
4. **Tracks earnings** in real-time with a beautiful dark-themed dashboard
5. **Withdraw panel** — request payouts without logging into 2captcha
6. Runs 24/7 — earns while you sleep

All solvers run in Docker containers for isolation and consistency.

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │         Worker Dashboard (:8890)        │
                    │  Login → Stats → Charts → Solver Health │
                    │              → Withdraw → Settings      │
                    │                   ↓ API                 │
                    │              Worker Bot                 │
                    │  Poll 2captcha → Route to fleet → Submit│
                    └───┬──────────┬──────────┬──────────┬───┘
                        │          │          │          │
                        ▼          ▼          ▼          ▼
                   ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐
                   │ OCR  │  │Turn  │  │reCap │  │ JSD  │
                   │:8855 │  │:8878 │  │:8866 │  │:8191 │
                   └──────┘  └──────┘  └──────┘  └──────┘
                    ddddocr   nodriver   Playwright  Go
                   Tesseract  camoufox   CaptchaPlg  Fiber

                   ┌──────┐  ┌──────┐  ┌──────┐
                   │ xCap │  │ Ext  │  │ Ext  │
                   │      │  │:8844 │  │:8833 │
                   └──────┘  └──────┘  └──────┘
                    VLM      json=1    json=1
```

There are TWO ways to run the worker bot:

| Method | Description | Best For |
|--------|-------------|----------|
| **Python Dashboard** | `server.py` — beautiful web UI on port 8890 | Monitoring, stats, withdrawals, production |
| **OpenBullet2** | `.loli` config — runs inside OB2 web UI on port 8069 | Config customization, multi-account, parallel bots |

Both use the same solver fleet and the same 2captcha API.

---

## Prerequisites

- **Docker** installed on your VPS/server
- **2captcha account** — sign up at [2captcha.com](https://2captcha.com/auth/register) (free)
- Your **2captcha account key** (32-character string, found in account settings)
- The **solver fleet** running in Docker (see below)

---

## Quick Start — Docker (recommended)

### Step 1: Clone & Deploy

```bash
git clone https://github.com/icemellow-me/2captcha-worker.git
cd 2captcha-worker
```

### Step 2: Configure your key

Edit `docker-compose.yml` or create a `.env` file:

```bash
# Create .env file
cat > .env << 'EOF'
DASHBOARD_PASSWORD=your_secure_password
CAPTCHA_KEY=your_32_char_2captcha_key
SOLVER_UNIVERSAL=http://172.17.0.1:8855
SOLVER_TURNSTILE=http://172.17.0.1:8878
SOLVER_RECAPTCHA=http://172.17.0.1:8866
POLL_INTERVAL=1.0
EOF
```

### Step 3: Launch

```bash
docker-compose up -d
```

### Step 4: Open the dashboard

```
http://YOUR_SERVER_IP:8890
```

Login with your `DASHBOARD_PASSWORD` (default: `admin`)

You'll see:
- 💰 Earnings today (live counter)
- 🏦 Total balance (from 2captcha API)
- ✅ Total solved + success rate
- ⚡ Average solve time
- 📈 24h earnings chart
- 🎯 Captchas solved by type (doughnut chart)
- 📋 Recent activity table (live updates every 5s)
- ▶ Start/Stop worker button

### Step 5: Start the worker

Click **▶ Start Worker** on the dashboard. The bot will:
1. Check your 2captcha balance
2. Begin polling for captchas
3. Route each captcha to the fastest solver
4. Submit answers and track earnings

---

## Deploying the Solver Fleet

The worker dashboard needs the solver fleet running. If you haven't deployed them yet:

### Universal OCR Solver (port 8855)
```bash
# Handles image/base64/hCaptcha/coordinate captchas
# Uses ddddocr + Tesseract + Cloudflare Workers AI VLM
docker run -d --name universal-captcha-solver \
  -p 8855:8855 \
  --restart unless-stopped \
  universal-captcha-solver
```

### Turnstile Solver (port 8878)
```bash
# Solves Cloudflare Turnstile using nodriver (CDP) + camoufox
docker run -d --name turnstile-solver-v2 \
  -p 8878:8878 \
  --restart unless-stopped \
  turnstile-solver:latest
```

### reCAPTCHA v2 Solver (port 8866)
```bash
# Solves reCAPTCHA v2 using Playwright + CaptchaPlugin
docker run -d --name recaptcha-v2-solver \
  -p 8866:8866 \
  --restart unless-stopped \
  recaptcha-v2-solver
```

### Cloudflare JSD Solver (port 8191) — NEW
```bash
# Solves silent JSD challenges (Go/Fiber, no browser needed)
docker build -t cloudflare-jsd-solver ./cloudflare-jsd/
docker run -d --name cloudflare-jsd-solver \
  -p 8191:8191 \
  --restart unless-stopped \
  cloudflare-jsd-solver
```

### Verify all solvers are healthy
```bash
curl http://localhost:8855/health   # Universal OCR
curl http://localhost:8878/health   # Turnstile
curl http://localhost:8866/health   # reCAPTCHA v2
curl http://localhost:8191/         # Cloudflare JSD
```

All should return HTTP 200 with `"status":"ok"`.

### Solver API Key

All solvers (except JSD) use the same API key: `8010000000ccojr5nrbg516w5jvw1wu9` (CaptchaPlugin key). This is pre-configured in the worker bot.

---

## Dashboard Walkthrough

### 📊 Dashboard Tab (main)

The main dashboard shows 4 stat cards at the top:

| Card | What it shows | Updates |
|------|---------------|---------|
| 💰 Earnings Today | Total $ earned today | Every 5s |
| 🏦 Total Balance | Live balance from 2captcha API | Every 5s |
| ✅ Total Solved | Lifetime solved count + success rate | Every 5s |
| ⚡ Avg Solve Time | Average milliseconds per solve | Every 5s |

Below the cards:

- **Earnings Chart** — 24-hour line graph showing earnings per hour (Chart.js)
- **Captcha by Type** — Doughnut chart showing distribution of solved captcha types
- **Recent Activity** — Live table showing the last 20 captchas with type, status, reward, solve time
- **Start/Stop button** — Toggle the worker bot on/off

### 🔧 Solvers Tab

Shows real-time health status for all solver endpoints:

- Green border = Online
- Red border = Offline
- Shows solved count and active sessions per solver
- Speed benchmarks table with average solve times

### 💸 Withdraw Tab

- Shows your current 2captcha balance
- **One-click withdrawal request** — sends payout request to 2captcha
- Minimum payout: $0.50 (2captcha minimum)
- Shows withdrawal history and status

### ⚙️ Settings Tab

Configure the worker bot without editing files:

- **2Captcha API Key** — your 32-character account key
- **Poll Interval** — seconds between captcha polls (default: 1.0)
- **Solver URLs** — shows the URLs for each solver endpoint
- Changes are saved to the database and persist across restarts

---

## OpenBullet2 Alternative

OpenBullet2 provides an alternative way to run the worker bot — with the advantage of visual block editing, multi-account parallel execution, and proxy rotation.

### Step 1: Deploy OpenBullet2

```bash
docker run -d --name openbullet2 \
  -p 8069:5000 \
  -v ~/openbullet2/UserData:/app/UserData/ \
  --restart unless-stopped \
  openbullet/openbullet2:latest
```

Open `http://YOUR_SERVER_IP:8069` and create an admin account on first visit.

### Step 2: Import the config

1. Go to **Configs** → **New Config**
2. Name it: `2captcha-worker`
3. Go to **Config** → **Edit LoliCode**
4. Paste the contents of `openbullet/2captcha_worker.loli`
5. Save

### Step 3: Configure Custom Inputs

Go to **Config** → **Settings** → **Custom Inputs** and add:

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `captcha_key` | String | — | Your 2captcha account key |
| `captcha_type` | String | `image` | `image`, `recaptcha_v2`, or `turnstile` |
| `site_url` | String | — | Page URL (for reCAPTCHA/Turnstile) |
| `site_key` | String | — | GoogleKey/sitekey (for reCAPTCHA/Turnstile) |

### Step 4: Create a wordlist and run

1. Create a wordlist file with one line: `dummy:dummy`
2. Go to **Jobs** → **New Job**
3. Select the `2captcha-worker` config
4. Select your wordlist
5. Set **Bots** to 1 (or more for parallelism)
6. Set ** proxies** to None (our solvers handle this)
7. Click **Start**

### What the OB2 config does

The LoliCode config mirrors the Python dashboard exactly:

1. **Startup script** — Initializes global counters (`globals.SolvedCount`, `globals.TotalEarnings`)
2. **Balance check** — Queries 2captcha API for current balance
3. **Main loop** — Runs forever, solving captchas one after another:
   - Routes by type: image → OCR solver, reCAPTCHA → recaptcha solver, Turnstile → turnstile solver
   - Submits via `/in.php` (2captcha-compatible API)
   - Polls `/res.php` for the solution
   - Parses the answer from `OK|answer` format
   - Submits the answer back to 2captcha via rucaptcha.com
   - Updates global counters (thread-safe with `ACQUIRELOCK`)
   - Logs everything to OB2's logger with colors
4. **Captures** — Each solve captures: Status, CaptchaId, CaptchaType, Answer, Reward, SolveMs

### Both use `.svb` and `.loli` formats

| File | Format | Usage |
|------|--------|-------|
| `2captcha_worker.svb` | OpenBullet v1 (SilverBullet) | For OB1/SB users |
| `2captcha_worker.loli` | OpenBullet2 LoliCode | For OB2 (recommended) |

### Speed comparison

| Method | Setup time | Max parallelism | Monitoring |
|--------|------------|-----------------|------------|
| **Python Dashboard** | 30s (docker-compose) | Single-threaded | Beautiful web UI |
| **OpenBullet2** | 2min (OB2 + config import) | Multi-bot (10-50 parallel) | OB2's built-in logger + stats |
| **OB2 + Dashboard** | 2.5min | Best of both | OB2 for solving + Dashboard for monitoring |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_PASSWORD` | `admin` | Dashboard login password |
| `DASHBOARD_PORT` | `8890` | Dashboard web server port |
| `CAPTCHA_KEY` | — | Your 2captcha account key (32 chars) |
| `SOLVER_UNIVERSAL` | `http://172.17.0.1:8855` | Universal OCR solver URL |
| `SOLVER_TURNSTILE` | `http://172.17.0.1:8878` | Turnstile solver URL |
| `SOLVER_RECAPTCHA` | `http://172.17.0.1:8866` | reCAPTCHA v2 solver URL |
| `POLL_INTERVAL` | `1.0` | Seconds between captcha polls |
| `MIN_PAYOUT` | `0.5` | Minimum withdrawal amount (USD) |

> **`172.17.0.1`** is the Docker gateway IP — it allows containers to reach other containers' published ports. If running outside Docker, use `localhost` instead.

---

## API Reference

All endpoints are under `http://YOUR_SERVER:8890`.

### Authentication
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/login` | GET | Login page (HTML) |
| `/login` | POST | Authenticate (sets cookie, 24h expiry) |
| `/logout` | GET | Clear session |

### Dashboard
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard (protected — redirects to `/login` if not authed) |

### Worker API
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stats` | GET | Aggregated stats + live bot state |
| `/api/balance` | GET | Live 2captcha balance (tries v2 then v1 API) |
| `/api/start` | POST | Start the worker bot |
| `/api/stop` | POST | Stop the worker bot |
| `/api/withdraw` | POST | Request a withdrawal from 2captcha |
| `/api/settings` | GET | Get current settings |
| `/api/settings` | POST | Update settings (key, poll interval) |
| `/api/solver-health` | GET | Health check for all solver endpoints |

### Example: Start the worker

```bash
curl -X POST http://YOUR_SERVER:8890/api/start
# → {"status": "started"}
```

### Example: Get stats

```bash
curl http://YOUR_SERVER:8890/api/stats
# → {
#   "total_solved": 1542,
#   "total_failed": 23,
#   "total_earnings": 1.2345,
#   "earnings_today": 0.3210,
#   "solved_today": 410,
#   "success_rate": 98.5,
#   "avg_solve_ms": 1850,
#   "by_type": {
#     "image": {"count": 800, "earnings": 0.40, "avg_solve_ms": 1100},
#     "recaptcha_v2": {"count": 500, "earnings": 0.50, "avg_solve_ms": 2200},
#     "turnstile": {"count": 200, "earnings": 0.20, "avg_solve_ms": 8200}
#   },
#   "balance": 2.5000,
#   "running": true
# }
```

---

## Worker Rates & Income Projections

### 2captcha Worker Pay Rates

| Captcha Type | Rate per 1000 | Per captcha | Our Solve Speed |
|---------------|---------------|-------------|-----------------|
| Normal/Image | $0.50 | $0.0005 | ~1,100ms |
| reCAPTCHA v2 | $1.00 | $0.001 | ~2,200ms |
| Cloudflare Turnstile | $1.00 | $0.001 | ~8,200ms |
| hCaptcha | $1.00 | $0.001 | ~5,000ms |
| Text | $0.10 | $0.0001 | ~500ms |
| Coordinates | $0.70 | $0.0007 | ~1,500ms |

### Projected Daily Income

Assuming 24/7 operation with a single bot:

| Scenario | Captchas/day | Est. Daily Income | Monthly |
|----------|--------------|-------------------|---------|
| Image-only (fast) | ~70,000 | ~$35 | ~$1,050 |
| Mixed (70% image, 20% recaptcha, 10% turnstile) | ~40,000 | ~$28 | ~$840 |
| reCAPTCHA-focused | ~35,000 | ~$35 | ~$1,050 |
| Realistic (with failures + queue waits) | ~20,000 | ~$15 | ~$450 |

> **Note:** Actual income depends on 2captcha's captcha availability, your accuracy rate, and whether your account passes training. New accounts may get lower priority captchas initially.

### Multiple accounts

Run multiple containers with different keys for parallel income:

```bash
# Account 1
docker run -d --name 2captcha-worker-1 \
  -p 8890:8890 \
  -e CAPTCHA_KEY=key1... \
  2captcha-worker

# Account 2
docker run -d --name 2captcha-worker-2 \
  -p 8891:8890 \
  -e CAPTCHA_KEY=key2... \
  2captcha-worker
```

Or use OB2 with 10-50 bots in a single job for multi-account parallelism.

---

## Troubleshooting

### Dashboard shows "Could not fetch balance"

Your 2captcha key may be:
- A **worker-only** account (no customer API access) — the worker bot still works, earnings are tracked locally
- **Invalid** — double-check the key in Settings
- **Rate-limited** — wait a few minutes and restart

### Solvers show "Offline"

```bash
# Check if solver containers are running
docker ps | grep -E 'captcha|solver'

# If not, restart them
docker start universal-captcha-solver turnstile-solver-v2 recaptcha-v2-solver cloudflare-jsd-solver

# Verify connectivity from the worker container
docker exec 2captcha-worker curl -s http://172.17.0.1:8855/health
```

### "ERROR_WRONG_USER_KEY" when solving

The solver fleet uses the CaptchaPlugin API key (`8010...`), not your 2captcha key. This is pre-configured — don't change it in the worker bot.

### reCAPTCHA solver returns errors

The Playwright-based reCAPTCHA solver needs Chrome and can be memory-hungry. Check:

```bash
docker logs recaptcha-v2-solver --tail 30
# Look for "browser closed" or "Target page has been closed"
# Restart if needed
docker restart recaptcha-v2-solver
```

### OpenBullet2 config not working

1. Make sure custom inputs are set (captcha_key at minimum)
2. Check that solver URLs are reachable from the OB2 container
3. The container needs network access to `172.17.0.1:8855` etc — use `--network host` if bridging doesn't work

### Turnstile returns dummy token

The Turnstile solver may return `XXXX.DUMMY.TOKEN.XXXX` on some sites. This means the solver auto-completed the challenge but the site's specific Turnstile configuration wasn't solvable without a real browser session. Try the Cloudflare JSD solver instead for silent challenges.

---

## File Structure

```
2captcha-worker/
├── server.py                    # Dashboard web server (aiohttp, port 8890)
├── worker.py                    # Worker bot engine (polls, routes, submits)
├── database.py                  # SQLite stats storage (aiosqlite)
├── static/
│   └── index.html               # Dashboard UI (dark theme, Chart.js, mobile responsive)
├── openbullet/
│   ├── 2captcha_worker.loli     # OB2 LoliCode config (full worker automation)
│   └── 2captcha_worker.svb      # OB1/SilverBullet config (same logic)
├── requirements.txt             # Python dependencies (aiohttp, aiosqlite)
├── Dockerfile                   # Container build
├── docker-compose.yml           # Orchestration (port 8890, volume for data)
├── .env.example                 # Environment variable template
└── README.md                    # This file
```

---

## License

MIT — Use freely, no attribution required.

## Credits

- Solver fleet: ddddocr, nodriver, camoufox, Playwright, CaptchaPlugin
- Cloudflare JSD solver: by [@B00H0](https://t.me/HK407) — [captcha-solver-suite](https://github.com/icemellow-me/captcha-solver-suite)
- OpenBullet2: by [openbullet](https://github.com/openbullet)
- 2captcha API: [2captcha.com](https://2captcha.com/2captcha-api)
