#!/usr/bin/env python3
"""
2Captcha Worker Bot — Core Worker Engine
Polls 2captcha for captchas, routes to our solver fleet, submits answers.
"""

import asyncio
import aiohttp
import base64
import time
import json
import logging
import os
import re
from typing import Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("2captcha-worker")

# ─── Configuration ────────────────────────────────────────────

# 2captcha account key (customer/worker key)
CAPTCHA_KEY = os.environ.get("CAPTCHA_KEY", "ec63b74d6ee7848c14b01cc436c6eb21")

# 2captcha API endpoints
API_BASE = os.environ.get("CAPTCHA_API_BASE", "https://2captcha.com")
RUCAPTCHA_BASE = os.environ.get("RUCAPTCHA_BASE", "https://rucaptcha.com")
API_V2_BASE = "https://api.2captcha.com"

# Our solver fleet endpoints (Docker gateway)
SOLVER_UNIVERSAL = os.environ.get("SOLVER_UNIVERSAL", "http://172.17.0.1:8855")
SOLVER_TURNSTILE = os.environ.get("SOLVER_TURNSTILE", "http://172.17.0.1:8878")
SOLVER_RECAPTCHA = os.environ.get("SOLVER_RECAPTCHA", "http://172.17.0.1:8866")
SOLVER_XCAPTCHA = os.environ.get("SOLVER_XCAPTCHA", "http://172.17.0.1:8899")

# Worker settings
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
MAX_SOLVE_TIME = 110  # 110s to stay under 120s timer
MIN_PAYOUT = 0.5  # minimum payout in USD

# Worker rates per 1000 captchas (2captcha worker pay rates)
RATES = {
    "normal": 0.50,       # $0.50 per 1000 normal captchas
    "recaptcha_v2": 1.00, # $1.00 per 1000 reCAPTCHA v2
    "turnstile": 1.00,    # $1.00 per 1000 Turnstile
    "hcaptcha": 1.00,     # $1.00 per 1000 hCaptcha
    "image": 0.50,        # $0.50 per 1000 image captchas
    "text": 0.10,         # $0.10 per 1000 text captchas
    "coordinates": 0.70,  # $0.70 per 1000 coordinate captchas
}


class WorkerBot:
    """2captcha worker bot that automates captcha solving."""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        self.total_solved = 0
        self.total_failed = 0
        self.total_earnings = 0.0
        self.current_balance = 0.0
        self.current_task = None  # current captcha being solved
        self.captcha_queue = asyncio.Queue()
        self._stop_event = asyncio.Event()

    async def start(self):
        """Start the worker bot."""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120)
        )
        self.running = True
        self._stop_event.clear()

        # Check balance
        balance = await self.get_balance()
        if balance is not None:
            self.current_balance = balance
            log.info(f"💰 Account balance: ${balance:.4f}")
        else:
            log.warning("⚠️ Could not retrieve balance — key may be worker-only or invalid")

        log.info("🚀 Worker bot started — polling for captchas...")
        
        # Start captcha polling and solving loops concurrently
        poller = asyncio.create_task(self._poll_loop())
        solver = asyncio.create_task(self._solve_loop())
        
        await self._stop_event.wait()
        
        # Clean up
        poller.cancel()
        solver.cancel()
        await self.session.close()
        log.info("🛑 Worker bot stopped")

    async def stop(self):
        """Stop the worker bot."""
        self.running = False
        self._stop_event.set()

    async def get_balance(self) -> Optional[float]:
        """Get current account balance."""
        # Try API v2 first
        try:
            async with self.session.post(
                f"{API_V2_BASE}/getBalance",
                json={"clientKey": CAPTCHA_KEY},
                timeout=10
            ) as resp:
                data = await resp.json()
                if data.get("errorId") == 0:
                    return float(data.get("balance", 0))
        except Exception:
            pass

        # Try v1 API
        try:
            async with self.session.get(
                f"{API_BASE}/res.php",
                params={"key": CAPTCHA_KEY, "action": "getbalance", "json": 1},
                timeout=10
            ) as resp:
                data = await resp.json()
                if data.get("status") == 1:
                    return float(data.get("request", 0))
        except Exception:
            pass

        # Try rucaptcha mirror
        try:
            async with self.session.get(
                f"{RUCAPTCHA_BASE}/res.php",
                params={"key": CAPTCHA_KEY, "action": "getbalance", "json": 1},
                timeout=10
            ) as resp:
                data = await resp.json()
                if data.get("status") == 1:
                    return float(data.get("request", 0))
        except Exception:
            pass

        return None

    async def _poll_loop(self):
        """Poll 2captcha for available captchas to solve as a worker."""
        while self.running:
            try:
                # In worker mode, we get captchas from the queue
                # The 2captcha worker system works like this:
                # 1. Worker software connects to 2captcha
                # 2. 2captcha sends captchas to the worker
                # 3. Worker solves and returns the answer
                
                # Since the API key may be worker-only, we need to use the
                # cabinet API. However, for automated solving, we can also
                # work as a customer proxy: submit captchas to our own solver
                # and return results.
                
                # For now, poll for balance changes and use the worker endpoint
                balance = await self.get_balance()
                if balance is not None and balance != self.current_balance:
                    diff = balance - self.current_balance
                    if diff > 0:
                        log.info(f"💰 Balance increased by ${diff:.4f} (new: ${balance:.4f})")
                    self.current_balance = balance

                await asyncio.sleep(5)  # Poll every 5 seconds

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Poll error: {e}")
                await asyncio.sleep(2)

    async def _solve_loop(self):
        """Solve captchas from the queue."""
        while self.running:
            try:
                # Get a captcha from the queue (or wait)
                task = await asyncio.wait_for(
                    self.captcha_queue.get(),
                    timeout=1.0
                )
                await self._solve_captcha(task)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Solve loop error: {e}")
                await asyncio.sleep(1)

    async def _solve_captcha(self, task: dict):
        """Solve a single captcha using our solver fleet."""
        captcha_id = task["id"]
        captcha_type = task.get("type", "image")
        start_time = time.time()

        log.info(f"📝 Solving captcha {captcha_id} (type: {captcha_type})")

        try:
            # Route to appropriate solver based on type
            if captcha_type in ("image", "base64", "normal", "text"):
                answer = await self._solve_image_captcha(task)
            elif captcha_type in ("recaptcha_v2", "userrecaptcha"):
                answer = await self._solve_recaptcha(task)
            elif captcha_type in ("turnstile",):
                answer = await self._solve_turnstile(task)
            elif captcha_type in ("hcaptcha",):
                answer = await self._solve_hcaptcha(task)
            elif captcha_type in ("coord", "coordinates", "grid", "canvas", "click"):
                answer = await self._solve_coord_captcha(task)
            else:
                answer = await self._solve_image_captcha(task)

            solve_ms = int((time.time() - start_time) * 1000)
            reward = RATES.get(captcha_type, 0.50) / 1000.0

            if answer:
                # Submit answer to 2captcha
                submitted = await self._submit_answer(captcha_id, answer)
                if submitted:
                    self.total_solved += 1
                    self.total_earnings += reward
                    log.info(f"✅ Solved {captcha_id} in {solve_ms}ms — +${reward:.6f}")
                    return answer
            else:
                self.total_failed += 1
                log.warning(f"❌ Failed to solve {captcha_id}")

        except Exception as e:
            self.total_failed += 1
            log.error(f"❌ Error solving {captcha_id}: {e}")

        return None

    async def _solve_image_captcha(self, task: dict) -> Optional[str]:
        """Solve image/base64 captcha using universal OCR solver."""
        image_data = task.get("image_base64", "")
        image_url = task.get("image_url", "")

        if not image_data and not image_url:
            return None

        try:
            # Submit to our universal solver
            if image_data:
                data = {
                    "key": "worker",
                    "method": "base64",
                    "body": image_data,
                }
            else:
                data = {
                    "key": "worker",
                    "method": "image",
                    "captcha_img": image_url,
                }

            async with self.session.post(
                f"{SOLVER_UNIVERSAL}/in.php",
                data=data,
                timeout=30
            ) as resp:
                result = await resp.text()
                if not result.startswith("OK|"):
                    log.error(f"Solver error: {result}")
                    return None
                task_id = result[3:]

            # Poll for result
            for _ in range(20):
                await asyncio.sleep(1)
                async with self.session.get(
                    f"{SOLVER_UNIVERSAL}/res.php",
                    params={"key": "worker", "id": task_id},
                    timeout=10
                ) as resp:
                    result = await resp.text()
                    if result == "CAPCHA_NOT_READY":
                        continue
                    if result.startswith("OK|"):
                        return result[3:]
                    return None

            return None
        except Exception as e:
            log.error(f"Image captcha error: {e}")
            return None

    async def _solve_recaptcha(self, task: dict) -> Optional[str]:
        """Solve reCAPTCHA v2 using our dedicated solver."""
        googlekey = task.get("googlekey", task.get("sitekey", ""))
        pageurl = task.get("pageurl", "")

        if not googlekey or not pageurl:
            return None

        try:
            # Submit to our reCAPTCHA solver
            async with self.session.post(
                f"{SOLVER_RECAPTCHA}/in.php",
                data={
                    "key": "worker",
                    "method": "userrecaptcha",
                    "googlekey": googlekey,
                    "pageurl": pageurl,
                },
                timeout=30
            ) as resp:
                result = await resp.text()
                if not result.startswith("OK|"):
                    return None
                task_id = result[3:]

            # Poll for token
            for _ in range(40):  # reCAPTCHA takes longer
                await asyncio.sleep(2)
                async with self.session.get(
                    f"{SOLVER_RECAPTCHA}/res.php",
                    params={"key": "worker", "id": task_id},
                    timeout=10
                ) as resp:
                    result = await resp.text()
                    if result == "CAPCHA_NOT_READY":
                        continue
                    if result.startswith("OK|"):
                        return result[3:]
                    return None

            return None
        except Exception as e:
            log.error(f"reCAPTCHA error: {e}")
            return None

    async def _solve_turnstile(self, task: dict) -> Optional[str]:
        """Solve Cloudflare Turnstile using our dedicated solver."""
        sitekey = task.get("sitekey", "")
        pageurl = task.get("pageurl", "")

        if not sitekey or not pageurl:
            return None

        try:
            async with self.session.post(
                f"{SOLVER_TURNSTILE}/in.php",
                data={
                    "key": "worker",
                    "method": "turnstile",
                    "sitekey": sitekey,
                    "pageurl": pageurl,
                },
                timeout=30
            ) as resp:
                result = await resp.text()
                if not result.startswith("OK|"):
                    return None
                task_id = result[3:]

            for _ in range(40):
                await asyncio.sleep(2)
                async with self.session.get(
                    f"{SOLVER_TURNSTILE}/res.php",
                    params={"key": "worker", "id": task_id},
                    timeout=10
                ) as resp:
                    result = await resp.text()
                    if result == "CAPCHA_NOT_READY":
                        continue
                    if result.startswith("OK|"):
                        return result[3:]
                    return None

            return None
        except Exception as e:
            log.error(f"Turnstile error: {e}")
            return None

    async def _solve_hcaptcha(self, task: dict) -> Optional[str]:
        """Solve hCaptcha using our universal solver (hcaptcha-challenger)."""
        sitekey = task.get("sitekey", "")
        pageurl = task.get("pageurl", "")

        if not sitekey or not pageurl:
            return None

        try:
            async with self.session.post(
                f"{SOLVER_UNIVERSAL}/in.php",
                data={
                    "key": "worker",
                    "method": "hcaptcha",
                    "sitekey": sitekey,
                    "pageurl": pageurl,
                },
                timeout=30
            ) as resp:
                result = await resp.text()
                if not result.startswith("OK|"):
                    return None
                task_id = result[3:]

            for _ in range(40):
                await asyncio.sleep(2)
                async with self.session.get(
                    f"{SOLVER_UNIVERSAL}/res.php",
                    params={"key": "worker", "id": task_id},
                    timeout=10
                ) as resp:
                    result = await resp.text()
                    if result == "CAPCHA_NOT_READY":
                        continue
                    if result.startswith("OK|"):
                        return result[3:]
                    return None

            return None
        except Exception as e:
            log.error(f"hCaptcha error: {e}")
            return None

    async def _solve_coord_captcha(self, task: dict) -> Optional[str]:
        """Solve coordinate/click captcha using OCR detection."""
        return await self._solve_image_captcha(task)

    async def _submit_answer(self, captcha_id: str, answer: str) -> bool:
        """Submit the answer back to 2captcha."""
        # In worker mode, we submit via the cabinet API
        # For now, this is a placeholder for the worker submission endpoint
        # The actual submission depends on how the captcha was received
        
        # If we're working in proxy mode (customer submits, we solve),
        # the answer is returned through res.php
        
        # If we're in worker mode, we submit to the cabinet
        try:
            # Try the rucaptcha worker endpoint
            async with self.session.post(
                f"{RUCAPTCHA_BASE}/res.php",
                data={
                    "key": CAPTCHA_KEY,
                    "action": "answer",
                    "id": captcha_id,
                    "answer": answer,
                },
                timeout=10
            ) as resp:
                result = await resp.text()
                return "OK" in result
        except Exception:
            return False

    async def submit_captcha(self, captcha_type: str, **kwargs) -> Optional[dict]:
        """
        Submit a captcha for solving (customer proxy mode).
        This allows the bot to also work as a solving service.
        """
        task = {
            "id": f"task_{int(time.time() * 1000)}",
            "type": captcha_type,
            **kwargs
        }
        await self.captcha_queue.put(task)
        return task

    def get_live_stats(self) -> dict:
        """Get live bot stats."""
        return {
            "running": self.running,
            "total_solved": self.total_solved,
            "total_failed": self.total_failed,
            "total_earnings": self.total_earnings,
            "current_balance": self.current_balance,
            "queue_size": self.captcha_queue.qsize(),
        }


# ─── Standalone runner ───────────────────────────────────────

async def main():
    bot = WorkerBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
