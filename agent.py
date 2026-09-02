#!/usr/bin/env python3
# ============================================================
# agent.py — Hệ thống Agent tự động đọc & phản hồi tin nhắn
# (Bổ sung: Stealth mode, emulator handling, human-like typing, stability tweaks)
# ============================================================
# (Phần mô tả kiến trúc giữ nguyên như file gốc)
# ============================================================

import time
import json
import os
import re
import subprocess
import logging
import threading
import queue
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import random

import numpy as np
from PIL import Image
from numpy.lib.stride_tricks import sliding_window_view

# ============================================================
# CONFIG — chỉnh sửa các giá trị này cho phù hợp
# ============================================================

GEMINI_API_KEYS = [
    "DIEN_API_KEY_GEMINI_1_VAO_DAY",
    "DIEN_API_KEY_GEMINI_2_VAO_DAY",
]
GEMINI_MODEL = "gemini-2.5-flash"

QWEN_API_KEYS = [
    "DIEN_API_KEY_QWEN_1_VAO_DAY",
]
QWEN_MODEL = "qwen-plus"
QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"

PROVIDER_PRIORITY = ["gemini", "qwen"]

ACCOUNT_EMAIL = "heege30@gmail.com"

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

AD_MATCH_MAX_DIFF = 25
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

# ================= Stealth / Humanization settings =================
# Bật chế độ này để mô phỏng hành vi người: jitter, typing chậm, long-press, v.v.
STEALTH_MODE = False
STEALTH_JITTER_PIXELS = 6            # +/- pixels khi tap
STEALTH_MIN_DELAY = 0.03             # base sleep trước hành động khi stealth
STEALTH_MAX_DELAY = 0.25

STEALTH_HUMAN_TYPING = True
STEALTH_TYPING_MIN = 0.03
STEALTH_TYPING_MAX = 0.16

STEALTH_LONG_PRESS_PROB = 0.03       # xác suất dùng long-press thay vì tap khi stealth
STEALTH_LONG_PRESS_DURATION_MS = 700

# Emulator-specific behavior
EMULATOR_BEHAVIOR_ENABLED = True
EMULATOR_LONG_PRESS_DURATION_MS = 1000
EMULATOR_SHAKE_ENABLED = True
# =================================================================

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(TEMPLATE_DIR, exist_ok=True)

if not os.path.exists(GOALS_FILE):
    with open(GOALS_FILE, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)

if not os.path.exists(SKILLS_FILE):
    with open(SKILLS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False, indent=2)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, "agent.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)


# ============================================================
# NẠP API KEY TỪ config.json (TUỲ CHỌN — không bắt buộc)
# ============================================================
CONFIG_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_api_keys_from_config_json():
    global GEMINI_API_KEYS, QWEN_API_KEYS, STEALTH_MODE
    if not os.path.exists(CONFIG_JSON_PATH):
        return
    try:
        with open(CONFIG_JSON_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log(f"⚠️ Không đọc được config.json ({e}) — dùng cấu hình trong code.", level="warning")
        return

    gemini_keys = cfg.get("GEMINI_API_KEYS")
    if not gemini_keys and cfg.get("GEMINI_API_KEY"):
        gemini_keys = [cfg["GEMINI_API_KEY"]]
    if gemini_keys:
        GEMINI_API_KEYS = gemini_keys

    qwen_keys = cfg.get("QWEN_API_KEYS")
    if not qwen_keys and cfg.get("QWEN_API_KEY"):
        qwen_keys = [cfg["QWEN_API_KEY"]]
    if qwen_keys:
        QWEN_API_KEYS = qwen_keys

    # optional stealth flag in config.json
    if "STEALTH_MODE" in cfg:
        try:
            STEALTH_MODE = bool(cfg["STEALTH_MODE"])
        except Exception:
            pass

    if gemini_keys or qwen_keys:
        log(f"🔑 Đã nạp API key từ {CONFIG_JSON_PATH}.")


_load_api_keys_from_config_json()


# ============================================================
# KHUNG GIỜ HOẠT ĐỘNG + GIỚI HẠN TẦN SUẤT
# (giữ nguyên như gốc)
# ============================================================

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
    log(f"😴 Ngoài khung giờ hoạt động ({ACTIVE_START_HOUR}:00 - {ACTIVE_END_HOUR}:{ACTIVE_END_MINUTE:02d}). "
        f"Chuyển sang chế độ chờ tới {wake_time.strftime('%d/%m %H:%M')}.")
    subprocess.run("termux-wake-unlock", shell=True)
    time.sleep(wait_sec)
    subprocess.run("termux-wake-lock", shell=True)
    log("☀️ Đã tới khung giờ hoạt động, agent tiếp tục làm việc.")


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
            return 0
        oldest = min(self.timestamps)
        return max(0, 3600 - (time.time() - oldest))


rate_limiter = RateLimiter(MAX_ACTIONS_PER_HOUR)


# ============================================================
# QUẢN LÝ NHIỀU API KEY
# (giữ nguyên)
# ============================================================

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
            raise ValueError("Chưa cấu hình API key hợp lệ nào (Gemini hoặc Qwen) trong CONFIG.")
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
        log(f"⏳ Toàn bộ API key (mọi nhà cung cấp) đều đang tạm nghỉ, chờ {wait:.0f}s.", level="warning")
        time.sleep(wait)
        return soonest

    def mark_rate_limited(self, provider: str, key: str):
        self.cooldown_until[(provider, key)] = time.time() + self.cooldown_sec
        log(f"⚠️ Key {provider} ...{key[-6:]} bị giới hạn hạn mức, tạm nghỉ {self.cooldown_sec}s.", level="warning")

    def key_count(self) -> int:
        return len(self.entries)

    def summary(self) -> str:
        counts = {}
        for p, _ in self.entries:
            counts[p] = counts.get(p, 0) + 1
        return ", ".join(f"{p}: {n} key" for p, n in counts.items())


api_key_pool = ApiKeyPool(provider_keys={"gemini": GEMINI_API_KEYS, "qwen": QWEN_API_KEYS},
                          priority=PROVIDER_PRIORITY, cooldown_sec=60)


# ============================================================
# LƯU MỤC TIÊU & KỸ NĂNG THEO TỪNG LOẠI TRANG/APP (giữ nguyên)
# ============================================================

def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_goal_completed(description: str, meta: dict = None):
    goals = load_json(GOALS_FILE)
    entry = {"timestamp": datetime.now().isoformat(timespec="seconds"), "description": description, "meta": meta or {}}
    goals.append(entry)
    save_json(GOALS_FILE, goals)
    log(f"📌 Đã ghi mục tiêu hoàn thành: {description}")


def get_skill_for_target(target_key: str) -> dict:
    skills = load_json(SKILLS_FILE)
    return skills.get(target_key, {})


def upsert_skill_for_target(target_key: str, config: dict):
    skills = load_json(SKILLS_FILE)
    skills[target_key] = config
    save_json(SKILLS_FILE, skills)
    log(f"🛠️ Đã cập nhật cấu hình cho: {target_key}")


# ============================================================
# LỚP TIỆN ÍCH: điều khiển ADB (cải thiện ổn định + stealth)
# ============================================================

def run_adb_command(command: str, timeout: int = 20) -> str:
    """Chạy một lệnh adb shell, trả về stdout. Không raise, chỉ log lỗi/timeout."""
    try:
        result = subprocess.run(f"adb shell {command}", shell=True, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            log(f"Lệnh adb lỗi: adb shell {command} -> {result.stderr.strip()}", level="warning")
        return result.stdout or ""
    except subprocess.TimeoutExpired:
        log(f"Lệnh adb timeout: adb shell {command}", level="warning")
        return ""
    except Exception as e:
        log(f"Lỗi khi chạy adb command 'adb shell {command}': {e}", level="error")
        return ""


_device_action_lock = threading.Lock()


def _maybe_sleep_for_stealth():
    if STEALTH_MODE:
        s = random.uniform(STEALTH_MIN_DELAY, STEALTH_MAX_DELAY)
        time.sleep(s)


def _jitter_coords(x: int, y: int):
    if STEALTH_MODE and STEALTH_JITTER_PIXELS > 0:
        dx = random.randint(-STEALTH_JITTER_PIXELS, STEALTH_JITTER_PIXELS)
        dy = random.randint(-STEALTH_JITTER_PIXELS, STEALTH_JITTER_PIXELS)
        return max(0, x + dx), max(0, y + dy)
    return x, y


def adb_tap(x: int, y: int, long_press: bool = False, long_press_ms: int = None):
    """Tap an toàn (với jitter và delay khi Stealth mode). Long-press được cài bằng swipe same-point."""
    with _device_action_lock:
        try:
            _maybe_sleep_for_stealth()
            tx, ty = _jitter_coords(x, y)
            if long_press or (STEALTH_MODE and random.random() < STEALTH_LONG_PRESS_PROB):
                dur = long_press_ms or STEALTH_LONG_PRESS_DURATION_MS
                run_adb_command(f"input swipe {tx} {ty} {tx} {ty} {int(dur)}")
                log(f"[ACTION] long-press tại ({tx},{ty}) dur={dur}ms (jittered).")
            else:
                run_adb_command(f"input tap {tx} {ty}")
                log(f"[ACTION] tap tại ({tx},{ty}) (jittered).")
        except Exception as e:
            log(f"Lỗi khi thực hiện tap: {e}", level="error")


def adb_type_text(text: str, human_like: bool = False):
    """Gõ văn bản. Nếu human_like True, sẽ gửi từng ký tự (chậm) để mô phỏng người."""
    with _device_action_lock:
        try:
            if STEALTH_MODE and human_like and STEALTH_HUMAN_TYPING:
                for ch in text:
                    safe = ch
                    if safe == " ":
                        safe = "%s"
                    # nhiều ký tự đặc biệt có thể gây vấn đề; gửi từng ký tự là phương án an toàn.
                    run_adb_command(f"input text {safe}")
                    time.sleep(random.uniform(STEALTH_TYPING_MIN, STEALTH_TYPING_MAX))
                log(f"[ACTION] human-typed text (len={len(text)})")
            else:
                safe_text = text.replace(" ", "%s")
                run_adb_command(f"input text {safe_text}")
                log(f"[ACTION] input text (fast) len={len(text)}")
        except Exception as e:
            log(f"Lỗi khi gõ text: {e}", level="error")


def adb_key_back():
    with _device_action_lock:
        run_adb_command("input keyevent 4")


def adb_key_enter():
    with _device_action_lock:
        run_adb_command("input keyevent 66")


def adb_pull_screenshot(local_path: str = SCREENSHOT_TMP):
    subprocess.run(f"adb shell screencap -p {local_path}", shell=True)
    return local_path


def get_screen_text() -> str:
    try:
        run_adb_command(f"uiautomator dump {UI_DUMP_PATH}")
        dump_result = subprocess.run(f"adb shell cat {UI_DUMP_PATH}", shell=True, capture_output=True, text=True, timeout=15)
        return dump_result.stdout or ""
    except Exception as e:
        log(f"Không lấy được UI dump (accessibility có thể đang tắt): {e}", level="warning")
        return ""


# ============================================================
# PHÁT HIỆN MÁY ẢO / HÀNH VI RIÊNG CHO EMULATOR
# ============================================================

_IS_EMULATOR = None
_EMULATOR_CHECK_LOCK = threading.Lock()


def detect_emulator(force_reload: bool = False) -> bool:
    """Phát hiện emulator/virtual device qua các property. Kết quả cache lại."""
    global _IS_EMULATOR
    with _EMULATOR_CHECK_LOCK:
        if _IS_EMULATOR is not None and not force_reload:
            return _IS_EMULATOR
        try:
            props = run_adb_command("getprop")
            lower = props.lower()
            indicators = [
                "ro.kernel.qemu", "generic sdk", "emulator", "virtual", "ro.product.model", "genymotion",
                "goldfish", "ranchu", "sdk_gphone", "sdk"
            ]
            _IS_EMULATOR = any(ind in lower for ind in indicators)
        except Exception as e:
            log(f"Lỗi khi detect emulator: {e}", level="warning")
            _IS_EMULATOR = False
        log(f"🧭 detect_emulator -> {_IS_EMULATOR}")
        return _IS_EMULATOR


def simulate_vibrate(duration_ms: int = 200):
    """Cố gắng gọi API vibrator; fallback bằng một chuỗi swipe ngắn nếu không hỗ trợ."""
    try:
        out = run_adb_command(f"cmd vibrator vibrate {int(duration_ms)}")
        if not out:
            # fallback: nhiều swipe ngắn (không thực sự rung nhưng tạo hoạt động)
            for _ in range(3):
                run_adb_command("input swipe 300 500 320 480 50")
                time.sleep(0.05)
        log(f"[EMULATION] simulate_vibrate {duration_ms}ms")
    except Exception as e:
        log(f"simulate_vibrate lỗi: {e}", level="warning")


def simulate_shake():
    """Cố gắng mô phỏng 'lắc' thiết bị: gọi vibrator nếu có, hoặc vài swipe nhanh."""
    try:
        simulate_vibrate(180)
        for _ in range(4):
            run_adb_command("input swipe 200 600 400 400 40")
            run_adb_command("input swipe 400 400 200 600 40")
            time.sleep(0.06)
        log("[EMULATION] simulate_shake executed")
    except Exception as e:
        log(f"simulate_shake lỗi: {e}", level="warning")


# ============================================================
# LỚP 1a/1b: ACCESSIBILITY + IMAGE MATCH (Pillow + numpy) - giữ nguyên logic
# ============================================================

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


def try_close_ad(ui_content: str) -> bool:
    coords = find_close_button_via_accessibility(ui_content)
    if coords:
        adb_tap(*coords)
        time.sleep(0.8)
        return True

    if not _looks_like_ad_present(ui_content):
        return False

    log("Accessibility Tree không thấy nút đóng nhưng nghi ngờ có quảng cáo -> dùng so khớp ảnh (phương án cuối) để tìm.")
    return _try_close_ad_via_image_match()


# ================= Image match using Pillow + numpy (giữ logic gốc) =================

_TEMPLATE_CACHE = None


def load_gray_array(path: str, scale: int = 1) -> "np.ndarray | None":
    try:
        with Image.open(path) as img:
            img = img.convert("L")
            if scale > 1:
                new_w = max(1, img.width // scale)
                new_h = max(1, img.height // scale)
                img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
            return np.array(img, dtype=np.uint8)
    except Exception as e:
        log(f"Không đọc được ảnh '{path}': {e}", level="warning")
        return None


def load_templates():
    global _TEMPLATE_CACHE
    templates = []
    if os.path.isdir(TEMPLATE_DIR):
        for fname in sorted(os.listdir(TEMPLATE_DIR)):
            if not fname.lower().endswith((".png", ".jpg", ".jpeg")):
                continue
            tmpl = load_gray_array(os.path.join(TEMPLATE_DIR, fname), AD_DECODE_SCALE)
            if tmpl is not None:
                templates.append((fname, tmpl))
    _TEMPLATE_CACHE = templates
    log(f"📦 Đã nạp {len(templates)} template nút đóng quảng cáo vào bộ nhớ (scale=1/{AD_DECODE_SCALE}) — chỉ dùng khi Accessibility Tree bó tay.")
    return templates


def get_templates():
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        load_templates()
    return _TEMPLATE_CACHE


def _best_match_position(region: np.ndarray, template: np.ndarray):
    th, tw = template.shape
    rh, rw = region.shape
    if rh < th or rw < tw:
        return None
    windows = sliding_window_view(region, (th, tw))
    diff = np.abs(windows.astype(np.int16) - template.astype(np.int16))
    mean_diff = diff.mean(axis=(2, 3))
    min_idx = np.unravel_index(np.argmin(mean_diff), mean_diff.shape)
    min_diff = float(mean_diff[min_idx])
    y, x = min_idx
    return min_diff, (int(x), int(y))


def _find_close_button_via_image_match(screenshot_gray_small: np.ndarray, max_diff: float = AD_MATCH_MAX_DIFF):
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
                match = _best_match_position(region_img, template)
            except Exception as e:
                log(f"Bỏ qua template lỗi khi so khớp ({fname}): {e}", level="warning")
                continue
            if match is None:
                continue
            diff_score, (mx, my) = match
            if diff_score <= max_diff:
                cx = (offset[0] + mx + tw // 2) * AD_DECODE_SCALE
                cy = (offset[1] + my + th // 2) * AD_DECODE_SCALE
                log(f"Tìm thấy nút đóng quảng cáo (template={fname}, vùng={region_name}, sai khác={diff_score:.1f})")
                return (cx, cy)
    return None


def _try_close_ad_via_image_match() -> bool:
    try:
        adb_pull_screenshot(SCREENSHOT_TMP)
        screen_small = load_gray_array(SCREENSHOT_TMP, AD_DECODE_SCALE)
        if screen_small is None:
            return False
        coords = _find_close_button_via_image_match(screen_small)
        if coords:
            adb_tap(*coords)
            time.sleep(0.8)
            return True
    except Exception as e:
        log(f"So khớp ảnh (phương án cuối) gặp lỗi, bỏ qua (không dừng agent): {e}", level="warning")
    return False


def compress_screenshot(input_path: str, output_path: str, max_width: int = 800, jpeg_quality: int = 70) -> str:
    with Image.open(input_path) as img:
        img = img.convert("RGB")
        if img.width > max_width:
            new_h = int(img.height * (max_width / img.width))
            img = img.resize((max_width, new_h), Image.Resampling.BILINEAR)
        img.save(output_path, "JPEG", quality=jpeg_quality)
    return output_path


# ============================================================
# LAYER 2: AI CALLS (giữ nguyên)
# ============================================================

def _call_gemini_with_key(key: str, prompt: str) -> str:
    from google import genai
    client = genai.Client(api_key=key)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return response.text


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
            is_rate_limit = any(term in err_text for term in ["429", "rate limit", "quota", "resource_exhausted"])
            if is_rate_limit:
                api_key_pool.mark_rate_limited(provider, key)
                last_error = e
                continue
            raise
    raise RuntimeError(f"Tất cả API key (mọi nhà cung cấp) đều bị giới hạn hạn mức. Lỗi cuối: {last_error}")


def ask_ai_decision(ui_content: str) -> dict:
    prompt = f"""
Bạn là trợ lý tự động hóa chạy trên điện thoại Android.
Đây là cấu trúc giao diện màn hình hiện tại (XML):
{ui_content}

Hãy phân tích xem có tin nhắn mới nào chưa trả lời không.
Nếu có, hãy trả về CHÍNH XÁC một chuỗi JSON theo định dạng:
{{"action": "reply", "text": "Nội dung câu trả lời", "x": 100, "y": 500}}
(x, y là tọa độ ô nhập liệu hoặc nút gửi, ước lượng từ bounds trong XML).
Nếu không cần làm gì, trả về: {{"action": "none"}}
Chỉ trả về JSON, không thêm giải thích hay markdown.
"""
    raw = call_llm(prompt)
    try:
        payload = raw.strip()
        if payload.startswith("```"):
            payload = payload.strip("`").replace("json", "", 1)
        return json.loads(payload)
    except Exception:
        log(f"Không parse được JSON từ AI: {raw}", level="warning")
        return {"action": "none"}


# ============================================================
# LAYER 2b: AI WORKER (giữ nguyên)
# ============================================================

_ai_result_queue = queue.Queue()
_ai_state_lock = threading.Lock()
_ai_busy = False


def hash_ui_content(ui_content: str) -> str:
    return hashlib.sha1((ui_content or "").encode("utf-8", errors="ignore")).hexdigest()


def is_ai_busy() -> bool:
    with _ai_state_lock:
        return _ai_busy


def _ai_worker(ui_content: str, fingerprint: str):
    global _ai_busy
    try:
        decision = ask_ai_decision(ui_content)
        _ai_result_queue.put({"ok": True, "decision": decision, "fingerprint": fingerprint})
    except Exception as e:
        log(f"Luồng AI nền gặp lỗi: {e}", level="error")
        _ai_result_queue.put({"ok": False, "error": str(e), "fingerprint": fingerprint})
    finally:
        with _ai_state_lock:
            _ai_busy = False


def submit_ai_decision_async(ui_content: str) -> bool:
    global _ai_busy
    with _ai_state_lock:
        if _ai_busy:
            return False
        _ai_busy = True
    fingerprint = hash_ui_content(ui_content)
    t = threading.Thread(target=_ai_worker, args=(ui_content, fingerprint), daemon=True)
    t.start()
    return True


def verify_context_with_ai(expected_context: str) -> dict:
    ui_content = get_screen_text()
    prompt = f"""
Bối cảnh mong đợi: {expected_context}
Đây là nội dung màn hình hiện tại (XML):
{ui_content}

Xác định:
1. Màn hình hiện tại có khớp với bối cảnh mong đợi không?
2. Nếu KHÔNG khớp, đề xuất hành động khôi phục.

Trả về CHÍNH XÁC định dạng JSON, không thêm giải thích:
{{"is_expected": true/false, "recovery_action": "back" | "home" | "none", "reason": "mô tả ngắn"}}
"""
    raw = call_llm(prompt)
    try:
        payload = raw.strip()
        if payload.startswith("```"):
            payload = payload.strip("`").replace("json", "", 1)
        return json.loads(payload)
    except Exception:
        return {"is_expected": False, "recovery_action": "back", "reason": "parse_error"}


# ============================================================
# CƠ CHẾ AN TOÀN: tap có xác minh + khôi phục (tích hợp stealth/emulator)
# ============================================================

def safe_tap_with_recovery(x: int, y: int, expected_context: str, max_retries: int = 2) -> bool:
    # Pre-behavior: nếu là emulator và bật hành vi emulator, thực hiện mô phỏng nhỏ
    try:
        if EMULATOR_BEHAVIOR_ENABLED and detect_emulator():
            if EMULATOR_SHAKE_ENABLED:
                simulate_shake()
            # thử long-press ngắn trước khi tap để "tương tác" nhẹ
            adb_tap(x, y, long_press=True, long_press_ms=EMULATOR_LONG_PRESS_DURATION_MS)
            time.sleep(0.4)
    except Exception as e:
        log(f"Lỗi khi thực hiện pre-emulator-behavior: {e}", level="warning")

    adb_tap(x, y)
    time.sleep(0.8)

    for attempt in range(max_retries):
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
            run_adb_command("input keyevent 3")

        time.sleep(1)

    log("Không khôi phục được sau nhiều lần thử. Cần kiểm tra thủ công.", level="error")
    save_incident_snapshot("recovery_failed")
    return False


def save_incident_snapshot(tag: str):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(LOG_DIR, f"{tag}_{ts}.jpg")
    try:
        adb_pull_screenshot(SCREENSHOT_TMP)
        compress_screenshot(SCREENSHOT_TMP, dest, max_width=800, jpeg_quality=80)
        log(f"Đã lưu ảnh sự cố: {dest}")
    except Exception as e:
        log(f"Không lưu được ảnh sự cố: {e}", level="error")


# ============================================================
# VÒNG LẶP CHÍNH (giữ logic gốc, tích hợp stealth/emulator)
# ============================================================

def main():
    log(f"🤖 Agent bắt đầu chạy cho tài khoản cấu hình: {ACCOUNT_EMAIL} ({api_key_pool.summary()})")
    load_templates()
    consecutive_uncertain = 0
    last_submitted_content = None

    # detect emulator once at startup (cache)
    is_emulator = detect_emulator()

    while True:
        try:
            if not is_within_active_hours():
                sleep_until_next_active_window()
                last_submitted_content = None
                continue

            ui_content = get_screen_text()

            ad_closed = try_close_ad(ui_content)
            if ad_closed:
                time.sleep(0.3)
                ui_content = get_screen_text()
                ok = verify_context_with_ai(EXPECTED_CONTEXT)
                if not ok.get("is_expected"):
                    log("Nghi ngờ tap nhầm sau khi đóng quảng cáo, đang khôi phục...", level="warning")
                    if ok.get("recovery_action") == "back":
                        adb_key_back()
                    time.sleep(1)
                    ui_content = get_screen_text()

            if not ui_content.strip() or len(ui_content.strip()) < MIN_UI_DUMP_LEN:
                log("⚠️ Accessibility Tree trống/quá ngắn (có thể dịch vụ accessibility đang tắt) - bỏ qua chu kỳ này.", level="warning")
                last_submitted_content = None
                time.sleep(FAST_CHECK_INTERVAL_SEC)
                continue

            try:
                result = _ai_result_queue.get_nowait()
            except queue.Empty:
                result = None

            if result is not None:
                if not result.get("ok"):
                    log(f"Bỏ qua kết quả AI lỗi từ luồng nền: {result.get('error')}", level="warning")
                else:
                    current_fp = hash_ui_content(ui_content)
                    if result["fingerprint"] != current_fp:
                        log("Màn hình đã đổi trong lúc chờ AI trả lời -> huỷ hành động cũ, không tap theo tọa độ lỗi thời.", level="warning")
                    else:
                        decision = result["decision"]
                        if decision.get("action") == "reply":
                            if not rate_limiter.allow():
                                wait_sec = rate_limiter.seconds_until_next_slot()
                                log(f"⏳ Đã đạt giới hạn {MAX_ACTIONS_PER_HOUR} hành động/giờ, bỏ qua phản hồi này (chờ thêm {wait_sec:.0f}s cho lần sau).", level="warning")
                            else:
                                text_to_send = decision.get("text", "")
                                x, y = decision.get("x"), decision.get("y")
                                if x is None or y is None:
                                    log("AI không cung cấp tọa độ hợp lệ, bỏ qua hành động.", level="warning")
                                else:
                                    if STEALTH_MODE and is_emulator and EMULATOR_BEHAVIOR_ENABLED:
                                        if EMULATOR_SHAKE_ENABLED:
                                            simulate_shake()
                                        time.sleep(random.uniform(0.2, 0.6))

                                    success = safe_tap_with_recovery(x, y, EXPECTED_CONTEXT)
                                    if success:
                                        adb_type_text(text_to_send, human_like=(STEALTH_MODE and STEALTH_HUMAN_TYPING))
                                        time.sleep(0.5)
                                        adb_key_enter()
                                        log(f"✅ Đã tự động trả lời: {text_to_send}")
                                        record_goal_completed(f"Trả lời tin nhắn: {text_to_send}", meta={"account": ACCOUNT_EMAIL})
                                        consecutive_uncertain = 0
                                    else:
                                        consecutive_uncertain += 1
                        else:
                            consecutive_uncertain = 0

                        if consecutive_uncertain >= MAX_CONSECUTIVE_UNCERTAIN:
                            log("🛑 Quá nhiều lần bất thường liên tiếp. Dừng agent để kiểm tra thủ công.", level="error")
                            save_incident_snapshot("stopped_too_many_errors")
                            break

            diff_ratio = 1.0 if last_submitted_content is None else compute_text_diff_ratio(last_submitted_content, ui_content)

            if diff_ratio >= TEXT_DIFF_THRESHOLD:
                if submit_ai_decision_async(ui_content):
                    last_submitted_content = ui_content
                    log(f"📤 Nội dung màn hình đổi (diff={diff_ratio:.3f}) - đã gửi yêu cầu AI chạy nền.")
                else:
                    log("AI đang bận xử lý yêu cầu trước đó, chưa gửi yêu cầu mới (tránh xếp chồng).")

        except Exception as e:
            log(f"❌ Có lỗi xảy ra: {e}", level="error")
            save_incident_snapshot("exception")

        time.sleep(FAST_CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()