"""AI worker — YOLO26x. XREADGROUP → decode → infer batch → XADD frames:res → XACK.

Stateless detection (architecture §3, §9): nhiều worker chia chung 1 GPU qua consumer
group; micro-batch theo ngân sách BLOCK ms. XPENDING+XCLAIM nhặt lại frame của worker chết
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
    """Detection có tính là sự kiện không (lớp + ngưỡng conf). Lớp rỗng = mọi lớp.
    Ngưỡng lấy theo lớp (EVENT_CONF_CLS) nếu có, không thì EVENT_CONF chung."""
    thr = config.EVENT_CONF_CLS.get(d["cls"], config.EVENT_CONF)
    return (not config.EVENT_CLASSES or d["cls"] in config.EVENT_CLASSES) and d["conf"] >= thr


def sharpness(img) -> float:
    """Độ nét = variance of Laplacian (cao = nét hơn). Dùng chọn ảnh TỐT NHẤT trong cửa sổ
    thay vì giữ ảnh đầu tiên (architecture §6.5 "frame tốt nhất").

    Tính trên ảnh thu nhỏ (cạnh dài ~320) vì hàm này chạy cho MỌI frame có event (hot-path):
    full-res ~7.6ms vs ds320 ~1.8ms. Chỉ cần THỨ HẠNG tương đối trong cùng (drone,cửa sổ) — các
    frame cùng drone cùng độ phân giải nên ranking giữ nguyên sau khi downscale.
    """
    h, w = img.shape[:2]
    if max(h, w) > 320:
        s = 320.0 / max(h, w)
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# Lua atomic: cập nhật điểm-best của key NẾU điểm mới cao hơn. Trả 1 nếu đây là ảnh nét
# nhất tới giờ trong cửa sổ → caller upload (ghi đè ảnh cũ). Lần đầu đặt TTL = cửa sổ;
# các lần sau KEEPTTL (cửa sổ không bị kéo dài). Đa worker an toàn nhờ chạy 1 lượt trên Redis.
_BEST_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2]); return 1
elseif tonumber(ARGV[1]) > tonumber(cur) then
  redis.call('SET', KEYS[1], ARGV[1], 'KEEPTTL'); return 1
end
return 0
"""


def is_best_in_window(r, dedup_key: str, score: float) -> bool:
    """True nếu `score` là điểm nét cao nhất tới giờ cho key (cửa sổ) — nên upload/ghi đè."""
    return bool(r.eval(_BEST_LUA, 1, dedup_key, score, config.DEDUP_WINDOW_S))


def reclaim_pending(r, name) -> list:
    """Nhặt lại entry pending idle quá lâu (worker chết) bằng XPENDING + XCLAIM thay cho
    XAUTOCLAIM — để tương thích cả Redis < 6.2 (vd 6.0.16). Trả list (id, fields).
    Lọc idle ở client (IDLE của XPENDING cũng chỉ có từ 6.2); XCLAIM còn chốt min-idle ở server.
    """
    pend = r.xpending_range(config.REQ_STREAM, config.CG_AI,
                            min="-", max="+", count=config.READ_COUNT)
    stale = [p["message_id"] for p in pend
             if p["time_since_delivered"] >= config.CLAIM_MIN_IDLE_MS]
    if not stale:
        return []
    return r.xclaim(config.REQ_STREAM, config.CG_AI, name, config.CLAIM_MIN_IDLE_MS, stale)


def drain_poison(r, claimed) -> list:
    """Tách entry đã giao quá MAX_DELIVERIES lần (job độc/treo) → đẩy dead-letter + ack,
    thôi retry. Trả lại danh sách entry còn xử lý được. Chỉ chạy ở nhánh XAUTOCLAIM.
    """
    if not claimed:
        return claimed
    delivered = {
        p["message_id"]: p["times_delivered"]
        for p in r.xpending_range(config.REQ_STREAM, config.CG_AI,
                                  min="-", max="+", count=len(claimed) * 2)
    }
    keep = []
    for eid, fields in claimed:
        n = delivered.get(eid, 1)
        if n > config.MAX_DELIVERIES:
            cap = fields.get(b"frame_id", b"?").decode()
            r.xadd(config.DLQ_STREAM, {
                b"captureId": cap.encode(), b"droneId": fields.get(b"drone_id", b"").decode().encode(),
                b"reason": f"exceeded {config.MAX_DELIVERIES} deliveries".encode(),
                b"deliveries": str(n).encode(),
            }, maxlen=10000, approximate=True)
            r.xack(config.REQ_STREAM, config.CG_AI, eid)
            print(f"[worker] dead-letter {cap} (giao {n} lần > {config.MAX_DELIVERIES})")
        else:
            keep.append((eid, fields))
    return keep


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
                    fields.get(b"frame_id", b"?").decode(), {}, status="error", err=str(e)
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

    for eid, req, res, dec, img in zip(ids, reqs, results, dec_ms, imgs):
        # Idempotency: captureId đã emit result trước đó (frame bị redeliver) → chỉ ack, bỏ qua.
        if r.exists(f"done:{req['frame_id']}"):
            r.xack(config.REQ_STREAM, config.CG_AI, eid)
            continue
        dets = to_detections(res)
        events = [d for d in dets if is_event(d)]
        # Lưu ảnh PHÂN TẦNG (chống mất bằng chứng do false-negative):
        #  A events/   : có sự kiện → full-res annotated, dedup best-in-window (evidenceKey cho BE).
        #  B uncertain/: có detection thô nhưng chưa đạt sự kiện → ứng viên false-negative, lưu để audit.
        #  C blackbox/ : MỌI frame, thumbnail, lấy mẫu 1/giây/drone → lưới an toàn + corpus retrain.
        # Chỉ tầng A xuất evidenceKey ra BE; B/C lưu nội bộ để truy vết, BE không cần.
        ta = time.time()
        evidence_key = ""
        if store is not None:
            if events:
                top = max(events, key=lambda d: d["conf"])  # lớp conf cao nhất làm đại diện
                win = int(req["ts"] // config.DEDUP_WINDOW_S)
                evidence_key = store.window_key(req["drone_id"], top["cls"], win)
                if is_best_in_window(r, f"best:{req['drone_id']}:{top['cls']}:{win}", sharpness(img)):
                    # JPEG encode chạy trong thread upload (off hot-path); chỉ res.plot() ở đây.
                    store.put_image_async(evidence_key, res.plot(), 85)
            elif config.SAVE_UNCERTAIN and dets:
                # Tầng B: box dưới ngưỡng vẫn vẽ (res.plot ở conf=CONF) để xem model "suýt" thấy gì.
                store.put_image_async(
                    store.uncertain_key(req["drone_id"], req["frame_id"]), res.plot(),
                    config.UNCERTAIN_JPEG_Q)
            # Tầng C độc lập tầng A/B: SET NX chốt 1 slot/giây/drone (đa worker an toàn).
            # downscale + encode đều trong thread upload (truyền ảnh gốc + max_side).
            if config.SAVE_BLACKBOX:
                slot = int(req["ts"] // config.BLACKBOX_SAMPLE_S)
                if r.set(f"bb:{req['drone_id']}:{slot}", b"1", nx=True, ex=config.BLACKBOX_SAMPLE_S + 2):
                    store.put_image_async(store.blackbox_key(req["drone_id"], slot), img,
                                          config.BLACKBOX_JPEG_Q, max_side=config.BLACKBOX_IMGSZ)
        ann_ms = (time.time() - ta) * 1000.0
        # Output theo BE: hasDetection + detections[{type,confidence,bbox}] + model/version.
        # `ts`/`timing` giữ cho đo latency/observability (BE bỏ qua field thừa).
        timing = {"wait": (t_recv - req["ts"]) * 1000.0, "dec": dec, "inf": inf_ms, "ann": ann_ms}
        detections_out = [
            {"type": d["cls"], "confidence": d["conf"], "bbox": d["bbox"]} for d in dets
        ]
        result_payload = {
            "hasDetection": bool(events),
            "detections": detections_out,
            "model": config.MODEL_NAME,
            "modelVersion": config.MODEL_VERSION,
            "ts": req["ts"],
            "timing": timing,
        }
        r.xadd(
            config.RES_STREAM,
            envelope.build_res(
                req["frame_id"], result_payload, evidence_key=evidence_key,
                drone_id=req["drone_id"],
            ),
            maxlen=config.RES_MAXLEN, approximate=True,
        )
        # Đánh dấu đã emit (sau XADD → ưu tiên "trùng nhưng BE dedup" hơn "mất"). TTL tự hết.
        r.set(f"done:{req['frame_id']}", b"1", ex=config.DONE_TTL_S)
        r.xack(config.REQ_STREAM, config.CG_AI, eid)
    return len(ids)


def main() -> None:
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    name = consumer_name()
    print(f"[worker {name}] loading {config.MODEL_PATH} on device={config.DEVICE} "
          f"imgsz={config.IMGSZ} half={config.HALF}")
    # task tường minh: file .engine không mang metadata task → để ultralytics đoán sẽ
    # in WARNING mỗi lần load.
    model = YOLO(config.MODEL_PATH, task="detect")
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
            print(f"[worker {name}] MinIO unavailable ({e}); evidenceKey sẽ rỗng")

    print(f"[worker {name}] ready. consuming {config.REQ_STREAM} group {config.CG_AI}")

    total, last_log = 0, time.time()
    while not _stop:
        # Trước hết nhặt lại frame pending của worker chết (XPENDING+XCLAIM, spec §4.4).
        try:
            claimed = reclaim_pending(r, name)
            claimed = drain_poison(r, claimed)
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
