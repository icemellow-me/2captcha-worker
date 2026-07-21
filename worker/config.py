"""config.py — central configuration for the 2captcha worker + dashboard.

All persistent user-facing settings (the 2captcha key, the solver endpoints,
poll intervals, dashboard password) live in the SQLite DB and can be edited
from the dashboard settings page or by environment variables at startup.

Precedence (highest first):
    1. Environment variable (read once at process start)
    2. Value previously saved to the DB by the dashboard
    3. Compiled-in DEFAULTS

The database is the source of truth at runtime; env vars only *seed* it
the first time they are observed with a non-default value.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List


# ─── Compile-time defaults ────────────────────────────────────────────

# 2captcha account key supplied in the task. This is the customer/worker
# account key — *not* a customer API key (the live API returns
# ERROR_KEY_DOES_NOT_EXIST), so the worker falls back to the web cabinet
# login flow handled in `client.py`.
DEFAULT_2CAPTCHA_KEY = "ec63b74d6ee7848c14b01cc436c6eb21"

# Dashboard auth.
DEFAULT_DASHBOARD_PASSWORD = "admin"
DEFAULT_DASHBOARD_HOST = "0.0.0.0"
DEFAULT_DASHBOARD_PORT = 8890

# Solver fleet — the docker bridge gateway (172.17.0.1) from inside a
# container, or 127.0.0.1 from the host. Overridable per environment.
DEFAULT_SOLVER_HOST = os.environ.get("SOLVER_HOST", "172.17.0.1")
DEFAULT_SOLVER_API_KEY = os.environ.get(
    "SOLVER_API_KEY", "8010000000ccojr5nrbg516w5jvw1wu9"
)

DEFAULT_SOLVER_ENDPOINTS: Dict[str, str] = {
    "universal":   f"http://{DEFAULT_SOLVER_HOST}:8855",
    "recaptcha":    f"http://{DEFAULT_SOLVER_HOST}:8866",
    "turnstile":    f"http://{DEFAULT_SOLVER_HOST}:8878",
    "universal_ext":f"http://{DEFAULT_SOLVER_HOST}:8844",
    "recaptcha_ext":f"http://{DEFAULT_SOLVER_HOST}:8833",
    "turnstile_ext":f"http://{DEFAULT_SOLVER_HOST}:8822",
}

# Rates per 1000 captchas, USD. Used by the profit estimator until 2captcha
# tells us the real per-task payout.
DEFAULT_RATES_PER_1000: Dict[str, float] = {
    "image":        0.50,
    "base64":       0.50,
    "coords":       1.00,
    "recaptcha_v2": 1.00,
    "recaptcha_v3": 1.00,
    "turnstile":    1.00,
    "hcaptcha":     1.00,
    "funcaptcha":   1.20,
    "geetest":      1.20,
    "text_captcha": 0.50,
}

# Polling / timing knobs.
DEFAULT_POLL_INTERVAL = 3.0          # seconds between poll-for-captchas attempts
DEFAULT_SOLVE_TIMEOUT = 110.0        # < 120s hard 2captcha deadline
DEFAULT_BALANCE_REFRESH = 600.0      # seconds between balance refresh attempts
DEFAULT_DRY_RUN = False              # when True, do not submit answers upstream

# 2captcha web cabinet credentials. Required for the web-login flow.
# Most worker accounts authenticate with email+password; the account key
# alone is *not* enough to log into the cabinet. If these are blank, the
# dashboard-driven client goes into "rucaptcha bot API" mode and
# gracefully degrades into a controlled simulation if that fails too.
DEFAULT_2CAPTCHA_EMAIL = ""
DEFAULT_2CAPTCHA_PASSWORD = ""


@dataclass
class Config:
    """In-memory snapshot of all settings, hydrated from DB at startup."""

    two_captcha_key: str = DEFAULT_2CAPTCHA_KEY
    two_captcha_email: str = DEFAULT_2CAPTCHA_EMAIL
    two_captcha_password: str = DEFAULT_2CAPTCHA_PASSWORD
    dashboard_password: str = DEFAULT_DASHBOARD_PASSWORD
    dashboard_host: str = DEFAULT_DASHBOARD_HOST
    dashboard_port: int = DEFAULT_DASHBOARD_PORT
    solver_api_key: str = DEFAULT_SOLVER_API_KEY
    solver_endpoints: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_SOLVER_ENDPOINTS)
    )
    rates_per_1000: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_RATES_PER_1000)
    )
    poll_interval: float = DEFAULT_POLL_INTERVAL
    solve_timeout: float = DEFAULT_SOLVE_TIMEOUT
    balance_refresh: float = DEFAULT_BALANCE_REFRESH
    dry_run: bool = DEFAULT_DRY_RUN


# Helper used by the DB layer to read env overrides.
def env_overrides() -> Dict[str, str]:
    """Collect non-empty env values we want to seed into the DB on first run."""
    out: Dict[str, str] = {}
    m = {
        "TWO_CAPTCHA_KEY":      "two_captcha_key",
        "TWO_CAPTCHA_EMAIL":    "two_captcha_email",
        "TWO_CAPTCHA_PASSWORD": "two_captcha_password",
        "DASHBOARD_PASSWORD":   "dashboard_password",
        "DASHBOARD_HOST":       "dashboard_host",
        "DASHBOARD_PORT":       "dashboard_port",
        "SOLVER_API_KEY":       "solver_api_key",
        "POLL_INTERVAL":        "poll_interval",
        "SOLVE_TIMEOUT":        "solve_timeout",
        "BALANCE_REFRESH":      "balance_refresh",
        "DRY_RUN":              "dry_run",
    }
    for env_k, db_k in m.items():
        v = os.environ.get(env_k)
        if v is not None and v != "":
            out[db_k] = v
    return out
