"""Load test tầng transport: mô phỏng đội drone bắn ảnh thật (COCO) qua Redis → AI worker
→ Redis → đo ở đầu BE. Đo throughput, latency p50/p95/p99, drop rate, ảnh MinIO.

Worker chạy TÁCH process (python -m transport.worker). Script này = đội drone + BE collector.
Dùng group riêng `cg:lt` nên không đụng `cg:be`.

Dùng:
  python -m transport.loadtest --src data/val2017 --drones 5 --fps 10 --secs 30 --max-images 1000
"""
from __future__ import annotations

import argparse
import threading
import time

import redis

from . import config, envelope
from .producer import load_images

LT_GROUP = "cg:lt"


def producer_thread(drone: str, images, fps: float, secs: float, stats: dict, stop: threading.Event):
    r = redis.from_url(config.REDIS_URL)
    period = 1.0 / fps if fps > 0 else 0.0
    seq = 0
    t_end = time.time() + secs
    while not stop.is_set() and time.time() < t_end:
        tick = time.time()
        name, img = images[seq % len(images)]
        frame_id = f"{drone}-{seq:07d}"
        gps = {"lat": 21.02 + seq * 1e-5, "lon": 105.84 + seq * 1e-5, "alt_agl": 80.0}
        att = {"yaw": 0.0, "pitch": -90.0, "roll": 0.0}
        fields = envelope.build_req(frame_id, drone, time.time(), img, gps=gps, attitude=att)
        r.xadd(config.REQ_STREAM, fields, maxlen=config.REQ_MAXLEN, approximate=True)
        seq += 1
        if period:
            sleep = period - (time.time() - tick)
            if sleep > 0:
                time.sleep(sleep)
    stats[drone] = seq


def collector_thread(stats: dict, stop: threading.Event):
    r = redis.from_url(config.REDIS_URL)
    try:
        r.xgroup_create(config.RES_STREAM, LT_GROUP, id="$", mkstream=True)
    except redis.exceptions.ResponseError:
        pass
    lats, n_ok, n_err, n_img = [], 0, 0, 0
    segs = {"wait": [], "dec": [], "inf": [], "ann": []}
    idle = 0.0
    while not stop.is_set() or idle < 2.0:
        resp = r.xreadgroup(LT_GROUP, "lt-be", {config.RES_STREAM: ">"}, count=64, block=200)
        if not resp or not resp[0][1]:
            idle += 0.2
            continue
        idle = 0.0
        now = time.time()
        ids = []
        for eid, fields in resp[0][1]:
            res = envelope.parse_res(fields)
            if res["status"] == "ok":
                n_ok += 1
                ts = res["result"].get("ts")
                if ts:
                    lats.append((now - ts) * 1000.0)
                tm = res["result"].get("timing")
                if tm:
                    for k in segs:
                        segs[k].append(tm[k])
                if res["annotated_ref"]:
                    n_img += 1
            else:
                n_err += 1
            ids.append(eid)
        r.xack(config.RES_STREAM, LT_GROUP, *ids)
    stats["lats"] = lats
    stats["segs"] = segs
    stats["n_ok"] = n_ok
    stats["n_err"] = n_err
    stats["n_img"] = n_img


def pct(xs, p):
    if not xs:
        return 0.0
    return sorted(xs)[min(len(xs) - 1, int(len(xs) * p))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="data/val2017")
    ap.add_argument("--drones", type=int, default=5)
    ap.add_argument("--fps", type=float, default=10.0, help="fps mỗi drone")
    ap.add_argument("--secs", type=float, default=30.0)
    ap.add_argument("--max-images", type=int, default=1000)
    args = ap.parse_args()

    print(f"nạp ảnh từ {args.src} (max {args.max_images}) ...")
    images = load_images(args.src, max_images=args.max_images)
    mb = sum(len(b) for _, b in images) / 1e6
    print(f"  {len(images)} ảnh, {mb:.0f}MB, avg {mb*1000/len(images):.0f}KB/ảnh")
    print(f"đội {args.drones} drone × {args.fps}fps × {args.secs:.0f}s "
          f"= target {args.drones*args.fps:.0f} fps tổng, "
          f"~{int(args.drones*args.fps*args.secs)} frame")

    pstats: dict = {}
    cstats: dict = {}
    stop = threading.Event()

    col = threading.Thread(target=collector_thread, args=(cstats, stop), daemon=True)
    col.start()
    time.sleep(0.3)

    t0 = time.time()
    prods = [
        threading.Thread(target=producer_thread,
                         args=(f"drone{i}", images, args.fps, args.secs, pstats, stop), daemon=True)
        for i in range(args.drones)
    ]
    for p in prods:
        p.start()
    for p in prods:
        p.join()
    send_dt = time.time() - t0
    sent = sum(pstats.values())
    print(f"\n== gửi xong: {sent} frame trong {send_dt:.1f}s = {sent/send_dt:.0f} fps thực (đầu vào) ==")
    print("chờ drain kết quả ...")
    stop.set()
    col.join(timeout=15)

    lats = cstats.get("lats", [])
    n_ok = cstats.get("n_ok", 0)
    n_err = cstats.get("n_err", 0)
    processed = n_ok + n_err
    dropped = max(0, sent - processed)
    print("\n================= KẾT QUẢ LOAD TEST =================")
    print(f"  gửi (drone→req)     : {sent}")
    print(f"  xử lý (res nhận)    : {processed}  (ok={n_ok} err={n_err})")
    print(f"  drop bởi MAXLEN     : {dropped}  ({100*dropped/sent:.1f}%)" if sent else "")
    print(f"  ảnh annotated MinIO : {cstats.get('n_img', 0)}")
    print(f"  throughput xử lý    : {processed/send_dt:.0f} fps")
    print(f"  g2g latency (ms)    : p50={pct(lats,0.50):.0f}  p95={pct(lats,0.95):.0f}  "
          f"p99={pct(lats,0.99):.0f}  max={max(lats) if lats else 0:.0f}")
    segs = cstats.get("segs", {})
    if segs.get("wait"):
        print("  --- phân rã chặng (p50 ms, đo trong worker) ---")
        print(f"    wait (xếp hàng+transport req) : {pct(segs['wait'],0.50):6.2f}   "
              f"(p95 {pct(segs['wait'],0.95):.0f})")
        print(f"    decode JPEG                   : {pct(segs['dec'],0.50):6.2f}")
        print(f"    infer YOLO26x                 : {pct(segs['inf'],0.50):6.2f}")
        print(f"    annotate (vẽ box+encode)      : {pct(segs['ann'],0.50):6.2f}   (upload MinIO async sau đó)")
    print("=====================================================")
    print("Ghi chú: drop>0 nghĩa là AI không kịp drone → ring-buffer cắt frame cũ (đúng thiết kế "
          "realtime). Tăng worker hoặc giảm fps để hạ drop.")


if __name__ == "__main__":
    main()
