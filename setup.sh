#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# setup.sh — Thiết lập môi trường Termux cho hệ thống Agent
# ============================================================
# Chức năng:
#   1. Cấp quyền cho Termux truy cập bộ nhớ ngoài (Download, DCIM, ...)
#   2. Cài các gói cần thiết (idempotent: gói nào có rồi thì bỏ qua)
#   3. Hướng dẫn/pair ADB qua Wireless Debugging (không cần root)
#   4. Bật wake-lock để tránh Android giết tiến trình khi màn hình tắt
#   5. (Tuỳ chọn) Lên lịch chạy agent.py tự động mỗi ngày
#
# LƯU Ý QUAN TRỌNG (đã xác minh lại lần 2, sửa từ bản trước):
# Termux (Android) dùng thư viện C "Bionic", KHÔNG phải "glibc" như Linux thường.
# PyPI không có sẵn bản build (wheel) cho Termux, nên "pip install numpy" hay
# "pip install opencv-python-headless" sẽ luôn cố BIÊN DỊCH TỪ MÃ NGUỒN — và
# luôn thất bại (thiếu header như spawn.h) hoặc cực kỳ nặng/lâu nếu có chạy được.
# ĐÂY LÀ GIỚI HẠN NỀN TẢNG, không phải do máy yếu.
#
# Cách đúng: dùng gói build-sẵn CHÍNH THỨC của kho Termux — cài trong vài
# giây, không biên dịch gì cả:
#   pkg install opencv python-numpy python-pillow
# Gói tên "python-opencv-python" KHÔNG TỒN TẠI trong kho Termux (đã kiểm tra
# lại, đây là lỗi ở bản hướng dẫn trước — xin lỗi vì đưa nhầm tên gói).
# Tên đúng, duy nhất, chính thức là "opencv" — gói này đã tích hợp sẵn binding
# cho Python 3 (import cv2 dùng được ngay), không cần cài thêm gói "python-*"
# nào khác cho phần OpenCV.
# ============================================================

set -e

echo "======================================================"
echo " BƯỚC 1: Cập nhật gói và cấp quyền lưu trữ"
echo "======================================================"

pkg update -y

# Cấp quyền truy cập bộ nhớ ngoài (Download, Pictures, ...)
# Lệnh này sẽ hiện popup xin quyền trên điện thoại — bấm "Allow"
termux-setup-storage
echo "-> Nếu có popup xin quyền vừa hiện ra, hãy bấm 'Cho phép' (Allow)."
echo "-> Sau khi cấp quyền, thư mục ~/storage/downloads sẽ trỏ tới Download thật của máy."
sleep 2

echo ""
echo "======================================================"
echo " BƯỚC 2: Cài đặt các gói cần thiết (bỏ qua gói đã có)"
echo "======================================================"

# Danh sách gói hệ thống (qua pkg). numpy/pillow/opencv dùng bản BUILD SẴN
# CHÍNH THỨC của kho Termux (KHÔNG qua pip) — xem ghi chú đầu file: pip sẽ
# luôn cố biên dịch từ mã nguồn trên Termux và thất bại, đây là giới hạn
# nền tảng. Gói "opencv" của Termux đã tích hợp sẵn cv2 cho Python 3.
PKGS=(
  python
  clang
  cmake
  make
  git
  curl
  termux-api
  sqlite
  android-tools
  tesseract
  cronie
  python-numpy
  python-pillow
  opencv
)

for pkg_name in "${PKGS[@]}"; do
    if dpkg -s "$pkg_name" >/dev/null 2>&1; then
        echo "[OK] $pkg_name"
    else
        echo "[INSTALL] $pkg_name"
        pkg install -y "$pkg_name"
    fi
done

echo ""
echo "-> Cài/nâng cấp thư viện Python (pip) — CHỈ những gói pure-Python,"
echo "   không cần biên dịch (requests, pybind11). numpy/pillow/opencv đã"
echo "   cài ở trên qua pkg, KHÔNG cài lại qua pip để tránh 2 bản đá nhau."
python -m pip install --upgrade --no-cache-dir \
    pybind11 \
    requests

echo ""
echo "======================================================"
echo " BƯỚC 2b: Cấu hình API key (Gemini/Qwen)"
echo "======================================================"
echo "Key sẽ được lưu vào $HOME/config.json — agent.py tự đọc file này,"
echo "bạn KHÔNG cần sửa trực tiếp vào code."
read -p "Nhập Gemini API key (để trống nếu không dùng Gemini): " INPUT_GEMINI_KEY
read -p "Nhập Qwen API key (để trống nếu không dùng Qwen): " INPUT_QWEN_KEY

python3 - "$INPUT_GEMINI_KEY" "$INPUT_QWEN_KEY" "$HOME/config.json" <<'PYEOF'
import json, sys, os

gemini_key, qwen_key, path = sys.argv[1], sys.argv[2], sys.argv[3]

config = {}
if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        config = {}

if gemini_key:
    config["GEMINI_API_KEYS"] = [gemini_key]
if qwen_key:
    config["QWEN_API_KEYS"] = [qwen_key]

with open(path, "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)

print(f"-> Đã ghi {path}")
PYEOF

echo ""
echo "======================================================"
echo " BƯỚC 3: Kết nối ADB qua Wireless Debugging"
echo "======================================================"
echo "Yêu cầu: Android 11+"
echo ""
echo "1. Vào Cài đặt > Giới thiệu điện thoại > bấm 7 lần vào 'Số bản dựng'"
echo "   để mở Tùy chọn nhà phát triển."
echo "2. Vào Cài đặt > Hệ thống > Tùy chọn nhà phát triển > bật"
echo "   'Gỡ lỗi qua mạng không dây' (Wireless debugging)."
echo "3. Bấm 'Ghép nối thiết bị bằng mã ghép nối' -> ghi lại IP:PORT và mã 6 số."
echo ""
read -p "Nhập IP:PORT để pair (ví dụ 192.168.1.5:39251), để trống nếu đã pair rồi: " PAIR_ADDR

if [ -n "$PAIR_ADDR" ]; then
    adb pair "$PAIR_ADDR"
fi

echo ""
read -p "Nhập IP:PORT để kết nối chính (ở màn hình Wireless debugging chính): " CONNECT_ADDR

if [ -n "$CONNECT_ADDR" ]; then
    adb connect "$CONNECT_ADDR"
fi

echo ""
adb devices
echo "-> Nếu thấy thiết bị hiện ra (không phải 'unauthorized'/'offline') là đã kết nối thành công."

echo ""
echo "======================================================"
echo " BƯỚC 4: Giữ Termux chạy nền ổn định"
echo "======================================================"
termux-wake-lock
echo "-> Đã bật wake-lock. Ngoài ra, vào Cài đặt > Ứng dụng > Termux > Pin"
echo "   và chọn 'Không giới hạn' (Unrestricted) để tránh Android tự tắt tiến trình."

echo ""
echo "======================================================"
echo " BƯỚC 5 (tùy chọn): Lên lịch chạy tự động 8h sáng"
echo "======================================================"
read -p "Bạn có muốn thiết lập cron chạy agent.py lúc 8h sáng mỗi ngày không? (y/n): " SETUP_CRON

if [ "$SETUP_CRON" = "y" ]; then
    CRON_LINE="0 8 * * * python $HOME/agent.py >> $HOME/agent_log.txt 2>&1"
    (crontab -l 2>/dev/null | grep -v "agent.py"; echo "$CRON_LINE") | crontab -
    crond
    echo "-> Đã thiết lập cron. Agent sẽ tự chạy lúc 8:00 sáng hàng ngày."
    echo "-> Log được ghi vào: $HOME/agent_log.txt"
fi

echo ""
echo "======================================================"
echo " HOÀN TẤT THIẾT LẬP"
echo "======================================================"
echo "Đặt file agent.py vào: $HOME/agent.py"
echo "API key đã lưu ở: $HOME/config.json (agent.py tự đọc, không cần sửa code)"
echo "Chạy thử thủ công bằng: python $HOME/agent.py"
echo ""
echo "LƯU Ý QUAN TRỌNG:"
echo "- Nếu máy có khóa màn hình (PIN/mẫu hình/vân tay), agent sẽ KHÔNG thể"
echo "  tương tác với app bên dưới khi màn hình khóa. Cân nhắc tắt khóa bảo mật"
echo "  trên thiết bị chuyên dụng cho việc này, hoặc chỉ dùng swipe-to-unlock."
echo "- Cổng kết nối ADB (CONNECT_ADDR) có thể đổi mỗi khi tắt/bật lại"
echo "  Wireless Debugging hoặc khởi động lại máy -> cần connect lại bằng tay"
echo "  hoặc viết thêm script tự dò cổng."
echo "- Máy RAM yếu: agent.py đã ưu tiên Accessibility Tree (đọc text UI, gần"
echo "  như miễn phí CPU/RAM), OpenCV chỉ chạy khi thật sự cần (phương án cuối)."
echo "- numpy/pillow/opencv dùng bản build sẵn của Termux (pkg), KHÔNG qua pip"
echo "  -> không tốn thời gian/CPU biên dịch, không lỗi 'spawn.h not found'."
echo "- Gói 'opencv' kéo theo vài thư viện phụ (ffmpeg, gstreamer...), tổng dung"
echo "  lượng cài đặt ~100-200MB Ổ ĐĨA (không phải RAM chạy) — bình thường,"
echo "  không ảnh hưởng tới giới hạn RAM 900MB lúc agent.py chạy."
