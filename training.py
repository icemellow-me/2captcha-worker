#!/usr/bin/env python3
"""
2Captcha Auto-Training Module
Handles the /api/v1/training/v2/ training flow for 2captcha worker accounts.

Training steps supported:
1. Text captchas with errorHint (answer revealed in hint) → auto-solved
2. Text captchas with image → OCR via universal solver
3. Coordinate captchas (letters in circle noise) → image processing + OCR
4. Skip steps (mustSkip=True) → auto-skipped

Uses auth cookies from a browser session (stored in auth_cookies.txt).
Cookies are extracted from a HAR file captured during a browser training session.
"""

import asyncio
import aiohttp
import base64
import hashlib
import io
import json
import logging
import os
import re
import time
from typing import Optional
from PIL import Image, ImageEnhance, ImageFilter
import numpy as np

log = logging.getLogger("2captcha-training")

# ─── Config ───────────────────────────────────────────────────
TRAINING_BASE = "https://2captcha.com/api/v1/training/v2"
COOKIES_FILE = os.environ.get("AUTH_COOKIES_FILE", "/app/auth_cookies.txt")
SOLVER_UNIVERSAL = os.environ.get("SOLVER_UNIVERSAL", "http://172.17.0.1:8855")
SOLVER_API_KEY = os.environ.get("SOLVER_API_KEY", "8010000000ccojr5nrbg516w5jvw1wu9")

# Use tesseract from the system if available
TESSERACT_BIN = os.environ.get("TESSERACT_BIN", "tesseract")


def load_auth_cookies() -> dict:
    """Load auth cookies from the cookies file."""
    if not os.path.exists(COOKIES_FILE):
        log.warning(f"Auth cookies file not found: {COOKIES_FILE}")
        return {}
    
    with open(COOKIES_FILE) as f:
        cookie_str = f.read().strip()
    
    cookies = {}
    for part in cookie_str.split("; "):
        if "=" in part:
            name, val = part.split("=", 1)
            cookies[name.strip()] = val.strip()
    
    return cookies


def _parse_cookies(cookie_str: str) -> dict:
    """Parse cookie string into dict."""
    cookies = {}
    for part in cookie_str.split("; "):
        if "=" in part:
            name, val = part.split("=", 1)
            cookies[name.strip()] = val.strip()
    return cookies


async def _ocr_with_solver(session: aiohttp.ClientSession, image_b64: str, whitelist: str = "") -> Optional[str]:
    """Send image to the universal OCR solver and get text back."""
    try:
        data = {"key": SOLVER_API_KEY, "method": "base64", "body": image_b64}
        async with session.post(f"{SOLVER_UNIVERSAL}/in.php", data=data) as resp:
            result = await resp.text()
            if not result.startswith("OK|"):
                log.warning(f"OCR solver rejected: {result[:100]}")
                return None
            task_id = result.split("|", 1)[1]
            
            for _ in range(15):
                await asyncio.sleep(1)
                async with session.get(
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


def _extract_answer_from_hint(hint_text: str) -> Optional[str]:
    """Extract the correct answer from errorHint text.
    
    Examples:
    - 'Correct answer: <span translate="no">january</span>' → 'january'
    - 'Correct answer: january' → 'january'
    - 'Correct answer: <span translate="no">w#=9</span>' → 'w#=9'
    """
    if not hint_text:
        return None
    
    # Try HTML span pattern first
    m = re.search(r'translate="no">(.*?)<', hint_text)
    if m:
        return m.group(1).strip()
    
    # Try plain text pattern
    m = re.search(r"Correct answer:\s*(.+?)(?:<|$)", hint_text)
    if m:
        answer = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return answer if answer else None
    
    # Check if it's a non-revealing hint
    if "Invalid answer" in hint_text or "be careful" in hint_text:
        return None
    
    return None


async def _ocr_instruction_tesseract(image_b64: str) -> str:
    """OCR the instruction image using tesseract (better for clean text)."""
    try:
        img_data = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_data))
        
        # Save to temp file
        tmp_path = "/tmp/training_inst.png"
        img.save(tmp_path)
        
        import subprocess
        result = subprocess.run(
            [TESSERACT_BIN, tmp_path, "stdout", "--psm", "7",
             "-c", "tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip()
    except Exception as e:
        log.error(f"Tesseract instruction OCR error: {e}")
        return ""


async def _find_letter_coordinates(
    session: aiohttp.ClientSession,
    main_img_data: bytes,
    instruction_img_b64: str,
    target_letters: str,
) -> Optional[str]:
    """
    Find letter coordinates in a coordinate captcha image.
    
    The main image has letters hidden behind circle noise.
    The instruction image shows which letters to find (clean, easy to OCR).
    
    Returns coordinates in 2captcha format: "coordinates:x=X,y=Y;x=X,y=Y;..."
    """
    try:
        from scipy import ndimage
    except ImportError:
        log.error("scipy not available for coordinate detection")
        return None
    
    try:
        main_img = Image.open(io.BytesIO(main_img_data))
        gray = np.array(main_img.convert('L'), dtype=np.float32)
        h, w = gray.shape
        
        # Binary threshold to isolate dark pixels (letters + circle edges)
        binary = (gray < 150).astype(np.int32)
        labeled, num_features = ndimage.label(binary)
        bboxes = ndimage.find_objects(labeled)
        
        # Find letter candidates (right size range)
        candidates = []
        for i, bbox in enumerate(bboxes):
            if bbox is None:
                continue
            mask = (labeled[bbox] == i + 1)
            s = int(mask.sum())
            if 20 < s < 500:
                ys, xs = bbox
                cx = (xs.start + xs.stop) // 2
                cy = (ys.start + ys.stop) // 2
                candidates.append((cx, cy, s, i + 1, bbox))
        
        if not candidates:
            log.warning("No letter candidates found in coordinate captcha")
            return None
        
        log.info(f"Found {len(candidates)} letter candidates for target '{target_letters}'")
        
        # For each candidate, crop, enlarge, and OCR
        results = []
        for cx, cy, s, lbl, bbox in candidates:
            ys, xs = bbox
            pad = 5
            x1 = max(0, xs.start - pad)
            y1 = max(0, ys.start - pad)
            x2 = min(w, xs.stop + pad)
            y2 = min(h, ys.stop + pad)
            
            crop = main_img.crop((x1, y1, x2, y2))
            crop = crop.resize((crop.width * 4, crop.height * 4), Image.LANCZOS)
            
            # Enhance contrast
            crop = ImageEnhance.Contrast(crop).enhance(2.0)
            
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            crop_b64 = base64.b64encode(buf.getvalue()).decode()
            
            # Try tesseract first
            tmp_path = f"/tmp/coord_crop_{cx}_{cy}.png"
            crop.save(tmp_path)
            
            import subprocess
            try:
                r = subprocess.run(
                    [TESSERACT_BIN, tmp_path, "stdout", "--psm", "10",
                     "-c", "tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"],
                    capture_output=True, text=True, timeout=5
                )
                tess_ocr = r.stdout.strip()
            except:
                tess_ocr = ""
            
            # Take first character
            tess_char = tess_ocr[0] if tess_ocr else ""
            
            # Also try universal solver
            solver_ocr = await _ocr_with_solver(session, crop_b64)
            solver_char = solver_ocr[0] if solver_ocr else ""
            
            results.append({
                "x": cx, "y": cy,
                "tesseract": tess_char,
                "solver": solver_char,
                "size": s,
            })
        
        # Match target letters to positions
        # Strategy: try both OCR results (case-insensitive) and pick best matches
        target_upper = [c.upper() for c in target_letters]
        matched_points = []
        used_positions = set()
        
        # First pass: exact matches (case-insensitive)
        for target_letter in target_upper:
            best_match = None
            best_score = 0
            for i, res in enumerate(results):
                if i in used_positions:
                    continue
                score = 0
                if res["tesseract"].upper() == target_letter:
                    score = 2
                if res["solver"].upper() == target_letter:
                    score = max(score, 2)
                if score > best_score:
                    best_score = score
                    best_match = i
            
            if best_match is not None and best_score > 0:
                matched_points.append((results[best_match]["x"], results[best_match]["y"]))
                used_positions.add(best_match)
        
        # If we couldn't match all letters, use position-based heuristic
        # (assign remaining candidates to remaining targets by reading order)
        if len(matched_points) < len(target_upper):
            log.warning(f"Only matched {len(matched_points)}/{len(target_upper)} letters via OCR")
            # Sort unmatched candidates by position (left-top to right-bottom)
            unmatched_candidates = [(i, results[i]) for i in range(len(results)) if i not in used_positions]
            unmatched_candidates.sort(key=lambda x: (x[1]["y"] // 50, x[1]["x"]))
            
            remaining_targets = target_upper[len(matched_points):]
            for j, target_letter in enumerate(remaining_targets):
                if j < len(unmatched_candidates):
                    idx, res = unmatched_candidates[j]
                    matched_points.append((res["x"], res["y"]))
                    used_positions.add(idx)
        
        if len(matched_points) == len(target_upper):
            coord_str = ";".join(f"x={x},y={y}" for x, y in matched_points)
            answer = f"coordinates:{coord_str}"
            log.info(f"Coordinate answer: {answer}")
            return answer
        else:
            log.error(f"Could not find all {len(target_upper)} target letters (found {len(matched_points)})")
            return None
        
    except Exception as e:
        log.error(f"Coordinate detection error: {e}")
        return None


async def complete_training(session: Optional[aiohttp.ClientSession] = None) -> dict:
    """
    Run the full training flow for the 2captcha account.
    
    Returns dict with:
    - success: bool
    - level: int (if completed)
    - steps_completed: int
    - error: str (if failed)
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
        max_steps = 50
        
        for step in range(max_steps):
            # Get current training step
            async with session.get(
                f"{TRAINING_BASE}/current-step",
                cookies=cookies,
                headers=headers,
            ) as r:
                if r.status != 200:
                    return {"success": False, "error": f"GET failed: {r.status}"}
                data = await r.json()
            
            # Check if training is complete
            is_completed = data.get("isLevelCompleted", False) or data.get("isTrainingCompleted", False)
            if is_completed:
                level = data.get("level", 0)
                log.info(f"✅ Training completed! Level: {level}")
                return {"success": True, "level": level, "steps_completed": steps_completed}
            
            must_skip = data.get("mustSkip", False)
            error_hint = data.get("errorHint", {}).get("en", "")
            hint_text = data.get("hint", {}).get("en", "")
            d = data.get("data", {})
            params = d.get("params", {})
            is_coord = params.get("coordinatescaptcha", 0) or params.get("coordinates_captcha", 0)
            captchatype = d.get("captchatype", "")
            cap_id = d.get("captcha_id", "?")
            
            log.info(f"Step {step+1}: cap={cap_id} type={captchatype} coord={is_coord} skip={must_skip}")
            
            # ── Skip step ──
            if must_skip:
                log.info(f"  -> Skipping (mustSkip=True)")
                async with session.post(
                    f"{TRAINING_BASE}/current-step",
                    cookies=cookies,
                    headers=headers,
                    json={"skip": True},
                ) as r2:
                    log.info(f"  Skip result: {r2.status}")
                steps_completed += 1
                await asyncio.sleep(1)
                continue
            
            # ── Text captcha with errorHint ──
            answer = _extract_answer_from_hint(error_hint)
            if answer and ("Invalid answer" not in error_hint and "be careful" not in error_hint):
                log.info(f"  -> Answer from errorHint: {answer}")
                async with session.post(
                    f"{TRAINING_BASE}/current-step",
                    cookies=cookies,
                    headers=headers,
                    json={"answer": answer},
                ) as r2:
                    status = r2.status
                    body = await r2.text()
                    log.info(f"  Submit: {status} {body[:100]}")
                    if status == 204:
                        steps_completed += 1
                    elif status == 422:
                        log.warning(f"  Wrong answer! Retrying...")
                await asyncio.sleep(1.5)
                continue
            
            # ── Coordinate captcha ──
            if is_coord:
                # Get instruction image
                inst_b64 = d.get("file_textinstruction", "")
                if inst_b64 and "base64," in inst_b64:
                    inst_b64_clean = inst_b64.split("base64,", 1)[1]
                    
                    # OCR the instruction to get target letters
                    target_letters = await _ocr_instruction_tesseract(inst_b64_clean)
                    if not target_letters:
                        # Fallback to universal solver
                        target_letters = await _ocr_with_solver(session, inst_b64_clean)
                    
                    if not target_letters:
                        log.warning("  Could not OCR instruction image, skipping")
                        # Try skip first
                        async with session.post(
                            f"{TRAINING_BASE}/current-step",
                            cookies=cookies,
                            headers=headers,
                            json={"skip": True},
                        ) as r2:
                            if r2.status != 204:
                                log.error(f"  Cannot skip coordinate captcha! Stuck.")
                                return {"success": False, "error": "Cannot skip coordinate captcha"}
                        steps_completed += 1
                        await asyncio.sleep(1)
                        continue
                    
                    log.info(f"  Target letters from instruction: {target_letters}")
                    
                    # Download main image
                    img_url = d.get("image", "")
                    img_data = None
                    if img_url:
                        if img_url.startswith("/"):
                            img_url = f"https://2captcha.com{img_url}"
                        async with session.get(img_url, cookies=cookies) as r_img:
                            img_data = await r_img.read()
                    
                    if img_data:
                        coord_answer = await _find_letter_coordinates(
                            session, img_data, inst_b64_clean, target_letters
                        )
                        if coord_answer:
                            log.info(f"  Coordinate answer: {coord_answer}")
                            async with session.post(
                                f"{TRAINING_BASE}/current-step",
                                cookies=cookies,
                                headers=headers,
                                json={"answer": coord_answer},
                            ) as r2:
                                status = r2.status
                                body = await r2.text()
                                log.info(f"  Submit: {status} {body[:100]}")
                                if status == 204:
                                    steps_completed += 1
                            await asyncio.sleep(1.5)
                            continue
                    
                    # If coordinate solving failed, try skip
                    log.warning("  Coordinate solving failed, trying skip...")
                    async with session.post(
                        f"{TRAINING_BASE}/current-step",
                        cookies=cookies,
                        headers=headers,
                        json={"skip": True},
                    ) as r2:
                        log.info(f"  Skip: {r2.status}")
                    await asyncio.sleep(1)
                    continue
            
            # ── Text captcha with image (no errorHint) ──
            img_data_field = d.get("image", "")
            if img_data_field:
                if "base64," in img_data_field:
                    img_b64 = img_data_field.split("base64,", 1)[1]
                elif img_data_field.startswith("/") or img_data_field.startswith("http"):
                    img_url_val = img_data_field
                    if img_url_val.startswith("/"):
                        img_url_val = f"https://2captcha.com{img_url_val}"
                    async with session.get(img_url_val, cookies=cookies) as r_img:
                        img_bytes = await r_img.read()
                    img_b64 = base64.b64encode(img_bytes).decode()
                else:
                    img_b64 = img_data_field
                
                if img_b64 and len(img_b64) > 100:
                    ocr_answer = await _ocr_with_solver(session, img_b64)
                    if ocr_answer:
                        log.info(f"  OCR answer: {ocr_answer}")
                        async with session.post(
                            f"{TRAINING_BASE}/current-step",
                            cookies=cookies,
                            headers=headers,
                            json={"answer": ocr_answer},
                        ) as r2:
                            status = r2.status
                            body = await r2.text()
                            log.info(f"  Submit: {status} {body[:100]}")
                            if status == 204:
                                steps_completed += 1
                        await asyncio.sleep(1.5)
                        continue
            
            # ── Fallback: try skip ──
            log.warning(f"  No solution found for step, trying skip...")
            async with session.post(
                f"{TRAINING_BASE}/current-step",
                cookies=cookies,
                headers=headers,
                json={"skip": True},
            ) as r2:
                status = r2.status
                if status == 422:
                    log.error("  Cannot skip this step! Training may be stuck.")
                    return {"success": False, "error": "Stuck on unsolvable step", "steps_completed": steps_completed}
            steps_completed += 1
            await asyncio.sleep(1)
        
        return {"success": False, "error": "Max steps reached", "steps_completed": steps_completed}
    
    except Exception as e:
        log.error(f"Training error: {e}")
        return {"success": False, "error": str(e)}
    finally:
        if own_session:
            await session.close()
