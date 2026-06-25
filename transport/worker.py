"""AI worker — YOLO26x. XREADGROUP → decode → infer batch → XADD frames:res → XACK.

Stateless detection (architecture §3, §9): nhiều worker chia chung 1 GPU qua consumer
group; micro-batch theo ngân sách BLOCK ms. XAUTOCLAIM nhặt lại frame của worker chết
(spec §4.4). Output bbox pixel thô theo contract §8 (tracking/georef là stage sau).

Dùng:
  python -m transport.worker                 # 1 worker, device 0
  WORKER_NAME=w2 DEVICE=1 python -m transport.worker
"""
from __future__ import annotations

import os
import signal
import socket
import time

import cv2
import numpy as np
import redis
from ultralytics import YOLO

from . import config, envelope, storage

_stop = False


def _handle_sig(*_):
    global _stop
    _stop = True


def consumer_name() -> str:
    # Định danh ổn định để XAUTOCLAIM truy được (spec §2).
    return os.getenv("WORKER_NAME", f"ai-worker-{socket.gethostname()}-{os.getpid()}")


def decode(img_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def to_detections(result) -> list[dict]:
    """Ultralytics Result → list detection theo contract §8 (geo/track_id = null ở stage này)."""
    dets = []
    boxes = result.boxes
    if boxes is None:
        return dets
    names = result.names
    xywh = boxes.xywh.cpu().numpy()
    cls = boxes.cls.cpu().numpy().astype(int)
    conf = boxes.conf.cpu().numpy()
    for (x, y, w, h), c, p in zip(xywh, cls, conf):
        dets.append(
            {
                "track_id": None,
                "cls": names[int(c)],
                "conf": round(float(p), 4),
                "bbox": [float(x - w / 2), float(y - h / 2), float(w), float(h)],
                "geo": None,
            }
        )
    return dets


def is_event(d) -> bool:
    """Detection có tính là sự kiện không (lớp + ngưỡng conf). Lớp rỗng = mọi lớp."""
    return (not config.EVENT_CLASSES or d["cls"] in config.EVENT_CLASSES) and d["conf"] >= config.EVENT_CONF


def dedup_fresh_classes(r, drone_id, event_dets) -> set:
    """Trả các lớp sự kiện 'mới' trong cửa sổ (qua Redis SET NX EX) → cần lưu ảnh.
    Lock chung ở Redis nên nhiều worker không lưu trùng cùng 1 sự kiện (architecture §6.5).
    """
    fresh = set()
    for cls in {d["cls"] for d in event_dets}:
        if r.set(f"dedup:{drone_id}:{cls}", b"1", nx=True, ex=config.DEDUP_WINDOW_S):
            fresh.add(cls)
    return fresh


def process_batch(model, r, entries, store) -> int:
    """entries: list (id_bytes, fields). Trả số frame xử lý."""
    t_recv = time.time()  # mốc worker NHẬN batch (đo wait = xếp hàng + transport req)
    imgs, reqs, ids, dec_ms = [], [], [], []
    for eid, fields in entries:
        try:
            req = envelope.parse_req(fields)
            td = time.time()
            img = decode(req["img"])
            dec = (time.time() - td) * 1000.0
            if img is None:
                raise ValueError("decode failed")
        except Exception as e:  # frame hỏng → ack + báo error, không kẹt pending
            r.xadd(
                config.RES_STREAM,
                envelope.build_res(
                    fields.get(b"frame_id", b"?").decode(), "", [], status="error", err=str(e)
                ),
                maxlen=config.RES_MAXLEN, approximate=True,
            )
            r.xack(config.REQ_STREAM, config.CG_AI, eid)
            continue
        imgs.append(img)
        reqs.append(req)
        ids.append(eid)
        dec_ms.append(dec)

    if not imgs:
        return 0

    ti = time.time()
    results = model.predict(
        imgs, device=config.DEVICE, imgsz=config.IMGSZ, conf=config.CONF,
        half=config.HALF, verbose=False,
    )
    inf_ms = (time.time() - ti) * 1000.0 / len(imgs)  # phân bổ đều cho frame trong batch

    for eid, req, res, dec in zip(ids, reqs, results, dec_ms):
        dets = to_detections(res)
        events = [d for d in dets if is_event(d)]
        # LƯU ảnh chỉ khi: có sự kiện + dedup (1 ảnh/(drone,cls)/cửa sổ) — architecture §6.5.
        # Metadata vẫn ra mọi frame; chỉ ẢNH bị lọc để không phình đĩa.
        ta = time.time()
        annotated_ref = ""
        if store is not None and events:
            fresh = dedup_fresh_classes(r, req["drone_id"], events)
            if fresh:
                key = store.key_for(req["frame_id"])
                ok, buf = cv2.imencode(".jpg", res.plot(), [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    store.put_async(key, buf.tobytes())
                    annotated_ref = key
        ann_ms = (time.time() - ta) * 1000.0
        # `ts` gốc + timing per-stage để BE đo g2g + phân rã (observability §12).
        # `event` = frame có vật sự kiện (cho BE alert, mọi frame — độc lập với dedup lưu ảnh).
        timing = {"wait": (t_recv - req["ts"]) * 1000.0, "dec": dec, "inf": inf_ms, "ann": ann_ms}
        result_payload = {"detections": dets, "n": len(dets), "ts": req["ts"],
                          "event": bool(events), "timing": timing}
        r.xadd(
            config.RES_STREAM,
            envelope.build_res(
                req["frame_id"], req["drone_id"], result_payload, annotated_ref=annotated_ref
            ),
            maxlen=config.RES_MAXLEN, approximate=True,
        )
        r.xack(config.REQ_STREAM, config.CG_AI, eid)
    return len(ids)


def main() -> None:
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    name = consumer_name()
    print(f"[worker {name}] loading {config.MODEL_PATH} on device={config.DEVICE} "
          f"imgsz={config.IMGSZ} half={config.HALF}")
    model = YOLO(config.MODEL_PATH)
    # Warmup để build kernel trước khi vào vòng nóng.
    model.predict(np.zeros((config.IMGSZ, config.IMGSZ, 3), np.uint8),
                  device=config.DEVICE, half=config.HALF, verbose=False)
    r = redis.from_url(config.REDIS_URL)

    store = None
    if config.ANNOTATE:
        try:
            store = storage.get_store()
            print(f"[worker {name}] MinIO {config.MINIO_ENDPOINT} bucket={config.MINIO_BUCKET} ready")
        except Exception as e:  # MinIO chưa tới được → degrade: bỏ ảnh annotated, vẫn chạy
            print(f"[worker {name}] MinIO unavailable ({e}); annotated_ref sẽ rỗng")

    print(f"[worker {name}] ready. consuming {config.REQ_STREAM} group {config.CG_AI}")

    total, last_log = 0, time.time()
    while not _stop:
        # Trước hết nhặt lại frame pending của worker chết (spec §4.4).
        try:
            _, claimed, _ = r.xautoclaim(
                config.REQ_STREAM, config.CG_AI, name,
                min_idle_time=config.CLAIM_MIN_IDLE_MS, start_id="0-0", count=config.READ_COUNT,
            )
            if claimed:
                total += process_batch(model, r, claimed, store)
        except redis.exceptions.ResponseError:
            pass

        try:
            resp = r.xreadgroup(
                groupname=config.CG_AI, consumername=name,
                streams={config.REQ_STREAM: ">"},
                count=config.READ_COUNT, block=config.READ_BLOCK_MS,
            )
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
            print(f"[worker {name}] redis err: {e}; backoff 0.5s")
            time.sleep(0.5)
            continue
        except redis.exceptions.ResponseError as e:
            # Redis bị flush/restart → group biến mất. Tạo lại rồi tiếp tục (spec §5).
            if "NOGROUP" in str(e):
                try:
                    r.xgroup_create(config.REQ_STREAM, config.CG_AI, id="$", mkstream=True)
                    print(f"[worker {name}] re-created group {config.CG_AI} sau NOGROUP")
                except redis.exceptions.ResponseError:
                    pass
                continue
            raise

        if resp:
            _, entries = resp[0]
            total += process_batch(model, r, entries, store)

        now = time.time()
        if now - last_log >= 2.0:
            print(f"[worker {name}] processed={total} xlen={r.xlen(config.REQ_STREAM)} "
                  f"pending={r.xpending(config.REQ_STREAM, config.CG_AI)['pending']}")
            last_log = now

    print(f"[worker {name}] stopped. total processed={total}")


if __name__ == "__main__":
    main()
