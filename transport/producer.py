"""Drone giả lập — đọc ảnh từ thư mục (lặp vòng) → XADD frames:req ở fps cấu hình.

Đính metadata gps/attitude tổng hợp (architecture §8 contract). Ring-buffer MAXLEN
giữ RAM bounded (spec §6). Thay script này bằng drone/gateway thật sau.

Dùng:
  python -m transport.producer --src samples --drone drone1 --fps 7 --count 200
Không có --src hoặc thư mục rỗng → tự sinh ảnh test tổng hợp.
"""
from __future__ import annotations

import argparse
import glob
import os
import time

import cv2
import numpy as np
import redis

from . import config, envelope


def load_images(src: str | None, max_images: int = 0) -> list[tuple[str, bytes]]:
    """Trả [(name, jpeg_bytes)]. JPEG đọc raw bytes (drone gửi nguyên, không re-encode);
    PNG mới decode+encode. Không có ảnh → 1 frame test tổng hợp. max_images=0 = không giới hạn.
    """
    out: list[tuple[str, bytes]] = []
    if src and os.path.isdir(src):
        paths = sorted(
            p for ext in ("jpg", "jpeg", "png") for p in glob.glob(os.path.join(src, f"*.{ext}"))
        )
        for p in paths:
            if p.lower().endswith((".jpg", ".jpeg")):
                with open(p, "rb") as f:
                    out.append((os.path.basename(p), f.read()))
            else:
                img = cv2.imread(p)
                if img is None:
                    continue
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    out.append((os.path.basename(p), buf.tobytes()))
            if max_images and len(out) >= max_images:
                break
    if not out:
        # Frame tổng hợp 1080p (không có detection thật, chỉ test transport).
        img = (np.random.rand(1080, 1920, 3) * 64 + 96).astype(np.uint8)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        out.append(("synthetic.jpg", buf.tobytes()))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="samples", help="thư mục ảnh nguồn")
    ap.add_argument("--drone", default="drone1")
    ap.add_argument("--fps", type=float, default=7.0)  # khớp tải thực drone: 7 ảnh/s/drone
    ap.add_argument("--count", type=int, default=200, help="số frame gửi (0 = vô hạn)")
    args = ap.parse_args()

    images = load_images(args.src)
    print(f"[producer {args.drone}] {len(images)} ảnh nguồn, fps={args.fps}, maxlen~{config.REQ_MAXLEN}")

    r = redis.from_url(config.REDIS_URL)
    period = 1.0 / args.fps if args.fps > 0 else 0.0
    seq = 0
    sent = 0
    t_start = time.time()
    while args.count == 0 or sent < args.count:
        name, img = images[seq % len(images)]
        frame_id = f"{args.drone}-{seq:06d}"
        # GPS/attitude tổng hợp: bay quanh Hà Nội, nadir (-90° pitch).
        gps = {"lat": 21.0204 + seq * 1e-5, "lon": 105.8412 + seq * 1e-5, "alt_agl": 80.0}
        attitude = {"yaw": 12.5, "pitch": -90.0, "roll": 0.0}
        fields = envelope.build_req(
            frame_id, args.drone, time.time(), img, gps=gps, attitude=attitude, bands="RGB"
        )
        r.xadd(config.REQ_STREAM, fields, maxlen=config.REQ_MAXLEN, approximate=True)
        seq += 1
        sent += 1
        if sent % 50 == 0:
            print(f"[producer {args.drone}] sent={sent} xlen={r.xlen(config.REQ_STREAM)}")
        if period:
            time.sleep(period)

    dt = time.time() - t_start
    print(f"[producer {args.drone}] done: {sent} frame trong {dt:.1f}s ({sent/dt:.1f} fps thực)")


if __name__ == "__main__":
    main()
