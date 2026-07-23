#!/usr/bin/env python3
"""
2Captcha Worker Bot — Core Worker Engine (v2)
Uses the REAL 2captcha worker cabinet API (reverse-engineered from JS).
Supports multiple accounts simultaneously.
"""

import asyncio
import aiohttp
import base64
import hashlib
import time
import json
import logging
import os
from typing import Optional, Dict, List
from dataclasses import dataclass, field

# Auto-training module
try:
    from training import complete_training, load_auth_cookies
    TRAINING_AVAILABLE = True
except ImportError:
    TRAINING_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("2captcha-worker")

# ─── Configuration ────────────────────────────────────────────

SOLVER_UNIVERSAL = os.environ.get("SOLVER_UNIVERSAL", "http://172.17.0.1:8855")
SOLVER_TURNSTILE = os.environ.get("SOLVER_TURNSTILE", "http://172.17.0.1:8878")
SOLVER_RECAPTCHA = os.environ.get("SOLVER_RECAPTCHA", "http://172.17.0.1:8866")
SOLVER_XCAPTCHA = os.environ.get("SOLVER_XCAPTCHA", "http://172.17.0.1:8899")
SOLVER_JSD = os.environ.get("SOLVER_JSD", "http://172.17.0.1:8191")
SOLVER_API_KEY = os.environ.get("SOLVER_API_KEY", "8010000000ccojr5nrbg516w5jvw1wu9")

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
MAX_SOLVE_TIME = 110
# Safety margin: skip captcha this many seconds BEFORE the actual timeout
# so we always have time to call closerecaptchabot and avoid account blocks
TIMEOUT_SAFETY_MARGIN = int(os.environ.get("TIMEOUT_SAFETY_MARGIN", "5"))

# 2captcha worker cabinet API
CABINET_BASE = "2captcha.com"
CABINET_VERSION = "interface:5"
SALT_PREFIX = "67y89ikojigf+"

# Worker rates per captcha type
RATES = {
    "captcha": 0.0005, "xcaptcha": 0.0005, "recaptcha": 0.001,
    "turnstile": 0.001, "hcaptcha": 0.001, "coordinates": 0.0007,
    "text": 0.0001,
}


def compute_salt(captcha_id: str) -> str:
    """Compute the salt for a captcha ID."""
    return hashlib.md5(f"{SALT_PREFIX}{captcha_id}".encode()).hexdigest()


@dataclass
class AccountInfo:
    """2captcha worker account info."""
    thash: str
    label: str = ""
    user_id: int = 0
    email: str = ""
    balance: float = 0.0
    reputation: float = 0.0
    solved: int = 0
    failed: int = 0
    earnings: float = 0.0
    running: bool = False
    status: str = "idle"  # idle, polling, training, banned, parallel, error
    last_captcha_id: str = ""
    last_captcha_type: str = ""
    last_solve_time_ms: int = 0
    last_error: str = ""
    history: List[dict] = field(default_factory=list)


class WorkerBot:
    """2captcha worker bot supporting multiple accounts."""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.accounts: Dict[str, AccountInfo] = {}
        self.running = False
        self._stop_event = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self.total_solved = 0
        self.total_failed = 0
        self.total_earnings = 0.0

    def add_account(self, thash: str, label: str = "") -> bool:
        """Add a 2captcha worker account by thash."""
        thash = thash.strip()
        if not thash or len(thash) < 16:
            return False
        if thash in self.accounts:
            return False
        self.accounts[thash] = AccountInfo(thash=thash, label=label or f"Account-{len(self.accounts)+1}")
        log.info(f"➕ Added account: {self.accounts[thash].label} (thash: {thash[:8]}...)")
        return True

    def remove_account(self, thash: str) -> bool:
        """Remove an account."""
        if thash in self.accounts:
            info = self.accounts.pop(thash)
            log.info(f"➖ Removed account: {info.label}")
            return True
        return False

    def get_accounts_summary(self) -> list:
        """Get summary of all accounts for the dashboard."""
        return [{
            "thash": a.thash,
            "label": a.label,
            "user_id": a.user_id,
            "email": a.email,
            "balance": a.balance,
            "reputation": a.reputation,
            "solved": a.solved,
            "failed": a.failed,
            "earnings": round(a.earnings, 6),
            "running": a.running,
            "status": a.status,
            "last_error": a.last_error,
            "last_captcha_id": a.last_captcha_id,
            "last_captcha_type": a.last_captcha_type,
            "last_solve_time_ms": a.last_solve_time_ms,
        } for a in self.accounts.values()]

    def get_aggregate_stats(self) -> dict:
        """Get aggregate stats across all accounts."""
        return {
            "total_solved": sum(a.solved for a in self.accounts.values()),
            "total_failed": sum(a.failed for a in self.accounts.values()),
            "total_earnings": round(sum(a.earnings for a in self.accounts.values()), 6),
            "total_balance": round(sum(a.balance for a in self.accounts.values()), 6),
            "account_count": len(self.accounts),
            "active_accounts": sum(1 for a in self.accounts.values() if a.running),
            "running": self.running,
        }

    async def start(self):
        """Start the worker bot with all accounts."""
        if not self.accounts:
            log.warning("⚠️ No accounts configured — add accounts first")
            return
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        self.running = True
        self._stop_event.clear()
        log.info(f"🚀 Worker bot started with {len(self.accounts)} account(s)")
        for thash, info in self.accounts.items():
            await self._login(info)
        self._tasks = [asyncio.create_task(self._account_loop(info)) for info in self.accounts.values()]
        await self._stop_event.wait()
        for t in self._tasks:
            t.cancel()
        await self.session.close()
        log.info("🛑 Worker bot stopped")

    async def _login(self, info: AccountInfo):
        """Login to the 2captcha worker cabinet."""
        try:
            async with self.session.post(
                f"https://{CABINET_BASE}/captcha_api.php?action=login&thash={info.thash}&v={CABINET_VERSION}",
                headers={"Referer": f"https://{CABINET_BASE}/play-and-earn/play"},
            ) as resp:
                data = await resp.json()
                if data.get("status") == 1:
                    d = data.get("data", {})
                    info.user_id = d.get("user_id", 0)
                    info.email = d.get("email", "")
                    info.balance = float(d.get("balance", 0))
                    info.reputation = float(d.get("reputation", 0))
                    log.info(f"✅ Login [{info.label}]: {info.email} balance=${info.balance:.5f} rep={info.reputation}")
                    return True
                else:
                    log.error(f"❌ Login failed [{info.label}]: {data.get('error', 'unknown')}")
                    return False
        except Exception as e:
            log.error(f"❌ Login error [{info.label}]: {e}")
            return False

    async def _get_captcha(self, info: AccountInfo) -> tuple:
        """
        Fetch a captcha from the worker cabinet.
        
        Returns:
            (captcha_data, error_code) tuple.
            - (dict, None) = got a captcha
            - (None, None) = no captcha available
            - (None, "TRAINING") = training required
            - (None, "PARALLEL") = parallel usage detected
            - (None, "BANNED") = account banned
        """
        try:
            async with self.session.get(
                f"https://{CABINET_BASE}/captcha_api.php",
                params={
                    "action": "getbot",
                    "captcha_type": "0",
                    "captchatype": "captcha",
                    "v": CABINET_VERSION,
                    "thread": "0",
                    "thash": info.thash,
                },
                headers={"Referer": f"https://{CABINET_BASE}/play-and-earn/play"},
            ) as resp:
                data = await resp.json()
                if data.get("status") == 1:
                    cap_data = data.get("data", {})
                    info.balance = float(data.get("balance", info.balance))
                    info.reputation = float(data.get("reputation", info.reputation))
                    if isinstance(cap_data, dict) and "captcha_id" in cap_data:
                        return cap_data, None
                    return None, None
                else:
                    error_code = data.get("error_code", "")
                    error_msg = data.get("error", "")
                    if error_code == "ERROR_CABINET_PASS_LEVEL_TRAINING":
                        log.warning(f"📋 [{info.label}] Training required: {error_msg[:80]}")
                        return None, "TRAINING"
                    elif error_code == "ERROR_PARALLEL_PROGRAM_USAGE":
                        log.warning(f"⚠️ [{info.label}] Parallel usage detected — waiting 60s")
                        return None, "PARALLEL"
                    elif error_code == "ERROR_CABINET_USER_BANNED":
                        log.error(f"🚫 [{info.label}] Account banned: {error_msg[:80]}")
                        return None, "BANNED"
                    else:
                        log.debug(f"[{info.label}] getbot status -1: {error_code}")
                        return None, None
        except Exception as e:
            log.debug(f"getbot error [{info.label}]: {e}")
            return None, None

    async def _submit_answer(self, info: AccountInfo, captcha_id: str, answer: str, confirmed: int = 1) -> dict:
        """Submit an answer to the worker cabinet."""
        salt = compute_salt(captcha_id)
        payload = {
            "action": "sendrecaptchabot",
            "v": CABINET_VERSION,
            "thash": info.thash,
            f"code[{captcha_id}]": answer,
            f"saltids[{captcha_id}]": salt,
            "confirmed": str(confirmed),
        }
        try:
            async with self.session.post(
                f"https://{CABINET_BASE}/captcha_api.php",
                data=payload,
                headers={"Referer": f"https://{CABINET_BASE}/play-and-earn/play"},
            ) as resp:
                return await resp.json()
        except Exception as e:
            log.error(f"Submit error [{info.label}]: {e}")
            return {"status": 0, "error": str(e)}

    async def _skip_captcha(self, info: AccountInfo, captcha_id: str) -> dict:
        """Skip/close a captcha."""
        salt = compute_salt(captcha_id)
        payload = {
            "action": "closerecaptchabot",
            "v": CABINET_VERSION,
            "thash": info.thash,
            f"ids[{captcha_id}]": captcha_id,
            f"saltids[{captcha_id}]": salt,
            "close": "0",
        }
        try:
            async with self.session.post(
                f"https://{CABINET_BASE}/captcha_api.php",
                data=payload,
                headers={"Referer": f"https://{CABINET_BASE}/play-and-earn/play"},
            ) as resp:
                return await resp.json()
        except Exception as e:
            log.debug(f"Skip error [{info.label}]: {e}")
            return {"status": 0, "error": str(e)}

    async def _solve_image_captcha(self, image_b64: str) -> Optional[str]:
        """Solve an image captcha using our OCR solver fleet."""
        b64_data = image_b64.split(",", 1)[1] if "," in image_b64 else image_b64
        try:
            async with self.session.post(
                f"{SOLVER_UNIVERSAL}/in.php",
                data={"key": SOLVER_API_KEY, "method": "base64", "body": b64_data},
            ) as resp:
                result = await resp.text()
                if result.startswith("OK|"):
                    task_id = result.split("|", 1)[1]
                    for _ in range(15):
                        await asyncio.sleep(1)
                        async with self.session.get(
                            f"{SOLVER_UNIVERSAL}/res.php",
                            params={"key": SOLVER_API_KEY, "id": task_id},
                        ) as r:
                            res = await r.text()
                            if res.startswith("OK|"):
                                return res.split("|", 1)[1]
                            if "CAPCHA_NOT_READY" not in res:
                                return None
                return None
        except Exception as e:
            log.error(f"OCR solver error: {e}")
            return None

    async def _solve_recaptcha(self, sitekey: str, pageurl: str) -> Optional[str]:
        """Solve reCAPTCHA v2 using our solver fleet."""
        try:
            async with self.session.post(
                f"{SOLVER_RECAPTCHA}/in.php",
                data={"key": SOLVER_API_KEY, "method": "userrecaptcha",
                      "googlekey": sitekey, "pageurl": pageurl},
            ) as resp:
                result = await resp.text()
                if result.startswith("OK|"):
                    task_id = result.split("|", 1)[1]
                    for _ in range(50):
                        await asyncio.sleep(2)
                        async with self.session.get(
                            f"{SOLVER_RECAPTCHA}/res.php",
                            params={"key": SOLVER_API_KEY, "id": task_id},
                        ) as r:
                            res = await r.text()
                            if res.startswith("OK|"):
                                return res.split("|", 1)[1]
                            if "CAPCHA_NOT_READY" not in res:
                                return None
                return None
        except Exception as e:
            log.error(f"reCAPTCHA solver error: {e}")
            return None

    async def _solve_turnstile(self, sitekey: str, pageurl: str) -> Optional[str]:
        """Solve Cloudflare Turnstile using our solver fleet."""
        try:
            async with self.session.post(
                f"{SOLVER_TURNSTILE}/in.php",
                data={"key": SOLVER_API_KEY, "method": "turnstile",
                      "sitekey": sitekey, "pageurl": pageurl},
            ) as resp:
                result = await resp.text()
                if result.startswith("OK|"):
                    task_id = result.split("|", 1)[1]
                    for _ in range(50):
                        await asyncio.sleep(2)
                        async with self.session.get(
                            f"{SOLVER_TURNSTILE}/res.php",
                            params={"key": SOLVER_API_KEY, "id": task_id},
                        ) as r:
                            res = await r.text()
                            if res.startswith("OK|"):
                                return res.split("|", 1)[1]
                            if "CAPCHA_NOT_READY" not in res:
                                return None
                return None
        except Exception as e:
            log.error(f"Turnstile solver error: {e}")
            return None

    async def _solve_xcaptcha(self, captcha_data: dict) -> Optional[str]:
        """
        Solve xCaptcha (wcaptcha) challenges using our xcaptcha solver fleet.
        2captcha sends xcaptcha tasks with an image (base64 of the xCaptcha grid)
        and sometimes a sitekey/pageurl.

        NOTE: The xcaptcha solver may return either:
        - 2captcha format: "OK|task_id" (newer code)
        - JSON format: {"status": 1, "request": "task_id"} (older running code)
        We handle both.
        """
        image_b64 = captcha_data.get("image", "")
        sitekey = captcha_data.get("sitekey", "")
        pageurl = captcha_data.get("pageurl", "")

        # If we have a sitekey, use the xcaptcha solver with that sitekey
        if not sitekey:
            # Use the default text-type sitekey from xcaptcha solver config
            sitekey = "11aa62606fb968f3674742df60598957"  # text type default

        try:
            async with self.session.post(
                f"{SOLVER_XCAPTCHA}/in.php",
                data={
                    "key": SOLVER_API_KEY,
                    "method": "wcaptcha",
                    "sitekey": sitekey,
                    "pageurl": pageurl or "https://xcaptcha.com",
                },
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")
                body = await resp.text()

                # Parse the response — handle both OK| format and JSON format
                task_id = None
                if body.startswith("OK|"):
                    task_id = body.split("|", 1)[1]
                elif "application/json" in content_type:
                    try:
                        data = json.loads(body)
                        if data.get("status") == 1:
                            task_id = data.get("request", "")
                    except json.JSONDecodeError:
                        pass

                if not task_id:
                    log.warning(f"xcaptcha solver rejected: {body[:100]}")
                    return None

                # Poll for result — xcaptcha solver takes ~9s
                for _ in range(30):
                    await asyncio.sleep(1)
                    async with self.session.get(
                        f"{SOLVER_XCAPTCHA}/res.php",
                        params={"key": SOLVER_API_KEY, "id": task_id},
                    ) as r:
                        res = await r.text()
                        if res.startswith("OK|"):
                            return res.split("|", 1)[1]
                        if "CAPCHA_NOT_READY" not in res:
                            # Try JSON format too
                            try:
                                res_data = json.loads(res)
                                if res_data.get("status") == 1:
                                    return res_data.get("request", "")
                            except (json.JSONDecodeError, TypeError):
                                pass
                            log.warning(f"xcaptcha solver error: {res[:100]}")
                            return None
                return None
        except Exception as e:
            log.error(f"xCaptcha solver error: {e}")
            return None

    async def _solve_hcaptcha(self, sitekey: str, pageurl: str) -> Optional[str]:
        """Solve hCaptcha using the universal solver (supports hcaptcha via hcaptcha-challenger)."""
        try:
            async with self.session.post(
                f"{SOLVER_UNIVERSAL}/in.php",
                data={"key": SOLVER_API_KEY, "method": "hcaptcha",
                      "sitekey": sitekey, "pageurl": pageurl},
            ) as resp:
                result = await resp.text()
                if result.startswith("OK|"):
                    task_id = result.split("|", 1)[1]
                    for _ in range(60):
                        await asyncio.sleep(2)
                        async with self.session.get(
                            f"{SOLVER_UNIVERSAL}/res.php",
                            params={"key": SOLVER_API_KEY, "id": task_id},
                        ) as r:
                            res = await r.text()
                            if res.startswith("OK|"):
                                return res.split("|", 1)[1]
                            if "CAPCHA_NOT_READY" not in res:
                                return None
                return None
        except Exception as e:
            log.error(f"hCaptcha solver error: {e}")
            return None

    async def _solve_cloudflare_jsd(self, pageurl: str, **kwargs) -> Optional[str]:
        """Solve Cloudflare JS challenge / Just Dance using the JSD solver at port 8191."""
        # The JSD solver uses a different API: POST /v1 with JSON body
        try:
            async with self.session.post(
                f"{SOLVER_JSD}/v1",
                json={
                    "cmd": "request.get",
                    "url": pageurl,
                    "max_timeout": 60000,
                },
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                status = data.get("status", {})
                if status.get("status") == "ok":
                    # Extract cf_clearance cookie or token
                    return data.get("solution", {}).get("url", "")
                return None
        except Exception as e:
            log.error(f"JSD/Cloudflare solver error: {e}")
            return None

    async def _solve_captcha(self, captcha_data: dict, deadline: float = 0) -> tuple:
        """
        Route captcha to appropriate solver. Returns (answer, solve_time_ms).
        If deadline is set, cancels early so caller can skip before timeout.
        """
        ctype = captcha_data.get("captchatype", "captcha")
        start = time.time()

        # Check if we still have time before the deadline
        if deadline > 0:
            remaining = deadline - time.time()
            if remaining < 2:
                log.warning(f"⏰ Only {remaining:.1f}s left before deadline — not enough time to solve")
                return None, 0

        if ctype in ("captcha", "normal"):
            # Plain text/image captcha — use universal OCR
            img = captcha_data.get("image", "")
            if img:
                answer = await self._solve_image_captcha(img)
            else:
                return None, 0

        elif ctype == "xcaptcha":
            # xCaptcha (wcaptcha emoji grid) — route to xcaptcha solver
            answer = await self._solve_xcaptcha(captcha_data)

        elif ctype in ("recaptcha",):
            pageurl = captcha_data.get("pageurl", "")
            sitekey = captcha_data.get("sitekey", "")
            answer = await self._solve_recaptcha(sitekey, pageurl)

        elif ctype in ("turnstile",):
            pageurl = captcha_data.get("pageurl", "")
            sitekey = captcha_data.get("sitekey", "")
            answer = await self._solve_turnstile(sitekey, pageurl)

        elif ctype in ("hcaptcha",):
            pageurl = captcha_data.get("pageurl", "")
            sitekey = captcha_data.get("sitekey", "")
            answer = await self._solve_hcaptcha(sitekey, pageurl)

        elif ctype in ("coordinates", "click"):
            # Click captcha — use universal OCR for coordinate detection
            img = captcha_data.get("image", "")
            if img:
                answer = await self._solve_image_captcha(img)
            else:
                return None, 0

        elif ctype in ("text",):
            # Text captcha — use universal OCR
            img = captcha_data.get("image", "")
            if img:
                answer = await self._solve_image_captcha(img)
            else:
                return None, 0

        else:
            log.warning(f"⚠️ Unknown captcha type: {ctype}")
            return None, 0

        elapsed = int((time.time() - start) * 1000)
        return answer, elapsed

    async def _account_loop(self, info: AccountInfo):
        """Main polling loop for a single account."""
        info.running = True
        log.info(f"🔄 [{info.label}] Polling for captchas...")
        info.status = "polling"
        training_in_progress = False
        last_parallel_check = 0
        while self.running and not self._stop_event.is_set():
            try:
                captcha_data, error_code = await self._get_captcha(info)
                
                # ── Handle error codes ────────────────────────────────
                if error_code == "TRAINING":
                    if not TRAINING_AVAILABLE:
                        log.error(f"📋 [{info.label}] Training required but module not available — will retry in 60s")
                        info.status = "error"
                        info.last_error = "Training required but module not available"
                        await asyncio.sleep(60)
                        continue
                    if training_in_progress:
                        await asyncio.sleep(30)
                        continue
                    training_in_progress = True
                    info.status = "training"
                    log.info(f"📚 [{info.label}] Starting auto-training...")
                    result = await complete_training(self.session)
                    training_in_progress = False
                    if result.get("success"):
                        log.info(f"✅ [{info.label}] Training completed! Level: {result.get('level', '?')}")
                        info.status = "polling"
                        info.last_error = ""
                        # Re-login to refresh status
                        await self._login(info)
                    else:
                        log.error(f"❌ [{info.label}] Training failed: {result.get('error', 'unknown')}")
                        info.status = "error"
                        info.last_error = f"Training failed: {result.get('error', 'unknown')}"
                        await asyncio.sleep(60)
                    continue
                
                elif error_code == "PARALLEL":
                    now = time.time()
                    info.status = "parallel"
                    info.last_error = "Parallel usage detected"
                    if now - last_parallel_check > 60:
                        log.warning(f"⚠️ [{info.label}] Parallel usage — another session may be active. Waiting 60s...")
                        last_parallel_check = now
                    await asyncio.sleep(60)
                    continue
                
                elif error_code == "BANNED":
                    log.error(f"🚫 [{info.label}] Account is banned. Suspending this account's loop.")
                    info.status = "banned"
                    info.last_error = "Account banned — waiting for moderator review"
                    info.running = False
                    return
                
                if captcha_data is None:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                captcha_id = str(captcha_data.get("captcha_id", ""))
                ctype = captcha_data.get("captchatype", "captcha")
                rate = float(captcha_data.get("rate", RATES.get(ctype, 0.0005)))
                timeout = int(captcha_data.get("timeout", 60))

                info.last_captcha_id = captcha_id
                info.last_captcha_type = ctype
                log.info(f"📥 [{info.label}] Got captcha #{captcha_id} type={ctype} rate=${rate:.5f} timeout={timeout}s")

                # ── Calculate deadline ────────────────────────────────
                # We must submit or skip BEFORE the captcha times out.
                # Safety margin = TIMEOUT_SAFETY_MARGIN seconds before the actual timeout.
                # This gives us time to call closerecaptchabot (skip) if solving fails/takes too long.
                solve_deadline = time.time() + max(timeout - TIMEOUT_SAFETY_MARGIN, 10)
                remaining_for_solve = solve_deadline - time.time()

                log.info(f"⏱️ [{info.label}] Solve deadline: {remaining_for_solve:.1f}s (timeout={timeout}s, margin={TIMEOUT_SAFETY_MARGIN}s)")

                # ── Attempt to solve with deadline ───────────────────
                answer = None
                solve_ms = 0
                try:
                    answer, solve_ms = await asyncio.wait_for(
                        self._solve_captcha(captcha_data, deadline=solve_deadline),
                        timeout=remaining_for_solve,
                    )
                except asyncio.TimeoutError:
                    log.warning(f"⏰ [{info.label}] Solve timed out after {remaining_for_solve:.1f}s — skipping captcha #{captcha_id}")
                    answer = None
                except asyncio.CancelledError:
                    log.warning(f"⏰ [{info.label}] Solve cancelled for #{captcha_id} — deadline reached")
                    answer = None

                info.last_solve_time_ms = solve_ms

                if answer:
                    result = await self._submit_answer(info, captcha_id, answer)
                    if result.get("status") == 1:
                        info.solved += 1
                        info.earnings += rate
                        info.balance += rate
                        info.history.append({"id": captcha_id, "type": ctype, "answer": answer,
                                           "reward": rate, "status": "solved", "time": time.time()})
                        log.info(f"✅ [{info.label}] Solved #{captcha_id} ({ctype}) in {solve_ms}ms +${rate:.5f}")
                    else:
                        info.failed += 1
                        info.history.append({"id": captcha_id, "type": ctype, "answer": answer,
                                           "reward": 0, "status": "submit_failed", "time": time.time()})
                        log.warning(f"⚠️ [{info.label}] Submit failed #{captcha_id}: {result.get('error')}")
                else:
                    # ── CRITICAL: Skip captcha via closerecaptchabot ──
                    # This tells 2captcha "I can't solve this" WITHOUT penalty.
                    # If we DON'T call this, the captcha times out and the account
                    # can get BLOCKED for ignoring captchas.
                    skip_result = await self._skip_captcha(info, captcha_id)
                    info.failed += 1
                    info.history.append({"id": captcha_id, "type": ctype, "answer": "",
                                       "reward": 0, "status": "skipped", "time": time.time()})
                    log.warning(f"⏭️ [{info.label}] Skipped #{captcha_id} ({ctype}) — solver returned no answer (skip_result: {skip_result.get('status', 'unknown')})")

                if len(info.history) > 100:
                    info.history = info.history[-50:]

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"❌ [{info.label}] Loop error: {e}")
                await asyncio.sleep(5)

        info.running = False
        log.info(f"🛑 [{info.label}] Account loop stopped")

    async def stop(self):
        """Stop the worker bot."""
        self.running = False
        self._stop_event.set()
        for t in self._tasks:
            t.cancel()

    async def refresh_balances(self):
        """Refresh all account balances by logging in again."""
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        for info in self.accounts.values():
            await self._login(info)

    async def withdraw(self, thash: str = "") -> dict:
        """Request withdrawal (stub — 2captcha doesn't have worker API withdrawal)."""
        return {"status": 0, "error": "Withdrawals must be done via 2captcha.com web interface"}
