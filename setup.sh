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
# LƯU Ý: máy cấu hình yếu (RAM <900MB) nên KHÔNG cài gói pkg "opencv"
# (bản C++ đầy đủ, nặng) — chỉ dùng "opencv-python-headless" qua pip
# (nhẹ hơn nhiều, không kèm GUI/Qt). numpy và pillow cũng chỉ cài qua
# pip (1 nguồn duy nhất) để tránh 2 bản numpy (pkg + pip) lệch version
# gây lỗi "import cv2" hoặc "ImportError: numpy.core.multiarray failed".
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

# Danh sách gói hệ thống (qua pkg). KHÔNG cài numpy/pillow/opencv ở đây —
# tất cả 3 thứ đó dùng CHUNG 1 nguồn duy nhất là pip (xem bên dưới), để
# tránh 2 bản numpy (pkg + pip) lệch phiên bản/ABI gây lỗi "import cv2".
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
echo "-> Cài/nâng cấp thư viện Python (pip) — nguồn DUY NHẤT cho numpy/pillow/opencv..."
python -m pip install --upgrade --no-cache-dir pip
python -m pip install --upgrade --no-cache-dir \
    numpy \
    pillow \
    opencv-python-headless \
    pybind11 \
    requests

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
