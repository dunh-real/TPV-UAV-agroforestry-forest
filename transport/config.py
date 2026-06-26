"""Cấu hình tầng transport — đọc từ env, có default chạy được ngay trên server test.

Tham chiếu: communication-transport-spec.md §2 (key/stream), §9 (bảng tham số).
"""
from __future__ import annotations

import os


def _load_dotenv() -> None:
    """Nạp `.env` ở gốc dự án (nếu có) → secrets thật KHÔNG nằm trong code/git.
    Biến set sẵn ở môi trường được ưu tiên (setdefault). Giá trị lấy nguyên dòng sau
    dấu `=` (không tách comment giữa dòng) để giữ password có ký tự đặc biệt.
    """
    root = os.path.join(os.path.dirname(__file__), os.pardir)
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)


_load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


# --- Kết nối ---------------------------------------------------------------
# Password thật đặt trong .env (REDIS_URL). Default chỉ là placeholder "changeme"
# (khớp default ${REDIS_PASSWORD:-changeme} trong docker-compose) — KHÔNG phải secret.
# Redis test ở host:6380, KHÔNG đụng redis-prod (6379).
REDIS_URL = os.getenv("REDIS_URL", "redis://:changeme@localhost:6380/0")

# --- Key / stream / consumer group (spec §2) -------------------------------
# Tên stream theo BE (dms:*). Group là nội bộ AI → giữ cg:ai/cg:be.
REQ_STREAM = os.getenv("REQ_STREAM", "dms:captures:ready")
RES_STREAM = os.getenv("RES_STREAM", "dms:ai:results")
CG_AI = os.getenv("CG_AI", "cg:ai")
CG_BE = os.getenv("CG_BE", "cg:be")

# --- Backpressure / batch (spec §9) ----------------------------------------
# MAXLEN ~ N : ring-buffer cho frames:req. N ≈ Σdrone × fps × giây_giữ.
REQ_MAXLEN = _int("REQ_MAXLEN", 2000)
# MAXLEN cho frames:res — XACK của BE KHÔNG xóa entry khỏi stream, chỉ MAXLEN/XTRIM mới
# xóa ⇒ thiếu trần thì frames:res phình vô hạn (entry nhẹ ~1-5KB nhưng tích lũy theo thời
# gian). Default 2× REQ_MAXLEN: dư đệm cho BE bắt kịp lúc burst/restart, ~8MB ở 4000 entry.
RES_MAXLEN = _int("RES_MAXLEN", 2 * REQ_MAXLEN)
# COUNT: số entry tối đa mỗi XREADGROUP (micro-batch).
READ_COUNT = _int("READ_COUNT", 16)
# BLOCK ms: ngân sách gom batch, latency-first.
READ_BLOCK_MS = _int("READ_BLOCK_MS", 20)
# XAUTOCLAIM: coi worker chết sau ngần này ms không ack.
CLAIM_MIN_IDLE_MS = _int("CLAIM_MIN_IDLE_MS", 30000)

# --- Độ tin cậy: idempotency + dead-letter (spec §8, luồng AI) --------------
# TTL key done:{captureId} — chặn emit result trùng khi frame bị redeliver.
DONE_TTL_S = _int("DONE_TTL_S", 120)
# Frame giao (delivery) quá số lần này mà chưa ack → đẩy sang dead-letter, thôi retry.
MAX_DELIVERIES = _int("MAX_DELIVERIES", 5)
DLQ_STREAM = os.getenv("DLQ_STREAM", "dms:ai:dead-letter")

# --- MinIO / S3 (ảnh annotated durable, architecture §4/§8) -----------------
# Endpoint + creds THẬT đặt trong .env (production: MinIO remote, vd s3.blogscloud.click).
# Default dưới đây chỉ là placeholder cho dev local (KHÔNG phải secret).
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "admin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "changeme")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "images")
MINIO_SECURE = os.getenv("MINIO_SECURE", "0") == "1"  # UseSSL
# Lifecycle TẦNG A (events/): tự xóa ảnh sự kiện cũ hơn N ngày. 0 = không đặt TTL.
MINIO_TTL_DAYS = _int("MINIO_TTL_DAYS", 7)
# Trần số ảnh đang chờ upload giữ trong RAM (mỗi worker). MinIO chậm/sập → quá trần thì
# BỎ ảnh đó (log) thay vì phình RAM. 4096 × ~150KB ≈ 600MB/worker — server 251GB dư thừa,
# đủ đệm khi MinIO trục trặc lâu mà không mất ảnh sự kiện. Hạ xuống nếu chạy nhiều worker.
MINIO_MAX_PENDING = _int("MINIO_MAX_PENDING", 4096)
# Bật/tắt vẽ box + upload ảnh annotated (chỉ khi có detection).
ANNOTATE = os.getenv("ANNOTATE", "1") == "1"

# --- Event gate + dedup lưu ảnh (architecture §6.5, §11) -------------------
# Chỉ LƯU ảnh khi có detection thuộc lớp sự kiện + đủ conf, và dedup theo cửa sổ
# thời gian: mỗi (drone, cls) chỉ lưu 1 ảnh / DEDUP_WINDOW_S giây. Metadata vẫn ra
# MỌI frame trên frames:res (không đổi) — chỉ ẢNH + alert mới bị lọc.
# EVENT_CLASSES rỗng = mọi lớp tính là sự kiện (giờ đang test COCO; sau siết lại
# vd "fire,smoke" cho viễn thám).
EVENT_CLASSES = set(filter(None, (c.strip() for c in os.getenv("EVENT_CLASSES", "").split(","))))
EVENT_CONF = float(os.getenv("EVENT_CONF", "0.5"))
DEDUP_WINDOW_S = _int("DEDUP_WINDOW_S", 5)

# --- Lưu ảnh phân tầng A/B/C (chống mất bằng chứng do false-negative) --------
# Input thực tế ~7 ảnh/s/drone × ~4 drone = ~28 fps. Lưu full mọi frame quá đắt (~hàng trăm
# GB/ngày) nên phân tầng theo GIÁ TRỊ: A đầy đủ/lâu, B vùng nghi ngờ, C hộp đen rẻ.
#
#   Tầng A (events/)    : frame CÓ sự kiện (đạt EVENT_CONF). Full-res annotated, dedup
#                         best-in-window, TTL MINIO_TTL_DAYS. (đã có sẵn)
#   Tầng B (uncertain/) : frame CÓ detection thô (≥CONF) nhưng KHÔNG đạt sự kiện — ứng viên
#                         false-negative cao nhất (model "suýt" thấy). Annotated chất lượng vừa.
#   Tầng C (blackbox/)  : MỌI frame kể cả 0-detection. Thumbnail, lấy mẫu thời gian → lưới an
#                         toàn khi model miss hẳn + corpus để chạy lại model mới sau này.
SAVE_UNCERTAIN = os.getenv("SAVE_UNCERTAIN", "1") == "1"
UNCERTAIN_JPEG_Q = _int("UNCERTAIN_JPEG_Q", 70)
UNCERTAIN_TTL_DAYS = _int("UNCERTAIN_TTL_DAYS", 2)

SAVE_BLACKBOX = os.getenv("SAVE_BLACKBOX", "1") == "1"
# Lấy mẫu: giữ tối đa 1 thumbnail / N giây / drone (đa worker an toàn qua SET NX). 1 = 1fps/drone.
BLACKBOX_SAMPLE_S = _int("BLACKBOX_SAMPLE_S", 1)
BLACKBOX_IMGSZ = _int("BLACKBOX_IMGSZ", 640)   # cạnh dài thumbnail (0 = giữ nguyên)
BLACKBOX_JPEG_Q = _int("BLACKBOX_JPEG_Q", 60)
BLACKBOX_TTL_DAYS = _int("BLACKBOX_TTL_DAYS", 1)

# --- Model -----------------------------------------------------------------
MODEL_PATH = os.getenv("MODEL_PATH", "weights/yolo26x.pt")
# Đưa vào output để BE truy vết model nào sinh kết quả (architecture: model + version).
MODEL_NAME = os.getenv("MODEL_NAME", os.path.splitext(os.path.basename(MODEL_PATH))[0])
MODEL_VERSION = os.getenv("MODEL_VERSION", "coco-pretrained")  # đổi khi fine-tune nhãn viễn thám
DEVICE = os.getenv("DEVICE", "0")  # GPU index hoặc "cpu"
CONF = float(os.getenv("CONF", "0.25"))
IMGSZ = _int("IMGSZ", 960)
HALF = os.getenv("HALF", "1") == "1"  # FP16 (docs §2: Blackwell, cứ FP16 thoải mái)
