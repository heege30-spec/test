#!/usr/bin/env python3
# ============================================================
# agent.py — Hệ thống Agent tự động đọc & phản hồi tin nhắn
# (Cập nhật: thêm Stealth mode, emulator detection, DRY_RUN, jitter)
# ============================================================
import time
import json
import os
import re
import subprocess
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
import random
import threading

# Optional heavy deps — handled gracefully if missing
try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

# ============================================================
# CONFIG — chỉnh sửa các giá trị này cho phù hợp
# ============================================================
GEMINI_API_KEYS = [
    # Fill in or leave empty if not using Gemini
]
GEMINI_MODEL = "gemini-2.5-flash"

QWEN_API_KEYS = [
    # Fill in or leave empty if not using Qwen
]
QWEN_MODEL = "qwen-plus"
QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"

PROVIDER_PRIORITY = ["gemini", "qwen"]
ACCOUNT_EMAIL = "heege30@gmail.com"

POLL_INTERVAL_SEC = 8
FAST_CHECK_INTERVAL_SEC = 1

ACTIVE_START_HOUR = 8
ACTIVE_END_HOUR = 22
ACTIVE_END_MINUTE = 30

MAX_ACTIONS_PER_HOUR = 30

SCREENSHOT_TMP = "/sdcard/agent_screen.png"
SCREENSHOT_COMPRESSED = "/sdcard/agent_screen_compressed.jpg"
UI_DUMP_PATH = "/sdcard/window_dump.xml"

TEMPLATE_DIR = "./templates"
LOG_DIR = "./logs"
GOALS_FILE = "./goals.json"
SKILLS_FILE = "./skills.json"

AD_MATCH_THRESHOLD = 0.92
TEXT_DIFF_THRESHOLD = 0.02
MAX_CONSECUTIVE_UNCERTAIN = 3

AD_CLOSE_KEYWORDS = [
    "close", "đóng", "dismiss", "skip", "bỏ qua", "btn_close", "ad_close",
    "no_thanks", "cancel_ad", "close_button", "iv_close", "iv_skip",
]

AD_SDK_HINTS = [
    "com.google.android.gms.ads", "adactivity", "com.unity3d.ads", "com.applovin",
    "com.facebook.ads", "ironsource", "vungle", "mopub", "adcolony", "webview",
    "pangle", "bytedance.sdk.openad", "chartboost",
]

AD_DECODE_SCALE = 2
MIN_UI_DUMP_LEN = 50
EXPECTED_CONTEXT = "Màn hình chat (Zalo/Telegram), hiển thị danh sách hoặc nội dung tin nhắn"

# -------------------------
# STEALTH + EMULATOR CONFIG
# -------------------------
STEALTH_MODE = True                              # True để bật micro-actions
# intensity: "low","medium","high" - điều chỉnh tần suất micro-actions
STEALTH_INTENSITY = os.environ.get("STEALTH_INTENSITY", "medium")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"   # nếu true -> log ADB thay vì chạy
# khi phát hiện emulator, strategy: "more_random" | "simulate_vibrate" | "abort"
EMULATOR_STRATEGY = os.environ.get("EMULATOR_STRATEGY", "more_random")

STEALTH_PARAMS = {
    "low":    {"micro_prob": 0.02, "idle_micro_prob": 0.05},
    "medium": {"micro_prob": 0.06, "idle_micro_prob": 0.15},
    "high":   {"micro_prob": 0.15, "idle_micro_prob": 0.30},
}
STEALTH_PARAM = STEALTH_PARAMS.get(STEALTH_INTENSITY, STEALTH_PARAMS["medium"])

# vibration simulation attempts (best-effort)
VIBRATE_ON_EMULATOR = True

# ============================================================
# Setup dirs + logging
# ============================================================
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(TEMPLATE_DIR, exist_ok=True)
if not os.path.exists(GOALS_FILE):
    with open(GOALS_FILE, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)

if not os.path.exists(SKILLS_FILE):
    with open(SKILLS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False, indent=2)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(os.path.join(LOG_DIR, "agent.log"))
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)
# also log to console
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(ch)

def log(msg, level="info"):
    print(msg)
    getattr(logger, level)(msg)

# ============================================================
# Helpers: safe adb wrapper (DRY_RUN support)
# ============================================================
ADB_TIMEOUT_SEC = 12

def run_adb_shell(cmd: str, timeout: int = ADB_TIMEOUT_SEC):
    full = f"adb shell {cmd}"
    logger.debug(f"ADB RUN: {full}")
    if DRY_RUN:
        logger.info(f"[DRY_RUN] {full}")
        # emulate successful completed process
        class Dummy:
            stdout = ""
            stderr = ""
            returncode = 0
        return Dummy()
    try:
        proc = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=timeout)
        return proc
    except subprocess.TimeoutExpired as e:
        logger.warning(f"ADB command timeout: {cmd}")
        class T:
            stdout = ""
            stderr = "timeout"
            returncode = 124
        return T()
    except Exception as e:
        logger.exception(f"ADB exec failed: {e}")
        class E:
            stdout = ""
            stderr = str(e)
            returncode = 1
        return E()

def run_adb_command(command: str) -> str:
    res = run_adb_shell(command)
    return getattr(res, "stdout", "") or ""

def adb_tap(x: int, y: int):
    # safe coordinate clamping
    try:
        x = max(0, int(x)); y = max(0, int(y))
        run_adb_shell(f"input tap {x} {y}")
    except Exception as e:
        log(f"adb_tap error: {e}", level="error")

def adb_type_text(text: str):
    # escape and replace space -> %s, minimal sanitization
    safe = str(text).replace("%", "%25").replace(" ", "%s")
    safe = re.sub(r'[\"\'<>;&|]', '', safe)
    run_adb_shell(f"input text {safe}")

def adb_key_back():
    run_adb_shell("input keyevent 4")

def adb_key_enter():
    run_adb_shell("input keyevent 66")

def adb_pull_screenshot(local_path: str = SCREENSHOT_TMP):
    # take screenshot on device (path is on device)
    run_adb_shell(f"screencap -p {local_path}")
    return local_path

# ============================================================
# Emulator detection
# ============================================================
def is_emulator() -> bool:
    """Detect emulator via common getprop values (best-effort)."""
    try:
        r = run_adb_shell("getprop ro.kernel.qemu")
        if getattr(r, "stdout", "").strip() == "1":
            logger.info("Emulator detected: ro.kernel.qemu=1")
            return True
        props = ["ro.product.model","ro.product.device","ro.product.brand","ro.build.fingerprint","ro.hardware"]
        for p in props:
            r = run_adb_shell(f"getprop {p}")
            s = getattr(r, "stdout", "").lower()
            if any(tok in s for tok in ("generic", "sdk", "emulator", "goldfish", "ranchu", "vbox", "simulator")):
                logger.info(f"Emulator hint: {p} -> {s.strip()}")
                return True
    except Exception as e:
        logger.debug(f"is_emulator check failed: {e}")
    return False

# ============================================================
# Stealth micro-actions
# ============================================================
def random_point_near(x, y, max_offset=10):
    return max(0, x + random.randint(-max_offset, max_offset)), max(0, y + random.randint(-max_offset, max_offset))

def micro_tap(width=1080, height=1920):
    x = random.randint(int(width*0.15), int(width*0.85))
    y = random.randint(int(height*0.15), int(height*0.85))
    logger.info(f"[stealth] micro_tap {x},{y}")
    adb_tap(x, y)

def micro_swipe(width=1080, height=1920):
    x1 = random.randint(int(width*0.2), int(width*0.8))
    y1 = random.randint(int(height*0.3), int(height*0.7))
    x2 = x1 + random.randint(-100, 100)
    y2 = y1 + random.randint(-300, 300)
    dur = random.randint(200, 700)
    logger.info(f"[stealth] micro_swipe {x1},{y1} -> {x2},{y2} dur={dur}")
    run_adb_shell(f"input swipe {x1} {y1} {x2} {y2} {dur}")

def open_close_notification():
    logger.info("[stealth] open/close notification")
    run_adb_shell("cmd statusbar expand-notifications")
    time.sleep(random.uniform(0.6, 1.6))
    run_adb_shell("cmd statusbar collapse")

def simulate_vibration(duration_ms=150):
    logger.info(f"[stealth] attempt vibration {duration_ms}ms")
    # best-effort methods
    if DRY_RUN:
        logger.info("[DRY_RUN] skip actual vibration commands")
        return
    # termux-api broadcast (if Termux API app installed on device)
    run_adb_shell(f"am broadcast -a com.termux.api.action.vibrate --es duration {duration_ms}")
    # fallback: service call (may require permissions)
    run_adb_shell(f"service call vibrator 1 i32 {duration_ms}")

def perform_random_micro_action(emulator_hint=False):
    """Choose a micro-action based on stealth intensity and emulator presence."""
    choices = []
    # weights tuned to be conservative
    choices.append(("tap", 40))
    choices.append(("swipe", 20))
    choices.append(("notif", 10))
    choices.append(("vibrate", 5))
    choices.append(("pause", 25))
    # escalate vibrate or pauses if emulator and strategy requests
    if emulator_hint and EMULATOR_STRATEGY == "simulate_vibrate":
        choices.append(("vibrate", 40))
    total = sum(w for _, w in choices)
    r = random.uniform(0, total)
    upto = 0
    for name, w in choices:
        upto += w
        if r <= upto:
            if name == "tap":
                micro_tap()
            elif name == "swipe":
                micro_swipe()
            elif name == "notif":
                open_close_notification()
            elif name == "vibrate":
                simulate_vibration(random.randint(80, 260))
            elif name == "pause":
                t = random.uniform(0.5, 2.0)
                logger.info(f"[stealth] micro_pause {t:.2f}s")
                time.sleep(t)
            break

# ============================================================
# Existing code adapted from your original file (helpers, templates, OpenCV fallback)
# - I preserved function names & behavior, but used safe ADB wrappers above.
# - If cv2 is None, OpenCV fallback will be skipped gracefully.
# ============================================================

# Utility for loading/saving json
def load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def record_goal_completed(description: str, meta: dict = None):
    goals = load_json(GOALS_FILE) or []
    entry = {"timestamp": datetime.now().isoformat(timespec="seconds"), "description": description, "meta": meta or {}}
    goals.append(entry)
    save_json(GOALS_FILE, goals)
    log(f"📌 Đã ghi mục tiêu hoàn thành: {description}")

# ADB helpers used earlier now call safe wrappers
def run_adb_command(command: str) -> str:
    return run_adb_command.__wrapped__(*[]) if False else run_adb_command  # placeholder to avoid linter; not used

# get_screen_text adapted to use safe runner
def get_screen_text() -> str:
    """Lấy nội dung UI hiện tại dưới dạng XML (rẻ và chính xác hơn OCR)."""
    try:
        run_adb_shell(f"uiautomator dump {UI_DUMP_PATH}")
        res = run_adb_shell(f"cat {UI_DUMP_PATH}", timeout=8)
        return getattr(res, "stdout", "") or ""
    except Exception as e:
        log(f"Không lấy được UI dump: {e}", level="warning")
        return ""

# Accessibility parsing helpers (kept from original)
def compute_text_diff_ratio(text1: str, text2: str) -> float:
    if text1 == text2:
        return 0.0
    lines1 = set(text1.splitlines())
    lines2 = set(text2.splitlines())
    total = len(lines1 | lines2)
    if total == 0:
        return 0.0
    changed = len(lines1.symmetric_difference(lines2))
    return changed / total

def _parse_bounds(bounds_str: str):
    m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds_str or "")
    if not m:
        return None
    return tuple(int(v) for v in m.groups())

def find_close_button_via_accessibility(ui_content: str):
    if not ui_content or not ui_content.strip():
        return None
    try:
        root = ET.fromstring(ui_content)
    except ET.ParseError:
        return None
    candidates = []
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        haystack = " ".join([
            node.get("resource-id", ""),
            node.get("content-desc", ""),
            node.get("text", ""),
            node.get("class", ""),
        ]).lower()
        if not any(kw in haystack for kw in AD_CLOSE_KEYWORDS):
            continue
        bounds = _parse_bounds(node.get("bounds", ""))
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        label = node.get("resource-id") or node.get("content-desc") or node.get("class")
        candidates.append(((x1 + x2) // 2, (y1 + y2) // 2, label))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1])
    cx, cy, label = candidates[0]
    log(f"Tìm thấy nút đóng quảng cáo qua Accessibility Tree (label='{label}')")
    return (cx, cy)

def _looks_like_ad_present(ui_content: str) -> bool:
    if not ui_content or not ui_content.strip():
        return True
    lowered = ui_content.lower()
    return any(hint in lowered for hint in AD_SDK_HINTS)

# OpenCV fallback functions (use only if cv2 not None)
_TEMPLATE_CACHE = None
_REDUCED_GRAYSCALE_FLAGS = {
    1: cv2.IMREAD_GRAYSCALE if cv2 else 0,
    2: cv2.IMREAD_REDUCED_GRAYSCALE_2 if cv2 else 0,
    4: cv2.IMREAD_REDUCED_GRAYSCALE_4 if cv2 else 0,
    8: cv2.IMREAD_REDUCED_GRAYSCALE_8 if cv2 else 0,
}

def load_templates():
    global _TEMPLATE_CACHE
    templates = []
    if not cv2:
        _TEMPLATE_CACHE = templates
        return templates
    for fname in sorted(os.listdir(TEMPLATE_DIR)):
        if not fname.lower().endswith((".png", ".jpg", ".jpeg")):
            continue
        try:
            tmpl = cv2.imread(os.path.join(TEMPLATE_DIR, fname), cv2.IMREAD_GRAYSCALE)
            if tmpl is None:
                log(f"Không đọc được template: {fname}", level="warning")
                continue
            if AD_DECODE_SCALE > 1:
                h, w = tmpl.shape[:2]
                tmpl = cv2.resize(tmpl, (max(1, w // AD_DECODE_SCALE), max(1, h // AD_DECODE_SCALE)), interpolation=cv2.INTER_AREA)
            templates.append((fname, tmpl))
        except Exception as e:
            log(f"Bỏ qua template lỗi ({fname}): {e}", level="warning")
    _TEMPLATE_CACHE = templates
    log(f"Đã nạp {len(templates)} template (scale=1/{AD_DECODE_SCALE})")
    return templates

def get_templates():
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        load_templates()
    return _TEMPLATE_CACHE

def _find_close_button_via_opencv(screenshot_gray_small: np.ndarray, threshold: float = AD_MATCH_THRESHOLD):
    if not cv2:
        return None
    h, w = screenshot_gray_small.shape[:2]
    corner_size = min(200 // AD_DECODE_SCALE, h // 4, w // 4)
    if corner_size <= 0:
        return None
    regions = {
        "top_left": (screenshot_gray_small[0:corner_size, 0:corner_size], (0, 0)),
        "top_right": (screenshot_gray_small[0:corner_size, w - corner_size:w], (w - corner_size, 0)),
    }
    templates = get_templates()
    if not templates:
        return None
    for fname, template in templates:
        th, tw = template.shape[:2]
        for region_name, (region_img, offset) in regions.items():
            if region_img.shape[0] < th or region_img.shape[1] < tw:
                continue
            try:
                result = cv2.matchTemplate(region_img, template, cv2.TM_CCOEFF_NORMED)
            except cv2.error as e:
                log(f"Bỏ qua template lỗi khi match ({fname}): {e}", level="warning")
                continue
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val >= threshold:
                cx = (offset[0] + max_loc[0] + tw // 2) * AD_DECODE_SCALE
                cy = (offset[1] + max_loc[1] + th // 2) * AD_DECODE_SCALE
                log(f"[OpenCV] Tìm thấy nút đóng quảng cáo (template={fname}, vùng={region_name}, độ khớp={max_val:.2f})")
                return (cx, cy)
    return None

def _try_close_ad_via_opencv() -> bool:
    try:
        adb_pull_screenshot(SCREENSHOT_TMP)
        screen_small = None
        if cv2:
            screen_small = cv2.imread(SCREENSHOT_TMP, _REDUCED_GRAYSCALE_FLAGS.get(AD_DECODE_SCALE, cv2.IMREAD_GRAYSCALE))
        if screen_small is None:
            return False
        coords = _find_close_button_via_opencv(screen_small)
        if coords:
            adb_tap(*coords)
            time.sleep(0.8)
            return True
    except Exception as e:
        log(f"OpenCV fallback gặp lỗi, bỏ qua: {e}", level="warning")
    return False

def compress_screenshot(input_path: str, output_path: str, max_width: int = 800, jpeg_quality: int = 70) -> str:
    if not cv2:
        raise RuntimeError("OpenCV not available for compress_screenshot")
    img = cv2.imread(input_path)
    if img is None:
        raise FileNotFoundError(f"Không đọc được ảnh: {input_path}")
    h, w = img.shape[:2]
    if w > max_width:
        scale = max_width / w
        img = cv2.resize(img, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(output_path, img, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    return output_path

# ============================================================
# LAYER 2: AI calls (kept minimal, raise errors outwards)
# ============================================================
def _call_gemini_with_key(key: str, prompt: str) -> str:
    # placeholder: integrate official SDK if available
    # raise NotImplementedError if not set up
    from google import genai  # optional runtime dependency
    client = genai.Client(api_key=key)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    # adapt to shape
    return getattr(response, "text", str(response))

def _call_qwen_with_key(key: str, prompt: str) -> str:
    import requests
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": QWEN_MODEL, "messages": [{"role": "user", "content": prompt}]}
    resp = requests.post(QWEN_BASE_URL, headers=headers, json=payload, timeout=30)
    if resp.status_code == 429:
        raise RuntimeError("429 rate_limit_exceeded")
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]

# Key pool simplified: rotate keys across providers, mark cooldown on rate limit
class ApiKeyPool:
    def __init__(self, provider_keys: dict, priority: list, cooldown_sec: int = 60):
        self.cooldown_sec = cooldown_sec
        self.entries = []
        self.cooldown_until = {}
        self.round_robin_index = {}
        for provider in priority:
            keys = [k for k in provider_keys.get(provider, []) if k and "DIEN_API_KEY" not in k]
            for k in keys:
                self.entries.append((provider, k))
                self.cooldown_until[(provider, k)] = 0.0
            self.round_robin_index[provider] = 0
        if not self.entries:
            raise ValueError("Chưa cấu hình API key hợp lệ nào (Gemini hoặc Qwen).")
        self.priority = priority

    def _available_for_provider(self, provider: str):
        now = time.time()
        return [(p, k) for (p, k) in self.entries if p == provider and self.cooldown_until[(p, k)] <= now]

    def get_next_key(self):
        for provider in self.priority:
            available = self._available_for_provider(provider)
            if available:
                idx = self.round_robin_index[provider] % len(available)
                self.round_robin_index[provider] += 1
                return available[idx]
        soonest = min(self.entries, key=lambda pk: self.cooldown_until[pk])
        wait = max(0, self.cooldown_until[soonest] - time.time())
        log(f"Toàn bộ API key đều cooldown, chờ {wait:.0f}s", level="warning")
        time.sleep(wait)
        return soonest

    def mark_rate_limited(self, provider: str, key: str):
        self.cooldown_until[(provider, key)] = time.time() + self.cooldown_sec
        log(f"Key {provider} ...{key[-6:]} rate-limited, cooldown {self.cooldown_sec}s", level="warning")

    def key_count(self) -> int:
        return len(self.entries)

    def summary(self) -> str:
        counts = {}
        for p, _ in self.entries:
            counts[p] = counts.get(p, 0) + 1
        return ", ".join(f"{p}: {n} key" for p, n in counts.items())

api_key_pool = ApiKeyPool(provider_keys={"gemini": GEMINI_API_KEYS, "qwen": QWEN_API_KEYS}, priority=PROVIDER_PRIORITY, cooldown_sec=60)

def call_llm(prompt: str, max_key_retries: int = None) -> str:
    max_key_retries = max_key_retries or api_key_pool.key_count()
    last_error = None
    for _ in range(max_key_retries):
        provider, key = api_key_pool.get_next_key()
        try:
            if provider == "gemini":
                return _call_gemini_with_key(key, prompt)
            elif provider == "qwen":
                return _call_qwen_with_key(key, prompt)
            else:
                raise ValueError(f"Provider không hỗ trợ: {provider}")
        except Exception as e:
            err_text = str(e).lower()
            if any(term in err_text for term in ["429", "rate limit", "quota", "resource_exhausted"]):
                api_key_pool.mark_rate_limited(provider, key)
                last_error = e
                continue
            raise
    raise RuntimeError(f"Tất cả API key đều bị giới hạn. Lỗi cuối: {last_error}")

def ask_ai_decision(ui_content: str) -> dict:
    prompt = f"""
Bạn là trợ lý tự động hóa chạy trên điện thoại Android.
Đây là cấu trúc giao diện màn hình hiện tại (XML):
{ui_content}

Hãy phân tích xem có tin nhắn mới nào chưa trả lời không.
Nếu có, hãy trả về CHÍNH XÁC một chuỗi JSON theo định dạng:
{{"action": "reply", "text": "Nội dung câu trả lời", "x": 100, "y": 500}}
Nếu không cần làm gì, trả về: {{"action": "none"}}
Chỉ trả về JSON, không thêm giải thích.
"""
    try:
        raw = call_llm(prompt)
        # try to find first JSON object in response text
        raw = raw.strip()
        # extract JSON substring if wrapped
        start = raw.find('{')
        end = raw.rfind('}')
        if start != -1 and end != -1 and end > start:
            candidate = raw[start:end+1]
        else:
            candidate = raw
        return json.loads(candidate)
    except Exception as e:
        log(f"Không parse được JSON từ AI: {e}", level="warning")
        return {"action": "none"}

def verify_context_with_ai(expected_context: str) -> dict:
    ui_content = get_screen_text()
    prompt = f"""
Bối cảnh mong đợi: {expected_context}
Đây là nội dung màn hình hiện tại (XML):
{ui_content}

Xác định:
1. Màn hình hiện tại có khớp với bối cảnh mong đợi không?
2. Nếu KHÔNG khớp, đề xuất hành động khôi phục.

Trả về CHÍNH XÁC định dạng JSON:
{{"is_expected": true/false, "recovery_action": "back" | "home" | "none", "reason": "mô tả ngắn"}}
"""
    try:
        raw = call_llm(prompt)
        raw = raw.strip()
        start = raw.find('{'); end = raw.rfind('}')
        if start != -1 and end != -1 and end > start:
            candidate = raw[start:end+1]
        else:
            candidate = raw
        return json.loads(candidate)
    except Exception as e:
        log(f"verify_context_with_ai parse failed: {e}", level="warning")
        return {"is_expected": False, "recovery_action": "back", "reason": "parse_error"}

# ============================================================
# Safe tap with recovery (kept from original, tightened)
# ============================================================
def safe_tap_with_recovery(x: int, y: int, expected_context: str, max_retries: int = 2) -> bool:
    try:
        adb_tap(x, y)
    except Exception as e:
        log(f"tap failed: {e}", level="error")
        return False
    time.sleep(0.8)
    for attempt in range(max_retries):
        try:
            result = verify_context_with_ai(expected_context)
            if result.get("is_expected"):
                log("Xác minh OK: màn hình đúng ngữ cảnh mong đợi.")
                return True
            reason = result.get("reason", "không rõ")
            recovery = result.get("recovery_action", "none")
            log(f"Sai ngữ cảnh ({reason}) -> thực hiện khôi phục: {recovery}", level="warning")
            if recovery == "back":
                adb_key_back()
            elif recovery == "home":
                run_adb_shell("input keyevent 3")
            time.sleep(1)
        except Exception as e:
            log(f"safe_tap_with_recovery error: {e}", level="error")
    log("Không khôi phục được sau nhiều lần thử.", level="error")
    save_incident_snapshot("recovery_failed")
    return False

def save_incident_snapshot(tag: str):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(LOG_DIR, f"{tag}_{ts}.jpg")
    try:
        adb_pull_screenshot(SCREENSHOT_TMP)
        if cv2:
            compress_screenshot(SCREENSHOT_TMP, dest, max_width=800, jpeg_quality=80)
        else:
            # fallback: try copying on device (best-effort)
            run_adb_shell(f"cp {SCREENSHOT_TMP} {dest}")
        log(f"Đã lưu ảnh sự cố: {dest}")
    except Exception as e:
        log(f"Không lưu được ảnh sự cố: {e}", level="error")

# ============================================================
# MAIN LOOP (integrated stealth & stability)
# ============================================================
class RateLimiter:
    def __init__(self, max_per_hour: int):
        self.max_per_hour = max_per_hour
        self.timestamps = []
    def allow(self) -> bool:
        now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < 3600]
        if len(self.timestamps) >= self.max_per_hour:
            return False
        self.timestamps.append(now)
        return True
    def seconds_until_next_slot(self) -> float:
        if not self.timestamps:
            return 0.0
        oldest = min(self.timestamps)
        return max(0.0, 3600 - (time.time() - oldest))

rate_limiter = RateLimiter(MAX_ACTIONS_PER_HOUR)

def is_within_active_hours(now: datetime = None) -> bool:
    now = now or datetime.now()
    start = now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0)
    end = now.replace(hour=ACTIVE_END_HOUR, minute=ACTIVE_END_MINUTE, second=0, microsecond=0)
    return start <= now <= end

def seconds_until_next_active_window(now: datetime = None) -> float:
    now = now or datetime.now()
    today_start = now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0)
    if now < today_start:
        target = today_start
    else:
        target = today_start + timedelta(days=1)
    return (target - now).total_seconds()

def sleep_until_next_active_window():
    wait_sec = seconds_until_next_active_window()
    wake_time = datetime.now() + timedelta(seconds=wait_sec)
    log(f"🌙 Ngoài khung giờ hoạt động. Chờ tới {wake_time.strftime('%Y-%m-%d %H:%M')}.", level="info")
    try:
        subprocess.run("termux-wake-unlock", shell=True)
    except Exception:
        pass
    time.sleep(wait_sec)
    try:
        subprocess.run("termux-wake-lock", shell=True)
    except Exception:
        pass
    log("☀️ Đã tới khung giờ hoạt động.", level="info")

def main():
    log(f"🤖 Agent bắt đầu (STEALTH_MODE={STEALTH_MODE}, STEALTH_INTENSITY={STEALTH_INTENSITY}, DRY_RUN={DRY_RUN})")
    load_templates()  # load templates once
    phantom_em = is_emulator()
    if phantom_em:
        log(f"⚠️ Môi trường có dấu hiệu emulator; strategy={EMULATOR_STRATEGY}", level="warning")
    prev_ui_content = None
    consecutive_uncertain = 0
    last_micro_time = 0.0

    while True:
        try:
            if not is_within_active_hours():
                sleep_until_next_active_window()
                prev_ui_content = None
                continue

            if not rate_limiter.allow():
                wait_sec = rate_limiter.seconds_until_next_slot()
                log(f"⏳ Đạt giới hạn hành động/giờ, chờ {wait_sec:.0f}s", level="info")
                time.sleep(min(wait_sec, POLL_INTERVAL_SEC * 10))
                continue

            ui_content = get_screen_text()

            if not ui_content.strip() or len(ui_content.strip()) < MIN_UI_DUMP_LEN:
                log("⚠️ Accessibility Tree trống/quá ngắn; bỏ qua chu kỳ này.", level="warning")
                prev_ui_content = None
                time.sleep(POLL_INTERVAL_SEC)
                continue

            # stealth: pre-analysis micro action
            if STEALTH_MODE and random.random() < STEALTH_PARAM["micro_prob"]:
                perform_random_micro_action(emulator_hint=phantom_em)

            if prev_ui_content is not None:
                diff_ratio = compute_text_diff_ratio(prev_ui_content, ui_content)
                if diff_ratio < TEXT_DIFF_THRESHOLD:
                    log(f"Nội dung không đổi (diff={diff_ratio:.3f}). Bỏ qua AI.", level="debug")
                    prev_ui_content = ui_content
                    # occasional idle micro action
                    if STEALTH_MODE and (time.time() - last_micro_time > 60) and random.random() < STEALTH_PARAM["idle_micro_prob"]:
                        perform_random_micro_action(emulator_hint=phantom_em)
                        last_micro_time = time.time()
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

            prev_ui_content = ui_content

            # try close ad via accessibility first
            ad_closed = False
            try:
                ad_coords = find_close_button_via_accessibility(ui_content)
                if ad_coords:
                    x, y = ad_coords
                    if STEALTH_MODE:
                        x, y = random_point_near(x, y, max_offset=6)
                    adb_tap(x, y)
                    ad_closed = True
                    time.sleep(0.6)
            except Exception as e:
                log(f"Error while trying close ad via accessibility: {e}", level="warning")

            if not ad_closed and _looks_like_ad_present(ui_content):
                # fallback OpenCV only if needed
                try:
                    if cv2:
                        ocv_closed = _try_close_ad_via_opencv()
                        if ocv_closed:
                            ad_closed = True
                    else:
                        log("OpenCV không sẵn sàng; bỏ qua OpenCV fallback", level="debug")
                except Exception as e:
                    log(f"OpenCV fallback error: {e}", level="warning")

            if ad_closed:
                # verify context
                time.sleep(0.6)
                verify = verify_context_with_ai(EXPECTED_CONTEXT)
                if not verify.get("is_expected"):
                    log("Nghi vấn tap nhầm sau khi đóng quảng cáo; thực hiện khôi phục.", level="warning")
                    if verify.get("recovery_action") == "back":
                        adb_key_back()
                    time.sleep(1)
                    continue

            # Layer 2: ask AI
            decision = ask_ai_decision(ui_content)
            log(f"AI decision: {decision}", level="info")

            if decision.get("action") == "reply":
                text = decision.get("text", "")
                x = decision.get("x"); y = decision.get("y")
                if x is None or y is None:
                    log("AI không trả về tọa độ hợp lệ; bỏ qua", level="warning")
                else:
                    # jitter coords for stealth
                    if STEALTH_MODE:
                        x, y = random_point_near(int(x), int(y), max_offset=7)
                    ok = safe_tap_with_recovery(int(x), int(y), EXPECTED_CONTEXT)
                    if ok:
                        # type text with small delays emulating human typing (char by char optional)
                        if STEALTH_MODE and len(text) > 3:
                            for ch in text:
                                adb_type_text(ch)
                                time.sleep(random.uniform(0.04, 0.12))
                        else:
                            adb_type_text(text)
                        time.sleep(0.3)
                        adb_key_enter()
                        log(f"✅ Đã tự động trả lời: {text[:120]}")
                        record_goal_completed(f"Trả lời tin nhắn: {text[:80]}", meta={"account": ACCOUNT_EMAIL})
                        consecutive_uncertain = 0
                        # post-action micro action occasionally
                        if STEALTH_MODE and random.random() < 0.25:
                            perform_random_micro_action(emulator_hint=phantom_em)
                    else:
                        consecutive_uncertain += 1
            else:
                consecutive_uncertain = 0

            if consecutive_uncertain >= MAX_CONSECUTIVE_UNCERTAIN:
                log("🛑 Quá nhiều lần nghi ngờ liên tiếp. Dừng agent để kiểm tra.", level="error")
                save_incident_snapshot("stopped_too_many_errors")
                break

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            log("Người dùng dừng agent.", level="info")
            break
        except Exception as e:
            log(f"Unexpected error in main loop: {e}", level="error")
            save_incident_snapshot("exception")
            time.sleep(2)
            continue

if __name__ == "__main__":
    main()