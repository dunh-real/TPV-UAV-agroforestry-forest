"""Cấu hình tầng transport — đọc từ env, có default chạy được ngay trên server test.

Tham chiếu: communication-transport-spec.md §2 (key/stream), §9 (bảng tham số).
"""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


# --- Kết nối ---------------------------------------------------------------
# Mặc định trỏ vào Redis test riêng (container rs-redis-test, host:6380),
# KHÔNG đụng redis-prod (6379, có auth).
REDIS_URL = os.getenv("REDIS_URL", "redis://:admin123456@localhost:6380/0")

# --- Key / stream / consumer group (spec §2) -------------------------------
REQ_STREAM = os.getenv("REQ_STREAM", "frames:req")
RES_STREAM = os.getenv("RES_STREAM", "frames:res")
CG_AI = os.getenv("CG_AI", "cg:ai")
CG_BE = os.getenv("CG_BE", "cg:be")

# --- Backpressure / batch (spec §9) ----------------------------------------
# MAXLEN ~ N : ring-buffer cho frames:req. N ≈ Σdrone × fps × giây_giữ.
REQ_MAXLEN = _int("REQ_MAXLEN", 2000)
# COUNT: số entry tối đa mỗi XREADGROUP (micro-batch).
READ_COUNT = _int("READ_COUNT", 16)
# BLOCK ms: ngân sách gom batch, latency-first.
READ_BLOCK_MS = _int("READ_BLOCK_MS", 20)
# XAUTOCLAIM: coi worker chết sau ngần này ms không ack.
CLAIM_MIN_IDLE_MS = _int("CLAIM_MIN_IDLE_MS", 30000)

# --- MinIO / S3 (ảnh annotated durable, architecture §4/§8) -----------------
# Mặc định LOCAL (compose). Máy MinIO 192.168.100.190 hiện không cùng mạng —
# khi thông thì đặt MINIO_ENDPOINT=192.168.100.190:9000.
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "admin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "admin123456")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "images")
MINIO_SECURE = os.getenv("MINIO_SECURE", "0") == "1"  # UseSSL
# Lifecycle: tự xóa ảnh annotated cũ hơn N ngày (chống đầy đĩa). 0 = không đặt TTL.
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

# --- Model -----------------------------------------------------------------
MODEL_PATH = os.getenv("MODEL_PATH", "weights/yolo26x.pt")
DEVICE = os.getenv("DEVICE", "0")  # GPU index hoặc "cpu"
CONF = float(os.getenv("CONF", "0.25"))
IMGSZ = _int("IMGSZ", 960)
HALF = os.getenv("HALF", "1") == "1"  # FP16 (docs §2: Blackwell, cứ FP16 thoải mái)
