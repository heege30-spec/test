#!/usr/bin/env python3
# ============================================================
# agent.py — Hệ thống Agent tự động đọc & phản hồi tin nhắn
# ============================================================
# Kiến trúc:
#   Lớp 1 (nhanh, local, OpenCV):  phát hiện thay đổi màn hình,
#                                  tìm & xử lý nút đóng quảng cáo
#   Lớp 2 (chậm hơn, gọi API):     hiểu ngữ nghĩa tin nhắn,
#                                  ra quyết định hành động,
#                                  xác minh/khôi phục khi nghi ngờ
#
# YÊU CẦU TRƯỚC KHI CHẠY:
#   - Đã chạy xong setup.sh (cài gói + kết nối ADB)
#   - Điền API key ở phần CONFIG bên dưới
#   - Chuẩn bị sẵn các ảnh mẫu (template) nút đóng quảng cáo,
#     đặt trong thư mục ./templates/
#
# TÍNH NĂNG THÊM:
#   - Chỉ hoạt động trong khung giờ 8:00 - 22:30, ngoài giờ đó tự
#     chuyển sang chế độ chờ (sleep) tới phiên làm việc kế tiếp,
#     kể cả khi công việc trong ngày chưa xử lý xong.
#   - Giới hạn số hành động/giờ (MAX_ACTIONS_PER_HOUR) để tần suất
#     thao tác hợp lý, không dồn dập — KHÔNG dùng yếu tố ngẫu nhiên
#     để né tránh hệ thống phát hiện của bất kỳ nền tảng nào.
#   - goals.json: lưu lại các việc đã hoàn thành (có timestamp).
#   - skills.json: lưu cấu hình riêng cho từng app/trang bạn tự
#     thiết lập (selector, tọa độ, ghi chú) — chỉnh sửa bằng tay
#     hoặc qua các hàm get_skill_for_target()/upsert_skill_for_target().
#   - GEMINI_API_KEYS / QWEN_API_KEYS: có thể khai báo nhiều key cho cả
#     2 nhà cung cấp. Agent tự xoay vòng key trong mỗi provider, và khi
#     TOÀN BỘ key của provider ưu tiên cao (mặc định: Gemini) đều bị
#     giới hạn hạn mức (429/quota), tự động rơi sang provider tiếp theo
#     (Qwen) theo PROVIDER_PRIORITY — thay vì bị chặn hoàn toàn.
#     Cần cài thêm: pip install requests (đã có trong setup.sh)
#   - Stealth mode: chế độ hoạt động thận trọng hơn (ít hành động hơn,
#     chu kỳ quét dài hơn, ngưỡng nhận diện chặt hơn, mức log thấp hơn).
# ============================================================

import time
import json
import os
import subprocess
import logging
from datetime import datetime, timedelta

import cv2
import numpy as np

# ============================================================
# CONFIG — chỉnh sửa các giá trị này cho phù hợp
# ============================================================

GEMINI_API_KEYS = [
    "DIEN_API_KEY_GEMINI_1_VAO_DAY",
    "DIEN_API_KEY_GEMINI_2_VAO_DAY",
    # Thêm bao nhiêu key tùy nhu cầu, để trống list nếu không dùng Gemini.
]
GEMINI_MODEL = "gemini-2.5-flash"

QWEN_API_KEYS = [
    "DIEN_API_KEY_QWEN_1_VAO_DAY",
    # Thêm bao nhiêu key tùy nhu cầu, để trống list nếu không dùng Qwen.
]
QWEN_MODEL = "qwen-plus"
# Endpoint tương thích OpenAI của DashScope (Qwen). Nếu tài khoản bạn đăng ký
# ở khu vực Trung Quốc đại lục, có thể cần đổi sang endpoint nội địa tương ứng.
QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"

# Thứ tự ưu tiên nhà cung cấp: thử hết key của provider này rồi mới rơi
# sang provider tiếp theo khi tất cả key hiện tại đều bị giới hạn hạn mức.
PROVIDER_PRIORITY = ["gemini", "qwen"]

# Chỉ để tổ chức/nhận diện cấu hình — KHÔNG phải cơ chế bảo mật hay
# giảm rủi ro bị khóa tài khoản. Rủi ro bị khóa phụ thuộc vào hành vi
# thao tác (tần suất, phạm vi), không phụ thuộc dòng kiểm tra này.
ACCOUNT_EMAIL = "heege30@gmail.com"

POLL_INTERVAL_SEC = 8            # chu kỳ quét chính (giây)
FAST_CHECK_INTERVAL_SEC = 1      # chu kỳ quét nhanh bằng OpenCV (giây)

# --- Khung giờ hoạt động ---
ACTIVE_START_HOUR = 8            # 8:00 sáng
ACTIVE_END_HOUR = 22             # 22:30 tối
ACTIVE_END_MINUTE = 30

# --- Giới hạn tần suất hành động (thay cho việc "giả ngẫu nhiên") ---
MAX_ACTIONS_PER_HOUR = 30

SCREENSHOT_TMP = "/sdcard/agent_screen.png"
SCREENSHOT_COMPRESSED = "/sdcard/agent_screen_compressed.jpg"
UI_DUMP_PATH = "/sdcard/window_dump.xml"

TEMPLATE_DIR = "./templates"       # chứa các ảnh mẫu nút đóng quảng cáo
LOG_DIR = "./logs"                 # nơi lưu log + ảnh khi có sự kiện bất thường
GOALS_FILE = "./goals.json"        # lưu các mục tiêu/việc đã hoàn thành
SKILLS_FILE = "./skills.json"      # lưu cấu hình riêng theo từng loại app/trang

AD_MATCH_THRESHOLD = 0.92          # ngưỡng tin cậy cao để tránh tap nhầm
FRAME_DIFF_THRESHOLD = 0.03        # % pixel khác nhau để coi là "có thay đổi"
MAX_CONSECUTIVE_UNCERTAIN = 3      # số lần nghi ngờ liên tiếp trước khi dừng hẳn

EXPECTED_CONTEXT = "Màn hình chat (Zalo/Telegram), hiển thị danh sách hoặc nội dung tin nhắn"

# ============================================================
# STEALTH MODE CONFIGURATION
# - Không dùng randomness để né detection. Stealth mode là giảm tần suất
#   và thận trọng hơn với các ngưỡng cảm biến / logging.
# - Bật/tắt runtime bằng file ./stealth.enabled hoặc mặc định bằng STEALTH_MODE_DEFAULT.
# ============================================================

STEALTH_MODE_DEFAULT = False  # đặt True nếu muốn khởi động luôn ở stealth
STEALTH_TOGGLE_FILE = "./stealth.enabled"

STEALTH_CONFIG = {
    "max_actions_per_hour": 10,        # ít hành động hơn
    "poll_interval_sec": 20,          # quét chậm hơn
    "ad_match_threshold": 0.98,       # chỉ tap nếu chắc chắn hơn
    "frame_diff_threshold": 0.06,     # yêu cầu thay đổi lớn hơn mới gọi AI
    "active_start_hour": 9,           # (tuỳ chọn) hạn chế giờ hoạt động
    "active_end_hour": 21,
    "active_end_minute": 0,
    "logging_level": logging.WARNING, # ghi file ít hơn, chỉ warn+error
}

# ============================================================
# tạo thư mục + tệp mặc định
# ============================================================

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(TEMPLATE_DIR, exist_ok=True)

if not os.path.exists(GOALS_FILE):
    with open(GOALS_FILE, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)

if not os.path.exists(SKILLS_FILE):
    with open(SKILLS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False, indent=2)

# Backup originals so we can restore when stealth is turned off
_ORIGINALS = {
    "POLL_INTERVAL_SEC": POLL_INTERVAL_SEC,
    "FAST_CHECK_INTERVAL_SEC": FAST_CHECK_INTERVAL_SEC,
    "MAX_ACTIONS_PER_HOUR": MAX_ACTIONS_PER_HOUR,
    "AD_MATCH_THRESHOLD": AD_MATCH_THRESHOLD,
    "FRAME_DIFF_THRESHOLD": FRAME_DIFF_THRESHOLD,
    "ACTIVE_START_HOUR": ACTIVE_START_HOUR,
    "ACTIVE_END_HOUR": ACTIVE_END_HOUR,
    "ACTIVE_END_MINUTE": ACTIVE_END_MINUTE,
    "LOGGING_LEVEL": logging.INFO,
}

logging.basicConfig(
    filename=os.path.join(LOG_DIR, "agent.log"),
    level=_ORIGINALS["LOGGING_LEVEL"],
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)


# ============================================================
# KHUNG GIỜ HOẠT ĐỘNG + GIỚI HẠN TẦN SUẤT
# ============================================================

def is_within_active_hours(now: datetime = None) -> bool:
    now = now or datetime.now()
    start = now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0)
    end = now.replace(hour=ACTIVE_END_HOUR, minute=ACTIVE_END_MINUTE, second=0, microsecond=0)
    return start <= now <= end


def seconds_until_next_active_window(now: datetime = None) -> float:
    """Tính số giây cần chờ tới 8:00 sáng của ngày tiếp theo (hoặc hôm nay nếu chưa tới giờ)."""
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
    # Giải phóng wake-lock trong lúc chờ để đỡ tốn pin, chỉ giữ khi thực sự hoạt động
    subprocess.run("termux-wake-unlock", shell=True)
    time.sleep(wait_sec)
    subprocess.run("termux-wake-lock", shell=True)
    log("☀️ Đã tới khung giờ hoạt động, agent tiếp tục làm việc.")


class RateLimiter:
    """Giới hạn số hành động (tap/gõ) trong 1 giờ để tần suất hoạt động hợp lý,
    không dồn dập bất thường."""

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
# QUẢN LÝ NHIỀU API KEY, NHIỀU NHÀ CUNG CẤP
# (xoay vòng + tự chuyển provider khi hết hạn mức)
# ============================================================

class ApiKeyPool:
    """Quản lý key của nhiều nhà cung cấp (Gemini, Qwen, ...) theo thứ tự
    ưu tiên. Trong mỗi provider, xoay vòng (round-robin) qua các key còn
    dùng được; khi TẤT CẢ key của provider ưu tiên cao đều bị giới hạn
    hạn mức, tự động rơi xuống provider tiếp theo trong PROVIDER_PRIORITY."""

    def __init__(self, provider_keys: dict, priority: list, cooldown_sec: int = 60):
        # provider_keys: {"gemini": [...], "qwen": [...]}
        self.cooldown_sec = cooldown_sec
        self.entries = []  # list of (provider, key), theo đúng thứ tự ưu tiên
        self.cooldown_until = {}
        self.round_robin_index = {}

        for provider in priority:
            keys = [k for k in provider_keys.get(provider, [])
                    if k and "DIEN_API_KEY" not in k]
            for k in keys:
                self.entries.append((provider, k))
                self.cooldown_until[(provider, k)] = 0.0
            self.round_robin_index[provider] = 0

        if not self.entries:
            raise ValueError(
                "Chưa cấu hình API key hợp lệ nào (Gemini hoặc Qwen) trong CONFIG."
            )
        self.priority = priority

    def _available_for_provider(self, provider: str):
        now = time.time()
        return [(p, k) for (p, k) in self.entries
                if p == provider and self.cooldown_until[(p, k)] <= now]

    def get_next_key(self):
        """Trả về (provider, key) khả dụng tiếp theo, ưu tiên theo PROVIDER_PRIORITY."""
        for provider in self.priority:
            available = self._available_for_provider(provider)
            if available:
                idx = self.round_robin_index[provider] % len(available)
                self.round_robin_index[provider] += 1
                return available[idx]

        # Tất cả provider/key đều đang cooldown -> chờ cái hết sớm nhất
        soonest = min(self.entries, key=lambda pk: self.cooldown_until[pk])
        wait = max(0, self.cooldown_until[soonest] - time.time())
        log(f"⏳ Toàn bộ API key (mọi nhà cung cấp) đều đang tạm nghỉ, "
            f"chờ {wait:.0f}s trước khi thử lại.", level="warning")
        time.sleep(wait)
        return soonest

    def mark_rate_limited(self, provider: str, key: str):
        self.cooldown_until[(provider, key)] = time.time() + self.cooldown_sec
        log(f"⚠️ Key {provider} ...{key[-6:]} bị giới hạn hạn mức, "
            f"tạm nghỉ {self.cooldown_sec}s.", level="warning")

    def key_count(self) -> int:
        return len(self.entries)

    def summary(self) -> str:
        counts = {}
        for p, _ in self.entries:
            counts[p] = counts.get(p, 0) + 1
        return ", ".join(f"{p}: {n} key" for p, n in counts.items())


api_key_pool = ApiKeyPool(
    provider_keys={"gemini": GEMINI_API_KEYS, "qwen": QWEN_API_KEYS},
    priority=PROVIDER_PRIORITY,
    cooldown_sec=60,
)


# ============================================================
# LƯU MỤC TIÊU & KỸ NĂNG THEO TỪNG LOẠI TRANG/APP
# ============================================================

def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_goal_completed(description: str, meta: dict = None):
    """Ghi lại một việc đã hoàn thành vào goals.json."""
    goals = load_json(GOALS_FILE)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "description": description,
        "meta": meta or {},
    }
    goals.append(entry)
    save_json(GOALS_FILE, goals)
    log(f"📌 Đã ghi mục tiêu hoàn thành: {description}")


def get_skill_for_target(target_key: str) -> dict:
    """Đọc cấu hình riêng (selector/tọa độ/ghi chú) cho một app/trang cụ thể
    mà bạn đã tự thiết lập trước, ví dụ 'zalo', 'telegram_web'."""
    skills = load_json(SKILLS_FILE)
    return skills.get(target_key, {})


def upsert_skill_for_target(target_key: str, config: dict):
    skills = load_json(SKILLS_FILE)
    skills[target_key] = config
    save_json(SKILLS_FILE, skills)
    log(f"🛠️ Đã cập nhật cấu hình cho: {target_key}")


# ============================================================
# LỚP TIỆN ÍCH: điều khiển ADB
# ============================================================

def run_adb_command(command: str) -> str:
    """Chạy một lệnh adb shell, trả về stdout."""
    result = subprocess.run(
        f"adb shell {command}", shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"Lệnh adb lỗi: {command} -> {result.stderr}", level="warning")
    return result.stdout


def adb_tap(x: int, y: int):
    run_adb_command(f"input tap {x} {y}")


def adb_type_text(text: str):
    safe_text = text.replace(" ", "%s")
    run_adb_command(f"input text {safe_text}")


def adb_key_back():
    run_adb_command("input keyevent 4")


def adb_key_enter():
    run_adb_command("input keyevent 66")


def adb_pull_screenshot(local_path: str = SCREENSHOT_TMP):
    """Chụp màn hình trên máy và kéo file về (dùng qua adb, không cần lệnh local riêng)."""
    subprocess.run(f"adb shell screencap -p {local_path}", shell=True)
    # Với thiết lập wireless debugging cục bộ, file đã nằm ngay trên máy (/sdcard),
    # nên có thể đọc trực tiếp bằng OpenCV mà không cần "adb pull" thêm.
    return local_path


def get_screen_text() -> str:
    """Lấy nội dung UI hiện tại dưới dạng XML (rẻ và chính xác hơn OCR)."""
    run_adb_command(f"uiautomator dump {UI_DUMP_PATH}")
    dump_result = subprocess.run(
        f"adb shell cat {UI_DUMP_PATH}", shell=True, capture_output=True, text=True
    )
    return dump_result.stdout


# ============================================================
# LỚP 1: XỬ LÝ ẢNH LOCAL BẰNG OPENCV (nhanh, không tốn API)
# ============================================================

def compute_diff_ratio(img1: np.ndarray, img2: np.ndarray) -> float:
    """Tính tỷ lệ pixel khác nhau giữa 2 ảnh xám cùng kích thước."""
    if img1.shape != img2.shape:
        return 1.0  # coi như khác hoàn toàn nếu kích thước lệch
    diff = cv2.absdiff(img1, img2)
    changed = np.count_nonzero(diff > 15)
    total = diff.size
    return changed / total


def compress_screenshot(input_path: str, output_path: str,
                         max_width: int = 800, jpeg_quality: int = 70) -> str:
    """Giảm kích thước ảnh trước khi gửi API để tiết kiệm chi phí."""
    img = cv2.imread(input_path)
    if img is None:
        raise FileNotFoundError(f"Không đọc được ảnh: {input_path}")

    h, w = img.shape[:2]
    if w > max_width:
        scale = max_width / w
        img = cv2.resize(img, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)

    cv2.imwrite(output_path, img, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    return output_path


def find_close_button(screenshot_gray: np.ndarray, threshold: float = AD_MATCH_THRESHOLD):
    """Quét thư viện template trong TEMPLATE_DIR để tìm nút đóng quảng cáo.
    Chỉ quét vùng góc trên (trái/phải) để giảm rủi ro tap nhầm nút thật của app."""
    h, w = screenshot_gray.shape[:2]
    corner_size = min(200, h // 4, w // 4)

    regions = {
        "top_left": (screenshot_gray[0:corner_size, 0:corner_size], (0, 0)),
        "top_right": (screenshot_gray[0:corner_size, w - corner_size:w], (w - corner_size, 0)),
    }

    if not os.path.isdir(TEMPLATE_DIR):
        return None

    for fname in os.listdir(TEMPLATE_DIR):
        if not fname.lower().endswith((".png", ".jpg", ".jpeg")):
            continue
        template = cv2.imread(os.path.join(TEMPLATE_DIR, fname), cv2.IMREAD_GRAYSCALE)
        if template is None:
            continue

        for region_name, (region_img, offset) in regions.items():
            if region_img.shape[0] < template.shape[0] or region_img.shape[1] < template.shape[1]:
                continue
            result = cv2.matchTemplate(region_img, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val >= threshold:
                th, tw = template.shape
                cx = offset[0] + max_loc[0] + tw // 2
                cy = offset[1] + max_loc[1] + th // 2
                log(f"Tìm thấy nút đóng quảng cáo (template={fname}, vùng={region_name}, độ khớp={max_val:.2f})")
                return (cx, cy)

    return None


def try_close_ad() -> bool:
    """Chụp màn hình, tìm và tap nút đóng quảng cáo nếu có. Trả về True nếu đã tap."""
    adb_pull_screenshot(SCREENSHOT_TMP)
    screen = cv2.imread(SCREENSHOT_TMP, cv2.IMREAD_GRAYSCALE)
    if screen is None:
        return False

    coords = find_close_button(screen)
    if coords:
        adb_tap(*coords)
        time.sleep(0.8)
        return True
    return False


# ============================================================
# LỚP 2: GỌI API AI ĐỂ RA QUYẾT ĐỊNH / XÁC MINH
# ============================================================

def _call_gemini_with_key(key: str, prompt: str) -> str:
    from google import genai
    client = genai.Client(api_key=key)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return response.text


def _call_qwen_with_key(key: str, prompt: str) -> str:
    """Gọi Qwen qua endpoint tương thích OpenAI của DashScope. Yêu cầu: pip install requests"""
    import requests

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(QWEN_BASE_URL, headers=headers, json=payload, timeout=30)

    if resp.status_code == 429:
        raise RuntimeError("429 rate_limit_exceeded")
    resp.raise_for_status()

    data = resp.json()
    return data["choices"][0]["message"]["content"]


def call_llm(prompt: str, max_key_retries: int = None) -> str:
    """Gọi mô hình AI (Gemini hoặc Qwen) để phân tích/ra quyết định.
    Tự động xoay vòng qua nhiều API key, ưu tiên theo PROVIDER_PRIORITY
    (mặc định: Gemini trước, hết hạn mức mới rơi sang Qwen)."""
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
            is_rate_limit = any(term in err_text for term in
                                 ["429", "rate limit", "quota", "resource_exhausted"])
            if is_rate_limit:
                api_key_pool.mark_rate_limited(provider, key)
                last_error = e
                continue
            # Lỗi khác (mạng, key sai định dạng, ...) -> không đáng để thử key khác
            raise

    raise RuntimeError(f"Tất cả API key (mọi nhà cung cấp) đều bị giới hạn hạn mức. "
                        f"Lỗi cuối: {last_error}")


def ask_ai_decision(ui_content: str) -> dict:
    """Hỏi AI xem có tin nhắn nào cần trả lời không."""
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
        return json.loads(raw.strip().strip("`").replace("json", "", 1) if raw.strip().startswith("```") else raw)
    except json.JSONDecodeError:
        log(f"Không parse được JSON từ AI: {raw}", level="warning")
        return {"action": "none"}


def verify_context_with_ai(expected_context: str) -> dict:
    """Xác minh màn hình hiện tại có đúng ngữ cảnh mong đợi không."""
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
        return json.loads(raw.strip().strip("`").replace("json", "", 1) if raw.strip().startswith("```") else raw)
    except json.JSONDecodeError:
        return {"is_expected": False, "recovery_action": "back", "reason": "parse_error"}


# ============================================================
# STEALTH MODE: áp dụng / khôi phục cấu hình
# ============================================================

def _set_logging_level(level):
    # Thay đổi level cho root logger và handlers hiện có
    logger = logging.getLogger()
    logger.setLevel(level)
    for h in logger.handlers:
        h.setLevel(level)


def apply_stealth_mode(enabled: bool):
    """Áp dụng hoặc khôi phục cấu hình khi chuyển stealth on/off."""
    global POLL_INTERVAL_SEC, FAST_CHECK_INTERVAL_SEC, MAX_ACTIONS_PER_HOUR
    global AD_MATCH_THRESHOLD, FRAME_DIFF_THRESHOLD
    global ACTIVE_START_HOUR, ACTIVE_END_HOUR, ACTIVE_END_MINUTE
    global rate_limiter

    if enabled:
        POLL_INTERVAL_SEC = STEALTH_CONFIG.get("poll_interval_sec", POLL_INTERVAL_SEC)
        # FAST_CHECK_INTERVAL_SEC giữ nguyên hoặc tăng nhẹ
        FAST_CHECK_INTERVAL_SEC = max(FAST_CHECK_INTERVAL_SEC, 2)
        MAX_ACTIONS_PER_HOUR = STEALTH_CONFIG.get("max_actions_per_hour", MAX_ACTIONS_PER_HOUR)
        AD_MATCH_THRESHOLD = STEALTH_CONFIG.get("ad_match_threshold", AD_MATCH_THRESHOLD)
        FRAME_DIFF_THRESHOLD = STEALTH_CONFIG.get("frame_diff_threshold", FRAME_DIFF_THRESHOLD)
        ACTIVE_START_HOUR = STEALTH_CONFIG.get("active_start_hour", ACTIVE_START_HOUR)
        ACTIVE_END_HOUR = STEALTH_CONFIG.get("active_end_hour", ACTIVE_END_HOUR)
        ACTIVE_END_MINUTE = STEALTH_CONFIG.get("active_end_minute", ACTIVE_END_MINUTE)
        _set_logging_level(STEALTH_CONFIG.get("logging_level", logging.WARNING))
        log("🔒 Stealth mode BẬT: hoạt động thận trọng hơn (ít hành động, quét chậm hơn).", level="warning")
    else:
        POLL_INTERVAL_SEC = _ORIGINALS["POLL_INTERVAL_SEC"]
        FAST_CHECK_INTERVAL_SEC = _ORIGINALS["FAST_CHECK_INTERVAL_SEC"]
        MAX_ACTIONS_PER_HOUR = _ORIGINALS["MAX_ACTIONS_PER_HOUR"]
        AD_MATCH_THRESHOLD = _ORIGINALS["AD_MATCH_THRESHOLD"]
        FRAME_DIFF_THRESHOLD = _ORIGINALS["FRAME_DIFF_THRESHOLD"]
        ACTIVE_START_HOUR = _ORIGINALS["ACTIVE_START_HOUR"]
        ACTIVE_END_HOUR = _ORIGINALS["ACTIVE_END_HOUR"]
        ACTIVE_END_MINUTE = _ORIGINALS["ACTIVE_END_MINUTE"]
        _set_logging_level(_ORIGINALS["LOGGING_LEVEL"])
        log("🔓 Stealth mode TẮT: trở về cấu hình mặc định.", level="info")

    # Khởi tạo lại rate_limiter với giới hạn mới
    rate_limiter = RateLimiter(MAX_ACTIONS_PER_HOUR)


# ============================================================
# CƠ CHẾ AN TOÀN: tap có xác minh + khôi phục
# ============================================================

def safe_tap_with_recovery(x: int, y: int, expected_context: str, max_retries: int = 2) -> bool:
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
    """Lưu lại ảnh + thời điểm khi có sự cố, để xem lại và tinh chỉnh sau."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(LOG_DIR, f"{tag}_{ts}.jpg")
    try:
        adb_pull_screenshot(SCREENSHOT_TMP)
        compress_screenshot(SCREENSHOT_TMP, dest, max_width=800, jpeg_quality=80)
        log(f"Đã lưu ảnh sự cố: {dest}")
    except Exception as e:
        log(f"Không lưu được ảnh sự cố: {e}", level="error")


# ============================================================
# VÒNG LẶP CHÍNH
# ============================================================

def main():
    # Áp dụng stealth mặc định nếu được bật qua config hoặc file
    current_stealth = False
    if STEALTH_MODE_DEFAULT or os.path.exists(STEALTH_TOGGLE_FILE):
        apply_stealth_mode(True)
        current_stealth = True
    else:
        apply_stealth_mode(False)
        current_stealth = False

    log(f"🤖 Agent bắt đầu chạy cho tài khoản cấu hình: {ACCOUNT_EMAIL} "
        f"({api_key_pool.summary()}) - stealth={'ON' if current_stealth else 'OFF'}")
    consecutive_uncertain = 0
    prev_gray_screen = None

    while True:
        try:
            # Kiểm tra if stealth file toggled at runtime
            stealth_flag = os.path.exists(STEALTH_TOGGLE_FILE)
            if stealth_flag != current_stealth:
                apply_stealth_mode(stealth_flag)
                current_stealth = stealth_flag

            # --- Kiểm tra khung giờ hoạt động (8:00 - 22:30) ---
            if not is_within_active_hours():
                sleep_until_next_active_window()
                prev_gray_screen = None  # màn hình có thể đã đổi trong lúc chờ, quét lại từ đầu
                continue

            # --- Kiểm tra giới hạn tần suất hành động/giờ ---
            if not rate_limiter.allow():
                wait_sec = rate_limiter.seconds_until_next_slot()
                log(f"⏳ Đã đạt giới hạn {MAX_ACTIONS_PER_HOUR} hành động/giờ, "
                    f"chờ {wait_sec:.0f}s trước khi tiếp tục.")
                time.sleep(min(wait_sec, POLL_INTERVAL_SEC * 10))
                continue

            # --- Lớp 1: quét nhanh, phát hiện thay đổi + dọn quảng cáo ---
            adb_pull_screenshot(SCREENSHOT_TMP)
            current_screen = cv2.imread(SCREENSHOT_TMP, cv2.IMREAD_GRAYSCALE)

            if current_screen is None:
                log("Không đọc được ảnh màn hình, bỏ qua chu kỳ này.", level="warning")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            # Thử đóng quảng cáo nếu phát hiện (ngưỡng tin cậy cao để tránh tap nhầm)
            ad_closed = try_close_ad()
            if ad_closed:
                # Xác minh lại bằng AI xem có bị điều hướng nhầm không
                ok = verify_context_with_ai(EXPECTED_CONTEXT)
                if not ok.get("is_expected"):
                    log("Nghi ngờ tap nhầm sau khi đóng quảng cáo, đang khôi phục...", level="warning")
                    if ok.get("recovery_action") == "back":
                        adb_key_back()
                    time.sleep(1)
                # chụp lại màn hình sau khi xử lý quảng cáo
                adb_pull_screenshot(SCREENSHOT_TMP)
                current_screen = cv2.imread(SCREENSHOT_TMP, cv2.IMREAD_GRAYSCALE)

            # So sánh với frame trước để quyết định có cần gọi AI không
            if prev_gray_screen is not None:
                diff_ratio = compute_diff_ratio(prev_gray_screen, current_screen)
                if diff_ratio < FRAME_DIFF_THRESHOLD:
                    log(f"Màn hình không đổi đáng kể (diff={diff_ratio:.3f}), bỏ qua gọi AI.")
                    prev_gray_screen = current_screen
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

            prev_gray_screen = current_screen

            # --- Lớp 2: gọi AI để hiểu ngữ nghĩa & ra quyết định ---
            ui_content = get_screen_text()
            decision = ask_ai_decision(ui_content)

            if decision.get("action") == "reply":
                text_to_send = decision.get("text", "")
                x, y = decision.get("x"), decision.get("y")

                if x is None or y is None:
                    log("AI không cung cấp tọa độ hợp lệ, bỏ qua hành động.", level="warning")
                    continue

                success = safe_tap_with_recovery(x, y, EXPECTED_CONTEXT)
                if success:
                    adb_type_text(text_to_send)
                    time.sleep(0.5)
                    adb_key_enter()
                    log(f"✅ Đã tự động trả lời: {text_to_send}")
                    record_goal_completed(
                        f"Trả lời tin nhắn: {text_to_send}",
                        meta={"account": ACCOUNT_EMAIL},
                    )
                    consecutive_uncertain = 0
                else:
                    consecutive_uncertain += 1
            else:
                consecutive_uncertain = 0

            if consecutive_uncertain >= MAX_CONSECUTIVE_UNCERTAIN:
                log("🛑 Quá nhiều lần bất thường liên tiếp. Dừng agent để kiểm tra thủ công.", level="error")
                save_incident_snapshot("stopped_too_many_errors")
                break

        except Exception as e:
            log(f"❌ Có lỗi xảy ra: {e}", level="error")
            save_incident_snapshot("exception")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()