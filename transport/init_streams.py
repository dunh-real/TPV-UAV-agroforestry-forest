"""Khởi tạo consumer group (chạy 1 lần). Spec §4.1.

Đọc từ cuối stream ($) — bỏ qua frame cũ tồn đọng. MKSTREAM tạo stream nếu chưa có.
"""
from __future__ import annotations

import redis
from redis.exceptions import ResponseError

from . import config


def ensure_group(r: redis.Redis, stream: str, group: str) -> None:
    try:
        r.xgroup_create(name=stream, groupname=group, id="$", mkstream=True)
        print(f"  created group {group} on {stream}")
    except ResponseError as e:
        if "BUSYGROUP" in str(e):
            print(f"  group {group} on {stream} already exists")
        else:
            raise


def main() -> None:
    r = redis.from_url(config.REDIS_URL)
    print(f"Redis: {config.REDIS_URL}  ping={r.ping()}")
    ensure_group(r, config.REQ_STREAM, config.CG_AI)
    ensure_group(r, config.RES_STREAM, config.CG_BE)
    print("init done.")


if __name__ == "__main__":
    main()
