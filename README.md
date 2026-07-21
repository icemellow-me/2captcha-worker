# 🤖 2Captcha Worker Bot — Automated Captcha Solving Dashboard

Automated captcha worker for 2captcha.com that polls for captchas, routes them to **our self-hosted solver fleet**, and earns income passively — with a beautiful real-time dashboard.

## What It Does

1. **Polls 2captcha** for available captchas using your account key
2. **Routes each captcha** to the fastest appropriate solver in our fleet
3. **Submits answers** back to 2captcha automatically
4. **Tracks earnings** in real-time with a beautiful dashboard
5. **Withdraw panel** — request payouts without logging into 2captcha
6. Runs 24/7 — earns while you sleep

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                  Dashboard (:8890)                        │
│   Login → Stats → Charts → Solver Health → Withdraw      │
│                     ↓ API                                │
│                  Worker Bot                               │
│   Polls 2captcha → Routes to solver fleet → Submits       │
└────┬──────────┬──────────┬──────────┬──────────┬─────────┘
     │          │          │          │          │
     ▼          ▼          ▼          ▼          ▼
  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐
  │OCR   │  │Turn  │  │reCap │  │JSD   │  │xCapt │
  │:8855 │  │:8878 │  │:8866 │  │:8191 │  │      │
  └──────┘  └──────┘  └──────┘  └──────┘  └──────┘
   ddddocr   nodriver   Playwright  Go       VLM
   Tesseract  camoufox   CaptchaPlugin Fiber
```

## Quick Start

### Docker (recommended)

```bash
# Clone and run
docker-compose up -d

# Or build manually
docker build -t 2captcha-worker .
docker run -d --name 2captcha-worker \
  -p 8890:8890 \
  -e DASHBOARD_PASSWORD=admin \
  -e CAPTCHA_KEY=YOUR_2CAPTCHA_KEY \
  -e SOLVER_UNIVERSAL=http://172.17.0.1:8855 \
  -e SOLVER_TURNSTILE=http://172.17.0.1:8878 \
  -e SOLVER_RECAPTCHA=http://172.17.0.1:8866 \
  2captcha-worker
```

Open **http://localhost:8890** → Login with password (default: `admin`)

### Manual

```bash
pip install -r requirements.txt
python3 server.py
```

## Dashboard Pages

### 📊 Dashboard (main)
- **Earnings Today** — live counter
- **Total Balance** — from 2captcha API
- **Total Solved** + success rate
- **Avg Solve Time** — benchmarking
- **Earnings Chart** — 24h line graph
- **Captcha by Type** — doughnut chart
- **Recent Activity** — live table of solved/failed captchas
- Start/Stop worker toggle

### 🔧 Solvers
- Health check for all solver endpoints
- Speed benchmarks table
- Status indicators (online/offline)

### 💸 Withdraw
- Shows current balance
- One-click withdrawal request
- Minimum payout: $0.50

### ⚙️ Settings
- 2captcha API key
- Poll interval
- Solver endpoint URLs
- Worker rates per captcha type

## Solver Fleet

| Solver | Port | Type | Avg Speed | Key Used |
|--------|------|------|-----------|----------|
| Universal OCR (ddddocr + Tesseract) | 8855 | Image/Base64/hCaptcha | ~1,100ms | CaptchaPlugin key |
| Turnstile v2 (nodriver + camoufox) | 8878 | Cloudflare Turnstile | ~8,200ms | CaptchaPlugin key |
| reCAPTCHA v2 (Playwright) | 8866 | reCAPTCHA v2 | ~2,200ms | CaptchaPlugin key |
| Cloudflare JSD (Go/Fiber) | 8191 | JSD silent challenges | ~65ms | Proxy-based |
| xCaptcha Solver | internal | xCaptcha | — | — |
| Extension Universal (json=1) | 8844 | All types | — | CaptchaPlugin key |
| Extension reCAPTCHA (json=1) | 8833 | reCAPTCHA v2 | — | CaptchaPlugin key |
| Extension Turnstile (json=1) | 8822 | Turnstile | — | CaptchaPlugin key |

## 2Captcha Worker Rates

| Type | Rate per 1000 | Per captcha |
|------|---------------|-------------|
| Normal/Image | $0.50 | $0.0005 |
| reCAPTCHA v2 | $1.00 | $0.001 |
| Turnstile | $1.00 | $0.001 |
| hCaptcha | $1.00 | $0.001 |
| Text | $0.10 | $0.0001 |
| Coordinates | $0.70 | $0.0007 |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_PASSWORD` | `admin` | Dashboard login password |
| `DASHBOARD_PORT` | `8890` | Dashboard port |
| `CAPTCHA_KEY` | — | Your 2captcha account key |
| `SOLVER_UNIVERSAL` | `http://172.17.0.1:8855` | Universal OCR solver URL |
| `SOLVER_TURNSTILE` | `http://172.17.0.1:8878` | Turnstile solver URL |
| `SOLVER_RECAPTCHA` | `http://172.17.0.1:8866` | reCAPTCHA solver URL |
| `POLL_INTERVAL` | `1.0` | Seconds between polls |
| `MIN_PAYOUT` | `0.5` | Minimum withdrawal amount |

## Files

```
2captcha-worker/
├── server.py          # Dashboard web server (aiohttp)
├── worker.py          # Worker bot engine
├── database.py        # SQLite stats storage
├── static/
│   └── index.html     # Dashboard UI (dark theme, Chart.js)
├── requirements.txt   # Python deps
├── Dockerfile         # Container build
├── docker-compose.yml # Orchestration
└── README.md          # This file
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/login` | GET | Login page |
| `/login` | POST | Authenticate |
| `/` | GET | Dashboard (protected) |
| `/api/stats` | GET | Aggregated stats + live bot state |
| `/api/balance` | GET | Live 2captcha balance |
| `/api/start` | POST | Start worker bot |
| `/api/stop` | POST | Stop worker bot |
| `/api/withdraw` | POST | Request withdrawal |
| `/api/settings` | GET | Get settings |
| `/api/settings` | POST | Update settings |
| `/api/solver-health` | GET | Check all solver endpoints |

## OpenBullet Config

See `openbullet/2captcha_worker.svb` for an alternative OpenBullet-based automation config.

## License

MIT
