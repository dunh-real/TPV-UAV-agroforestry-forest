"""BE giả lập — XREADGROUP frames:res → in detection + đo độ trễ end-to-end.

Độ trễ glass-to-glass suy từ `ts` gắn lúc producer XADD (spec §11, §12). Thay phần in
bằng đẩy WebSocket/lưu DB sau (architecture §10).

Dùng:
  python -m transport.consumer
"""
from __future__ import annotations

import signal
import time

import redis

from . import config, envelope

_stop = False


def _handle_sig(*_):
    global _stop
    _stop = True


def main() -> None:
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    name = "be-1"
    r = redis.from_url(config.REDIS_URL)
    print(f"[be {name}] consuming {config.RES_STREAM} group {config.CG_BE}")

    n, lat_sum, lat_max = 0, 0.0, 0.0
    last_log = time.time()
    while not _stop:
        try:
            resp = r.xreadgroup(
                groupname=config.CG_BE, consumername=name,
                streams={config.RES_STREAM: ">"},
                count=config.READ_COUNT, block=500,
            )
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
            print(f"[be {name}] redis err: {e}; backoff 0.5s")
            time.sleep(0.5)
            continue

        if not resp:
            continue
        _, entries = resp[0]
        now = time.time()
        for eid, fields in entries:
            res = envelope.parse_res(fields)
            # Độ trễ glass-to-glass: từ `ts` producer gắn lúc bắt frame tới giờ ở BE.
            src_ts = res["result"].get("ts") if res["status"] == "ok" else None
            lat = (now - src_ts) * 1000.0 if src_ts else 0.0
            n += 1
            lat_sum += lat
            lat_max = max(lat_max, lat)
            if res["status"] == "ok":
                dets = res["result"]["detections"]
                top = ", ".join(f"{d['cls']}:{d['conf']:.2f}" for d in dets[:4])
                ref = res["annotated_ref"]
                ref_s = f" img={ref}" if ref else ""
                print(f"[be] {res['frame_id']} n={len(dets)} [{top}]{ref_s}")
            else:
                print(f"[be] {res['frame_id']} ERROR: {res['err']}")
            r.xack(config.RES_STREAM, config.CG_BE, eid)

        if time.time() - last_log >= 2.0 and n:
            print(f"[be] consumed={n} g2g avg={lat_sum/n:.1f}ms max={lat_max:.1f}ms")
            last_log = time.time()

    if n:
        print(f"[be {name}] stopped. consumed={n} g2g avg={lat_sum/n:.1f}ms max={lat_max:.1f}ms")


if __name__ == "__main__":
    main()
