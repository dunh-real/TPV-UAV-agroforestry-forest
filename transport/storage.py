"""MinIO/S3 — lưu ảnh annotated durable, upload ASYNC (architecture §4, spec §3.2).

Chỉ ảnh có detection mới encode + upload; stream chỉ mang `annotated_ref` (key MinIO),
KHÔNG nhét ảnh nặng vào Redis. Upload chạy trên thread pool để không chặn vòng infer.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO

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
        """Đặt TTL: tự xóa object `annotated/` cũ hơn MINIO_TTL_DAYS ngày (chống đầy đĩa)."""
        if config.MINIO_TTL_DAYS <= 0:
            return
        try:
            cfg = LifecycleConfig([
                Rule(ENABLED, rule_id="expire-annotated",
                     rule_filter=Filter(prefix="annotated/"),
                     expiration=Expiration(days=config.MINIO_TTL_DAYS))
            ])
            self.client.set_bucket_lifecycle(self.bucket, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"[minio] set lifecycle failed: {e}")

    @staticmethod
    def key_for(frame_id: str) -> str:
        # Phân thư mục theo ngày cho dễ quản lý/đời sống.
        day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        return f"annotated/{day}/{frame_id}.jpg"

    def put_async(self, key: str, jpeg: bytes) -> bool:
        """Đẩy upload vào pool, trả ngay. Quá trần pending → BỎ (giữ RAM bounded), trả False.
        Lỗi upload chỉ log, không làm hỏng vòng infer.
        """
        with self._plock:
            if self._pending >= config.MINIO_MAX_PENDING:
                self.dropped_uploads += 1
                if self.dropped_uploads % 100 == 1:
                    print(f"[minio] upload backlog đầy ({self._pending}), bỏ ảnh "
                          f"(tổng bỏ={self.dropped_uploads}) — MinIO chậm?")
                return False
            self._pending += 1
        data = bytes(jpeg)

        def _do():
            try:
                self.client.put_object(
                    self.bucket, key, BytesIO(data), length=len(data), content_type="image/jpeg"
                )
            except Exception as e:  # noqa: BLE001
                print(f"[minio] upload {key} failed: {e}")
            finally:
                with self._plock:
                    self._pending -= 1

        self._pool.submit(_do)
        return True

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
