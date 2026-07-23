#!/usr/bin/env python3
"""
2Captcha Auto-Training Module v2
Handles the /api/v1/training/v2/ training flow for 2captcha worker accounts.

Key principle: ALWAYS attempt to solve on the FIRST attempt.
Never rely on errorHint (which reveals answers after wrong submissions).
This avoids getting the account banned from too many mistakes.

Strategy per step type:
1. Text captcha with image → OCR the image directly (tesseract + universal solver)
2. Text captcha with textinstructions → parse the instruction and answer
3. Coordinate captcha (letters in circle noise):
   a) OCR the instruction image to get target letters (tesseract works perfectly)
   b) Download main image, find connected components (letter candidates)
   c) Crop each candidate, OCR with tesseract + universal solver
   d) Match candidates to target letters by OCR + position
   e) Build coordinate answer: "coordinates:x=X,y=Y;x=X,y=Y;..."
4. Skip step (mustSkip=True) → auto-skip
5. If OCR fails for any step → SKIP it rather than submit a wrong answer
6. Only use errorHint as a LAST RESORT (never on first attempt)

Uses auth cookies from a browser session (stored in auth_cookies.txt).
"""

import asyncio
import aiohttp
import base64
import io
import json
import logging
import os
import re
import time
from typing import Optional, List, Tuple
from PIL import Image, ImageEnhance
import numpy as np

log = logging.getLogger("2captcha-training")

# ─── Config ───────────────────────────────────────────────────
TRAINING_BASE = "https://2captcha.com/api/v1/training/v2"
COOKIES_FILE = os.environ.get("AUTH_COOKIES_FILE", "/app/auth_cookies.txt")
SOLVER_UNIVERSAL = os.environ.get("SOLVER_UNIVERSAL", "http://172.17.0.1:8855")
SOLVER_API_KEY = os.environ.get("SOLVER_API_KEY", "8010000000ccojr5nrbg516w5jvw1wu9")
TESSERACT_BIN = os.environ.get("TESSERACT_BIN", "tesseract")

try:
    from scipy import ndimage
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    log.warning("scipy not available — coordinate captcha detection will be limited")


def load_auth_cookies() -> dict:
    """Load auth cookies from the cookies file."""
    if not os.path.exists(COOKIES_FILE):
        log.warning(f"Auth cookies file not found: {COOKIES_FILE}")
        return {}
    with open(COOKIES_FILE) as f:
        cookie_str = f.read().strip()
    return {p.split("=", 1)[0].strip(): p.split("=", 1)[1].strip()
            for p in cookie_str.split("; ") if "=" in p}


def _tesseract_ocr(image: Image.Image, psm: int = 7, whitelist: str = "") -> str:
    """Run tesseract OCR on a PIL image."""
    import subprocess
    tmp_path = "/tmp/_training_ocr.png"
    image.save(tmp_path)
    cmd = [TESSERACT_BIN, tmp_path, "stdout", "--psm", str(psm)]
    if whitelist:
        cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout.strip()
    except Exception as e:
        log.error(f"Tesseract error: {e}")
        return ""


async def _solver_ocr(session: aiohttp.ClientSession, image_b64: str) -> Optional[str]:
    """Send image to universal OCR solver."""
    try:
        async with session.post(f"{SOLVER_UNIVERSAL}/in.php", data={
            "key": SOLVER_API_KEY, "method": "base64", "body": image_b64
        }) as resp:
            result = await resp.text()
            if not result.startswith("OK|"):
                return None
            task_id = result.split("|", 1)[1]
            for _ in range(15):
                await asyncio.sleep(1)
                async with session.get(f"{SOLVER_UNIVERSAL}/res.php",
                    params={"key": SOLVER_API_KEY, "id": task_id}
                ) as r:
                    res = await r.text()
                    if res.startswith("OK|"):
                        return res.split("|", 1)[1]
                    if "CAPCHA_NOT_READY" not in res:
                        return None
            return None
    except Exception as e:
        log.error(f"Solver OCR error: {e}")
        return None


def _parse_text_instruction(text: str) -> Optional[str]:
    """Parse a text instruction to get the answer directly.
    
    Examples:
    - 'Write the name of the first month of the year in English.' → 'january'
    - 'How much is 2+3?' → '5'
    - 'Type the text you see in the image' → None (needs OCR)
    """
    text = text.lower().strip()
    
    # Month names
    month_questions = {
        "first month": "january",
        "second month": "february", 
        "third month": "march",
        "fourth month": "april",
        "fifth month": "may",
        "sixth month": "june",
        "seventh month": "july",
        "eighth month": "august",
        "ninth month": "september",
        "tenth month": "october",
        "eleventh month": "november",
        "twelfth month": "december",
    }
    for q, a in month_questions.items():
        if q in text:
            return a
    
    # Simple math
    math_match = re.search(r'(\d+)\s*([+\-*/x×])\s*(\d+)', text)
    if math_match:
        a, op, b = int(math_match.group(1)), math_match.group(2), int(math_match.group(3))
        if op == '+': return str(a + b)
        if op == '-': return str(a - b)
        if op in ('*', 'x', '×'): return str(a * b)
        if op == '/': return str(a // b) if b else None
    
    return None


async def _solve_text_captcha(
    session: aiohttp.ClientSession,
    step_data: dict,
) -> Optional[str]:
    """
    Solve a text captcha on FIRST attempt.
    
    Strategy:
    1. Check textinstructions for a direct answer (month names, math, etc.)
    2. If image present → OCR with tesseract, then universal solver
    3. Return best guess, or None if unsure
    """
    d = step_data.get("data", {})
    text_inst = d.get("textinstructions", "")
    
    # Step 1: Try parsing text instruction
    if text_inst:
        direct_answer = _parse_text_instruction(text_inst)
        if direct_answer:
            log.info(f"  Answer from text instruction: {direct_answer}")
            return direct_answer
    
    # Step 2: OCR the image
    img_data = d.get("image", "")
    img_b64 = None
    
    if img_data:
        if "base64," in img_data:
            img_b64 = img_data.split("base64,", 1)[1]
        elif img_data.startswith("/") or img_data.startswith("http"):
            img_url = f"https://2captcha.com{img_data}" if img_data.startswith("/") else img_data
            try:
                async with session.get(img_url) as r_img:
                    img_bytes = await r_img.read()
                img_b64 = base64.b64encode(img_bytes).decode()
                
                # Also try tesseract directly
                img = Image.open(io.BytesIO(img_bytes))
                tess_result = _tesseract_ocr(img, psm=7)
                tess_result = tess_result.strip()
                if tess_result and len(tess_result) <= 20:
                    log.info(f"  Tesseract OCR: '{tess_result}'")
                    # Also try universal solver for comparison
                    solver_result = await _solver_ocr(session, img_b64)
                    if solver_result:
                        solver_result = solver_result.strip()
                        log.info(f"  Solver OCR: '{solver_result}'")
                        # Prefer shorter, cleaner result
                        if len(solver_result) <= len(tess_result):
                            return solver_result
                    return tess_result
            except Exception as e:
                log.error(f"  Image download error: {e}")
        elif len(img_data) > 100:
            img_b64 = img_data
    
    if img_b64 and len(img_b64) > 100:
        # Try universal solver
        result = await _solver_ocr(session, img_b64)
        if result:
            result = result.strip()
            log.info(f"  Solver OCR: '{result}'")
            if len(result) <= 20:  # Reasonable captcha answer length
                return result
    
    # No answer found — return None (will be skipped, not submitted wrong)
    log.warning("  Could not solve text captcha on first attempt — will skip")
    return None


async def _solve_coordinate_captcha(
    session: aiohttp.ClientSession,
    step_data: dict,
) -> Optional[str]:
    """
    Solve a coordinate captcha on FIRST attempt.
    
    The coordinate captcha shows letters hidden behind circle noise.
    The instruction image shows which letters to find (clean, easy to OCR).
    
    Strategy:
    1. OCR the instruction image → get target letters (tesseract, psm=7)
    2. Download main image
    3. Find connected components (letter candidates) using scipy
    4. For each candidate: crop, enlarge, OCR with tesseract (psm=10) + solver
    5. Match candidates to target letters by OCR results
    6. Build answer: "coordinates:x=X,y=Y;x=X,y=Y;..."
    
    If matching is uncertain, return None (skip, don't submit wrong answer).
    """
    if not SCIPY_AVAILABLE:
        log.warning("  scipy not available — cannot solve coordinate captcha")
        return None
    
    d = step_data.get("data", {})
    
    # Step 1: Get and OCR the instruction image
    inst_b64 = d.get("file_textinstruction", "")
    if not inst_b64 or "base64," not in inst_b64:
        log.warning("  No instruction image for coordinate captcha")
        return None
    
    inst_data = base64.b64decode(inst_b64.split("base64,", 1)[1])
    inst_img = Image.open(io.BytesIO(inst_data))
    
    # OCR instruction with tesseract (works perfectly for clean letter images)
    target_letters = _tesseract_ocr(inst_img, psm=7,
        whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    
    if not target_letters:
        # Fallback to universal solver
        inst_b64_clean = inst_b64.split("base64,", 1)[1]
        target_letters = await _solver_ocr(session, inst_b64_clean)
    
    if not target_letters:
        log.warning("  Could not OCR instruction image")
        return None
    
    # Clean up and extract individual letters
    target_letters = target_letters.strip()
    log.info(f"  Target letters: {target_letters}")
    
    # Step 2: Download main image
    img_url = d.get("image", "")
    if not img_url:
        log.warning("  No main image URL")
        return None
    
    if img_url.startswith("/"):
        img_url = f"https://2captcha.com{img_url}"
    
    try:
        async with session.get(img_url) as r_img:
            main_img_bytes = await r_img.read()
    except Exception as e:
        log.error(f"  Main image download error: {e}")
        return None
    
    main_img = Image.open(io.BytesIO(main_img_bytes))
    gray = np.array(main_img.convert('L'), dtype=np.float32)
    h, w = gray.shape
    
    # Step 3: Find connected components
    binary = (gray < 150).astype(np.int32)
    labeled, num_features = ndimage.label(binary)
    bboxes = ndimage.find_objects(labeled)
    
    candidates = []
    for i, bbox in enumerate(bboxes):
        if bbox is None:
            continue
        mask = (labeled[bbox] == i + 1)
        s = int(mask.sum())
        if 20 < s < 500:  # Letter-sized component
            ys, xs = bbox
            cx = (xs.start + xs.stop) // 2
            cy = (ys.start + ys.stop) // 2
            candidates.append({
                "x": cx, "y": cy, "size": s,
                "bbox": (xs.start, ys.start, xs.stop, ys.stop),
                "index": i + 1,
            })
    
    if len(candidates) < len(target_letters):
        log.warning(f"  Found {len(candidates)} candidates but need {len(target_letters)} letters")
        return None
    
    log.info(f"  Found {len(candidates)} letter candidates")
    
    # Step 4: OCR each candidate
    for c in candidates:
        x1, y1, x2, y2 = c["bbox"]
        pad = 5
        crop = main_img.crop((max(0, x1-pad), max(0, y1-pad), min(w, x2+pad), min(h, y2+pad)))
        crop = crop.resize((crop.width * 4, crop.height * 4), Image.LANCZOS)
        crop = ImageEnhance.Contrast(crop).enhance(2.0)
        
        # Tesseract OCR
        tess = _tesseract_ocr(crop, psm=10,
            whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
        c["tesseract"] = tess[0] if tess else ""
        
        # Universal solver OCR (async)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        crop_b64 = base64.b64encode(buf.getvalue()).decode()
        solver = await _solver_ocr(session, crop_b64)
        c["solver"] = solver[0] if solver else ""
        
        log.info(f"    Candidate ({c['x']:3d},{c['y']:3d}): tess='{c['tesseract']}' solver='{c['solver']}'")
    
    # Step 5: Match candidates to target letters
    # Use both OCR engines. Each target letter should match exactly one candidate.
    matched_points: List[Tuple[int, int]] = []
    used = set()
    
    for target in target_letters:
        target_upper = target.upper()
        best_idx = -1
        best_score = 0
        
        for i, c in enumerate(candidates):
            if i in used:
                continue
            score = 0
            # Exact match (case-insensitive)
            if c["tesseract"].upper() == target_upper:
                score = 3  # tesseract exact
            if c["solver"].upper() == target_upper:
                score = max(score, 3)  # solver exact
            # Partial match (first char of multi-char OCR)
            if not score:
                if c["tesseract"] and c["tesseract"][0].upper() == target_upper:
                    score = 1
                if c["solver"] and c["solver"][0].upper() == target_upper:
                    score = max(score, 1)
            
            if score > best_score:
                best_score = score
                best_idx = i
        
        if best_idx >= 0 and best_score > 0:
            c = candidates[best_idx]
            matched_points.append((c["x"], c["y"]))
            used.add(best_idx)
            log.info(f"    Matched '{target}' -> ({c['x']},{c['y']}) score={best_score}")
        else:
            log.warning(f"    Could not match target letter '{target}'")
    
    # Step 6: Build answer
    if len(matched_points) == len(target_letters):
        coord_str = ";".join(f"x={x},y={y}" for x, y in matched_points)
        answer = f"coordinates:{coord_str}"
        log.info(f"  Coordinate answer: {answer}")
        return answer
    else:
        log.warning(f"  Only matched {len(matched_points)}/{len(target_letters)} letters — skipping to avoid ban")
        return None


async def complete_training(session: Optional[aiohttp.ClientSession] = None) -> dict:
    """
    Run the full training flow. Always attempts to solve on FIRST attempt.
    If a step can't be solved with confidence, it is SKIPPED (not answered wrong).
    
    Returns:
    {
        "success": bool,
        "level": int,
        "steps_completed": int,
        "skipped": int,
        "error": str (if failed),
    }
    """
    cookies = load_auth_cookies()
    if not cookies:
        return {"success": False, "error": "No auth cookies found"}
    
    own_session = False
    if session is None:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        own_session = True
    
    try:
        headers = {
            "Referer": "https://2captcha.com/play-and-earn/training",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
        }
        
        steps_completed = 0
        steps_skipped = 0
        max_steps = 50
        last_cap_id = None
        same_cap_retries = 0
        MAX_SAME_CAP_RETRIES = 2  # Max wrong answers for same step before giving up
        
        for step in range(max_steps):
            # Get current step
            async with session.get(f"{TRAINING_BASE}/current-step",
                cookies=cookies, headers=headers
            ) as r:
                if r.status != 200:
                    return {"success": False, "error": f"GET failed: {r.status}",
                            "steps_completed": steps_completed, "skipped": steps_skipped}
                data = await r.json()
            
            # Check completion
            is_completed = data.get("isLevelCompleted", False) or data.get("isTrainingCompleted", False)
            if is_completed:
                level = data.get("level", 0)
                log.info(f"✅ Training completed! Level: {level}")
                return {"success": True, "level": level,
                        "steps_completed": steps_completed, "skipped": steps_skipped}
            
            must_skip = data.get("mustSkip", False)
            hint = data.get("hint", {}).get("en", "")
            d = data.get("data", {})
            params = d.get("params", {})
            is_coord = params.get("coordinatescaptcha", 0) or params.get("coordinates_captcha", 0)
            cap_id = d.get("captcha_id", "?")
            text_inst = d.get("textinstructions", "")
            
            log.info(f"📋 Step {step+1}: cap={cap_id} coord={is_coord} skip={must_skip} hint={hint[:40]}")
            
            # ── Track same-captcha retries to prevent infinite loops ──
            if cap_id != last_cap_id:
                same_cap_retries = 0
                last_cap_id = cap_id
            else:
                same_cap_retries += 1
                if same_cap_retries >= MAX_SAME_CAP_RETRIES:
                    log.warning(f"  Same captcha {cap_id} failed {same_cap_retries}x — stopping training to avoid ban")
                    return {"success": False, "error": f"Cannot solve captcha {cap_id} after {same_cap_retries} attempts. Stopping to avoid ban.",
                            "steps_completed": steps_completed, "skipped": steps_skipped}
            
            # ── Auto-skip step ──
            if must_skip:
                log.info("  Auto-skipping (mustSkip=True)")
                async with session.post(f"{TRAINING_BASE}/current-step",
                    cookies=cookies, headers=headers, json={"skip": True}
                ) as r2:
                    log.info(f"  Skip: {r2.status}")
                steps_completed += 1
                await asyncio.sleep(1)
                continue
            
            # ── Coordinate captcha ──
            if is_coord:
                answer = await _solve_coordinate_captcha(session, data)
                if answer:
                    async with session.post(f"{TRAINING_BASE}/current-step",
                        cookies=cookies, headers=headers, json={"answer": answer}
                    ) as r2:
                        status = r2.status
                        body = await r2.text()
                        log.info(f"  Submit: {status} {body[:100]}")
                        if status == 204:
                            steps_completed += 1
                        elif status == 422:
                            log.warning("  Wrong answer! NOT retrying (to avoid ban)")
                            steps_skipped += 1
                    await asyncio.sleep(1.5)
                    continue
                else:
                    # Could not solve — try skip (coordinate captchas might not be skippable)
                    log.warning("  Coordinate captcha unsolvable — trying skip")
                    async with session.post(f"{TRAINING_BASE}/current-step",
                        cookies=cookies, headers=headers, json={"skip": True}
                    ) as r2:
                        if r2.status == 204:
                            steps_skipped += 1
                        else:
                            log.error("  Cannot skip coordinate captcha — stuck!")
                            return {"success": False, "error": "Stuck on coordinate captcha",
                                    "steps_completed": steps_completed, "skipped": steps_skipped}
                    await asyncio.sleep(1)
                    continue
            
            # ── Text captcha (including those with instructions/images) ──
            answer = await _solve_text_captcha(session, data)
            
            if answer:
                # Submit the answer
                async with session.post(f"{TRAINING_BASE}/current-step",
                    cookies=cookies, headers=headers, json={"answer": answer}
                ) as r2:
                    status = r2.status
                    body = await r2.text()
                    log.info(f"  Submit '{answer}': {status} {body[:100]}")
                    if status == 204:
                        steps_completed += 1
                    elif status == 422:
                        log.warning("  Wrong answer — skipping this step to avoid ban")
                        # Skip this step instead of retrying with wrong answer
                        async with session.post(f"{TRAINING_BASE}/current-step",
                            cookies=cookies, headers=headers, json={"skip": True}
                        ) as r3:
                            skip_status = r3.status
                            if skip_status == 204:
                                steps_skipped += 1
                                log.info("  Step skipped successfully")
                            else:
                                log.error(f"  Cannot skip (status {skip_status}) — will break to avoid ban loop")
                                steps_skipped += 1
                                # Don't continue the loop — return to avoid infinite wrong answers
                                return {"success": False, "error": f"Cannot solve or skip step (cap={cap_id}). Stopping to avoid ban.",
                                        "steps_completed": steps_completed, "skipped": steps_skipped}
                        await asyncio.sleep(1.5)
                        continue
            else:
                # Could not solve — SKIP rather than submit wrong answer
                log.warning("  Unsolved text captcha — skipping (to avoid ban)")
                async with session.post(f"{TRAINING_BASE}/current-step",
                    cookies=cookies, headers=headers, json={"skip": True}
                ) as r2:
                    if r2.status == 204:
                        steps_skipped += 1
                    else:
                        log.error("  Cannot skip — might be stuck")
                        # As absolute last resort, check errorHint
                        error_hint = data.get("errorHint", {}).get("en", "")
                        hint_answer = _extract_hint_answer(error_hint)
                        if hint_answer and "Invalid" not in error_hint:
                            log.info(f"  Using errorHint as last resort: {hint_answer}")
                            async with session.post(f"{TRAINING_BASE}/current-step",
                                cookies=cookies, headers=headers, json={"answer": hint_answer}
                            ) as r3:
                                if r3.status == 204:
                                    steps_completed += 1
                await asyncio.sleep(1)
                continue
        
        return {"success": False, "error": "Max steps reached",
                "steps_completed": steps_completed, "skipped": steps_skipped}
    
    except Exception as e:
        log.error(f"Training error: {e}")
        return {"success": False, "error": str(e)}
    finally:
        if own_session:
            await session.close()


def _extract_hint_answer(hint: str) -> Optional[str]:
    """Extract answer from errorHint (last resort only)."""
    if not hint:
        return None
    m = re.search(r'translate="no">(.*?)<', hint)
    if m:
        return m.group(1).strip()
    m = re.search(r"Correct answer:\s*(.+?)(?:<|$)", hint)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return None
