"""MinIO/S3 — lưu ảnh annotated durable, upload ASYNC (architecture §4, spec §3.2).

Chỉ ảnh có detection mới encode + upload; stream chỉ mang `annotated_ref` (key MinIO),
KHÔNG nhét ảnh nặng vào Redis. Upload chạy trên thread pool để không chặn vòng infer.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO

import cv2
from minio import Minio
from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule
from minio.commonconfig import ENABLED, Filter

from . import config


class ImageStore:
    def __init__(self, max_workers: int = 4):
        self.client = Minio(
            config.MINIO_ENDPOINT,
            access_key=config.MINIO_ACCESS_KEY,
            secret_key=config.MINIO_SECRET_KEY,
            secure=config.MINIO_SECURE,
        )
        self.bucket = config.MINIO_BUCKET
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="minio-up")
        # Backpressure: chặn số ảnh chờ upload trong RAM (MinIO chậm → bỏ, không phình).
        self._pending = 0
        self._plock = threading.Lock()
        self.dropped_uploads = 0
        self._ensure_bucket()
        self.ensure_lifecycle()

    def _ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def ensure_lifecycle(self) -> None:
        """Đặt TTL theo TỪNG tầng (chống đầy đĩa): A=events/ lâu, B=uncertain/ trung, C=blackbox/
        ngắn. MinIO lifecycle chỉ theo NGÀY. Trước đây rule đặt nhầm prefix `annotated/` (không
        khớp `events/`) nên ảnh sự kiện không bao giờ tự xóa — nay sửa đúng.
        """
        rules = []
        if config.MINIO_TTL_DAYS > 0:
            rules.append(Rule(ENABLED, rule_id="expire-events",
                              rule_filter=Filter(prefix="events/"),
                              expiration=Expiration(days=config.MINIO_TTL_DAYS)))
        if config.UNCERTAIN_TTL_DAYS > 0:
            rules.append(Rule(ENABLED, rule_id="expire-uncertain",
                              rule_filter=Filter(prefix="uncertain/"),
                              expiration=Expiration(days=config.UNCERTAIN_TTL_DAYS)))
        if config.BLACKBOX_TTL_DAYS > 0:
            rules.append(Rule(ENABLED, rule_id="expire-blackbox",
                              rule_filter=Filter(prefix="blackbox/"),
                              expiration=Expiration(days=config.BLACKBOX_TTL_DAYS)))
        if not rules:
            return
        try:
            self.client.set_bucket_lifecycle(self.bucket, LifecycleConfig(rules))
        except Exception as e:  # noqa: BLE001
            print(f"[minio] set lifecycle failed: {e}")

    @staticmethod
    def key_for(frame_id: str) -> str:
        # Phân thư mục theo ngày cho dễ quản lý/đời sống.
        day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        return f"annotated/{day}/{frame_id}.jpg"

    @staticmethod
    def window_key(drone_id: str, window_id: int) -> str:
        """Key ỔN ĐỊNH cho 1 (drone, cửa sổ) — ảnh nét nhất ghi đè vào đây; KHÔNG tách
        thư mục theo lớp (smoke/fire lưu chung, ảnh annotated đã vẽ đủ box mọi lớp).
        Cửa sổ sau = key khác → mỗi cửa sổ giữ 1 ảnh đại diện riêng.
        """
        day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        return f"events/{day}/{drone_id}/{window_id}.jpg"

    @staticmethod
    def uncertain_key(drone_id: str, frame_id: str) -> str:
        """Tầng B — vùng nghi ngờ: 1 key/frame (lưu mọi frame nghi ngờ để audit/retrain)."""
        day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        return f"uncertain/{day}/{drone_id}/{frame_id}.jpg"

    @staticmethod
    def blackbox_key(drone_id: str, slot: int) -> str:
        """Tầng C — hộp đen: 1 key/slot-thời-gian (lấy mẫu) → timeline liên tục, nhẹ."""
        day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        return f"blackbox/{day}/{drone_id}/{slot}.jpg"

    def _reserve(self) -> bool:
        """Chiếm 1 slot trong hàng đợi upload. False = quá trần → caller BỎ ảnh (giữ RAM bounded)."""
        with self._plock:
            if self._pending >= config.MINIO_MAX_PENDING:
                self.dropped_uploads += 1
                if self.dropped_uploads % 100 == 1:
                    print(f"[minio] upload backlog đầy ({self._pending}), bỏ ảnh "
                          f"(tổng bỏ={self.dropped_uploads}) — MinIO chậm?")
                return False
            self._pending += 1
        return True

    def _release(self) -> None:
        with self._plock:
            self._pending -= 1

    def put_async(self, key: str, jpeg: bytes) -> bool:
        """Đẩy upload bytes đã encode vào pool, trả ngay. Lỗi upload chỉ log, không hỏng vòng infer."""
        if not self._reserve():
            return False
        data = bytes(jpeg)

        def _do():
            try:
                self.client.put_object(
                    self.bucket, key, BytesIO(data), length=len(data), content_type="image/jpeg"
                )
            except Exception as e:  # noqa: BLE001
                print(f"[minio] upload {key} failed: {e}")
            finally:
                self._release()

        self._pool.submit(_do)
        return True

    def put_image_async(self, key: str, bgr, quality: int, max_side: int = 0) -> bool:
        """Như put_async nhưng JPEG-encode (và downscale nếu max_side>0) NGAY TRONG thread upload.
        Mục đích: lấy `cv2.imencode` (CPU nặng ~vài ms/frame) RA KHỎI vòng infer → nâng trần
        throughput. Hot-path chỉ còn `res.plot()` (cần Result/GPU nên giữ lại). `bgr` là numpy
        ảnh BGR; caller KHÔNG được sửa in-place sau khi gọi (thread đọc bất đồng bộ).
        """
        if not self._reserve():
            return False
        img = bgr

        def _do():
            try:
                im = img
                if max_side > 0:
                    h, w = im.shape[:2]
                    m = max(h, w)
                    if m > max_side:
                        s = max_side / m
                        im = cv2.resize(im, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, quality])
                if not ok:
                    return
                data = buf.tobytes()
                self.client.put_object(
                    self.bucket, key, BytesIO(data), length=len(data), content_type="image/jpeg"
                )
            except Exception as e:  # noqa: BLE001
                print(f"[minio] put_image {key} failed: {e}")
            finally:
                self._release()

        self._pool.submit(_do)
        return True

    def presigned_get(self, key: str, expiry_s: int = 3600) -> str:
        """Presigned GET URL — cách CHUẨN để BE/FE truy cập object mà không expose bucket
        public và không phát creds. Trả URL tự hết hạn sau `expiry_s` giây.
        """
        from datetime import timedelta
        return self.client.presigned_get_object(self.bucket, key, expires=timedelta(seconds=expiry_s))

    def get(self, key: str) -> bytes:
        resp = self.client.get_object(self.bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def close(self) -> None:
        self._pool.shutdown(wait=True)


_singleton: ImageStore | None = None
_lock = threading.Lock()


def get_store() -> ImageStore:
    global _singleton
    if _singleton is None:
        with _lock:
            if _singleton is None:
                _singleton = ImageStore()
    return _singleton
