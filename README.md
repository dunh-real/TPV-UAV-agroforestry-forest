# Remote Sensing — Drone AI Pipeline

Pipeline triển khai thực tế: drone **viễn thám** (cháy rừng, ngập lụt, hạn hán / sức khỏe cây
trồng, sâu bệnh) → **Redis Streams** → **AI (YOLO26x)** → **Redis Streams** → **Backend (BE)**,
ảnh bằng chứng durable trên **MinIO**.

```
Drone ─XADD─> [Redis: dms:captures:ready] ─XREADGROUP─> AI Worker ─XADD─> [Redis: dms:ai:results] ─XREADGROUP─> BE
 (cam)         ring-buffer MAXLEN (bytes ảnh)            YOLO26x GPU         metadata theo BE (msgpack)          │
                                                            │ lưu ảnh 3 TẦNG (chống mất false-negative)          │
                                                            └─ PUT ─> MinIO: events/ · uncertain/ · blackbox/    │
                                                                         └────── GET (evidenceKey, chỉ tầng A) ──┘
                          job lỗi quá N lần ──> [Redis: dms:ai:dead-letter]
```

> Thiết kế: [kiến trúc tổng](docs/drone-yolo-pipeline-architecture.md) · [đặc tả transport](docs/communication-transport-spec.md).
> Chi tiết kỹ thuật tầng transport: [transport/README.md](transport/README.md).

---

## Trạng thái

**Lane A (real-time, drone→detect→alert) — tầng transport + AI server: ĐÃ XONG, đo kiểm bằng tải thật.**

- [x] **Kết nối** Redis Streams: consumer group, ack, reclaim worker chết (`XPENDING`+`XCLAIM`, hợp Redis ≥5), tự dựng lại group khi Redis flush.
- [x] **Chịu tải**: trần ~110 fps / 2 worker (có lưu A/B/C; ~282 fps / 4 worker); mục tiêu ~28 fps (4 drone @ 7fps) dư ~4×; backpressure `MAXLEN`, 0 drop dưới trần.
- [x] **Real-time**: g2g 16–18 ms dưới tải (đã phân rã từng chặng).
- [x] **Lưu trữ 3 tầng** (chống mất bằng chứng do model bỏ sót): A `events/` (sự kiện, dedup best-in-window), B `uncertain/` (frame nghi ngờ dưới ngưỡng — ứng viên false-negative), C `blackbox/` (mọi frame, thumbnail lấy mẫu) — bound RAM, lifecycle TTL theo tầng.
- [x] **Độ tin cậy**: idempotency (`done:{captureId}`) + dead-letter (`dms:ai:dead-letter`) + reclaim job treo.
- [x] **Output theo BE**: `captureId, hasDetection, detections[{type,confidence,bbox}], model/version, evidenceKey`.
- [x] **AI**: YOLO26x thật (TensorRT FP16 engine). 80 lớp COCO để test tốc độ/kết nối.

**Chưa làm** (cần data + domain, hoặc ngoài đường nóng Lane A):
- [ ] Dữ liệu + nhãn viễn thám thật → fine-tune (thay 80 lớp COCO) + định nghĩa event/ngưỡng cụ thể.
- [ ] **Georef** pixel→lat/lon (cần camera intrinsics + attitude).
- [ ] **Tracking** (`track_id`) → dedup theo vật thể thay vì cửa sổ thời gian.
- [ ] Ingestion thật (drone/RTSP) thay producer giả lập.
- [ ] **Lane B** (orthomosaic / NDVI / change detection) — pipeline batch riêng.

> Gom nhóm cảnh báo / `AlertEvent` / `EvidenceCount` / severity / ticket là việc của **BE** — worker
> chỉ trả kết quả phân tích + chọn ảnh đại diện tốt nhất.

---

## Contract I/O của AI server

Ảnh **bytes đi thẳng qua Redis** (không đẩy reference) để hot-path infer nhanh — MinIO ở remote
nên GET-per-frame quá đắt (đo được **66 ms**). AI upload ảnh lên MinIO **async** sau khi infer.

```jsonc
// NHẬN  dms:captures:ready
{ "frame_id": "drone1-000123", "drone_id": "drone1", "ts": 1719000000.1,
  "meta": <msgpack: gps, attitude, bands>, "img": <JPEG bytes> }

// TRẢ  dms:ai:results  (schema theo BE)
{ "captureId": "drone1-000123", "droneId": "drone1", "status": "ok",
  "result": { "hasDetection": true,
              "detections": [ { "type": "blast", "confidence": 0.91, "bbox": [x,y,w,h] } ],
              "model": "yolo26x", "modelVersion": "coco-pretrained" },
  "evidenceKey": "events/2026/06/25/drone1/blast/356475310.jpg",  // ảnh nét nhất cửa sổ (rỗng nếu không lưu)
  "err": "" }

// dms:ai:dead-letter : job giao > MAX_DELIVERIES lần (độc/treo) → đẩy vào đây + ack, thôi retry.
```
`type` = nhãn model (sau fine-tune = mã sâu/bệnh). Idempotency: `done:{captureId}` chặn emit trùng.

---

## Phần cứng & môi trường

| | |
|---|---|
| GPU | 2× **RTX PRO 6000 Blackwell** (sm_120, 96GB/card) |
| CPU / RAM | 16 core / 251GB |
| Python | **3.12.10** qua `uv`, venv `.venv/` |
| Stack | torch 2.12.1+cu130, ultralytics 8.4.75, TensorRT 11.1, redis-py, msgpack, minio |
| Hạ tầng | Redis + MinIO **remote** qua `.env` (Tailscale `100.x` / `s3.blogscloud.click`); compose local là dev fallback |

### Cài đặt
```bash
# 1) Python deps
uv venv --python 3.12.10 .venv
uv pip install --python .venv --index-strategy unsafe-best-match \
  --extra-index-url https://download.pytorch.org/whl/cu128 \
  torch torchvision ultralytics redis msgpack minio tensorrt onnx onnxslim

# 2) Secrets: copy .env.example -> .env rồi điền (config.py tự nạp; .env đã gitignore)
cp .env.example .env    # sửa REDIS_PASSWORD, MINIO_ENDPOINT/keys...

# 3) Hạ tầng local (Redis; MinIO local chỉ là dev fallback — production dùng remote qua .env)
docker compose up -d

# 4) Weights: yolo26x.pt tự tải lần đầu; export TensorRT engine:
.venv/bin/python -c "from ultralytics import YOLO; YOLO('weights/yolo26x.pt').export(format='engine', half=True, imgsz=960, device=0)"

# 5) (tùy chọn) data load test
mkdir -p data && cd data && curl -sLO http://images.cocodataset.org/zips/val2017.zip && unzip -q val2017.zip
```

---

## Chạy

```bash
# Production: 2 worker engine, mỗi GPU 1 (khuyến nghị ~4 drone)
scripts/run_workers.sh

# Demo end-to-end (worker + BE nền, rồi producer giả lập) — 7 ảnh/s/drone = tải thực
DRONE=drone1 FPS=7 COUNT=100 scripts/run_e2e.sh

# Load test ảnh thật COCO (đội drone + BE collector)
.venv/bin/python -m transport.loadtest --src data/val2017 --drones 4 --fps 7 --secs 20

# Giám sát (pass đọc từ .env)
docker exec rs-redis-test redis-cli -a "$REDIS_PASSWORD" XINFO GROUPS dms:captures:ready
```

---

## Kết quả đo (server 2× Blackwell, qua Redis đầy đủ)

### TensorRT engine vs PyTorch (YOLO26x, FP16, imgsz 960)
| | inference | e2e/frame |
|---|---|---|
| PyTorch `.pt` | 7.74 ms | 10.5 ms |
| **TensorRT `.engine`** | **3.18 ms** | **6.04 ms** |

→ engine nhanh **~2.4×**. (imgsz 640 nhanh hơn 960 ~1.18× nhưng bắt ít hơn ~10% vật nhỏ → giữ 960.)

### Chịu tải (engine, MinIO bật)
| Worker | Tải vào | Drop | Throughput | g2g p50 / p99 |
|---|---|---|---|---|
| 1× `.pt` | 50 fps | 0% | 50 | 722 / 1477 ms (bão hòa) |
| 1× engine | 50 fps | 0% | 50 | **38 / 75 ms** |
| 1× engine | 250 fps | 55% | ~111 | trần 1 worker |
| **4× engine** | 200 fps | 0% | 199 | **17 / 33 ms** |
| 4× engine | 350 fps | 19% | 282 | 1439 ms (quá trần → queue nổ) |

**Khuyến nghị ~4 drone (≈28 fps @ 7fps/drone): 2 worker engine, mỗi GPU 1** — đo p50=18ms, 0 drop. Chọn 2 vì
latency thấp + **dự phòng** (1 chết thì cái kia gánh + reclaim), không vì tốc độ (1 worker đã thừa).

### Trần 2 worker (engine, có lưu 3 tầng A/B/C) — quét fps thật trên 4 drone COCO
| fps tổng | fps/drone | g2g p50 | drop | trạng thái |
|---|---|---|---|---|
| 80 | 20 | 28 ms | 0% | vững |
| 100 | 25 | 28 ms | 0% | vững |
| **110** | **27** | **30 ms** | 0% | **mức cuối ổn định** |
| 120 | 30 | 742 ms | 0%* | knee — latency nổ |
| 140 | 35 | 2161 ms | 0%* | quá tải |

**Trần bão hòa ≈ 110–115 fps tổng ⇒ ~27 fps/drone (4 drone).** Knee rất sắc: dưới ngưỡng p50~30ms,
vượt là queue-wait nổ. Chọn **7 fps/drone ≈ 25% trần** → dư địa rất lớn. *(drop=0% ở 120/140 chỉ vì
ring-buffer 2000 hấp thụ burst 18s; chạy lâu sẽ tràn — tín hiệu quá tải là latency, không phải drop.)*
**Lưu ý:** trần này là **worst-case** (COCO detect ~97% frame → gần như mọi frame phải `plot()`+encode
trên hot-path); cảnh viễn thám thật phần lớn trống → ít detection → trần thật cao hơn nhiều.

### Phân rã tổng time pipeline (p50, ms)
| chặng | ms | ghi chú |
|---|---|---|
| XADD req → worker pickup | ~1 | transport Redis |
| decode JPEG | 2 | CPU |
| **infer YOLO26x** | 7–9 | ~50% |
| dedup (sharpness ds320) + vẽ box `res.plot()` | 2–4 | |
| XADD res → BE pickup | ~1.5 | |
| **= G2G hot-path** | **~16** | cái BE thấy |
| JPEG encode + upload MinIO (remote) | ~85 | **ASYNC, off hot-path** |

Phần xử lý ~**hằng số 13ms** ở mọi tải; vượt trần thì latency nổ do **queue wait** + `MAXLEN`
drop frame cũ (đúng thiết kế realtime). *(Chưa gồm mạng drone→server thực địa 5–30ms.)*

---

## Lưu trữ & độ tin cậy

**Lưu ảnh 3 tầng** — chỉ lưu ảnh-có-detection thì tiết kiệm nhưng **mất frame model bỏ sót**
(false-negative). Phân tầng theo giá trị, giảm chất lượng/tần suất cho tầng rủi ro thấp (input
~7fps×4drone≈28fps, lưu full mọi frame ~hàng trăm GB/ngày là bất khả thi):

| Tầng | Prefix | Lưu gì | Chất lượng | TTL | Ra BE? |
|---|---|---|---|---|---|
| **A** | `events/` | frame **có sự kiện** (≥`EVENT_CONF`) | full annotated, **dedup best-in-window** | `MINIO_TTL_DAYS` (7) | ✅ `evidenceKey` |
| **B** | `uncertain/` | có detection thô (≥`CONF`) **chưa đạt sự kiện** — ứng viên false-negative cao nhất | annotated q`UNCERTAIN_JPEG_Q` (70) | `UNCERTAIN_TTL_DAYS` (2) | ❌ nội bộ |
| **C** | `blackbox/` | **MỌI frame** kể cả 0-detection | thumbnail `BLACKBOX_IMGSZ` (640), lấy mẫu 1/`BLACKBOX_SAMPLE_S`(1)s/drone | `BLACKBOX_TTL_DAYS` (1) | ❌ nội bộ |

→ Footprint cuộn ~15-20 GB thay vì hàng trăm GB/ngày. B/C là lưới an toàn + corpus để **chạy lại
model mới** (sau fine-tune) bắt các miss cũ. Tầng C lấy mẫu đa-worker an toàn qua `SET NX bb:{drone}:{slot}`.

**Dedup best-in-window** (tầng A): mỗi `(drone, lớp-chủ-đạo, cửa sổ DEDUP_WINDOW_S)` → 1 `evidenceKey`
ổn định; chỉ ảnh **nét nhất** (variance of Laplacian) được upload/ghi đè (so điểm bằng **Lua atomic**
→ đa worker an toàn). Metadata vẫn ra mọi frame → BE gom nhóm + đếm `EvidenceCount`.

**Idempotency**: `done:{captureId}` (TTL `DONE_TTL_S`) — frame redeliver đã emit → chỉ ack, không trùng.
**Dead-letter**: frame giao > `MAX_DELIVERIES` lần → `dms:ai:dead-letter` + ack (tránh loop vô tận).

**MinIO hardening**: lifecycle TTL **theo từng tầng** (rule riêng `events/`·`uncertain/`·`blackbox/`);
upload queue bound `MINIO_MAX_PENDING` (bỏ + log khi MinIO chậm, không phình RAM); upload async off hot-path.

---

## Cấu hình ([transport/config.py](transport/config.py), qua env / `.env`)

| Nhóm | Biến | Mặc định |
|---|---|---|
| Redis | `REDIS_URL` · `REDIS_PASSWORD` | (qua `.env`) |
| Stream | `REQ_STREAM` · `RES_STREAM` · `DLQ_STREAM` | `dms:captures:ready` · `dms:ai:results` · `dms:ai:dead-letter` |
| Backpressure | `REQ_MAXLEN` · `READ_COUNT` · `READ_BLOCK_MS` | 2000 · 16 · 20 |
| Model | `MODEL_PATH` · `MODEL_VERSION` · `IMGSZ` · `HALF` · `CONF` | `weights/yolo26x.pt` · `coco-pretrained` · 960 · FP16 · 0.25 |
| Event/dedup | `EVENT_CLASSES` · `EVENT_CONF` · `DEDUP_WINDOW_S` | (mọi lớp) · 0.5 · 5 |
| Lưu tầng B | `SAVE_UNCERTAIN` · `UNCERTAIN_JPEG_Q` · `UNCERTAIN_TTL_DAYS` | 1 · 70 · 2 |
| Lưu tầng C | `SAVE_BLACKBOX` · `BLACKBOX_SAMPLE_S` · `BLACKBOX_IMGSZ` · `BLACKBOX_JPEG_Q` · `BLACKBOX_TTL_DAYS` | 1 · 1 · 640 · 60 · 1 |
| Độ tin cậy | `DONE_TTL_S` · `MAX_DELIVERIES` · `CLAIM_MIN_IDLE_MS` | 120 · 5 · 30000 |
| MinIO | `MINIO_ENDPOINT` · `MINIO_SECURE` · `MINIO_BUCKET` · `MINIO_TTL_DAYS` · `MINIO_MAX_PENDING` | (qua `.env`) · `images` · 7 · 4096 |

Secrets thật chỉ ở `.env` (gitignored). File tracked dùng placeholder. Redis qua mạng ngoài → bật TLS (`rediss://`).

---

## Cấu trúc

```
docs/            thiết kế (kiến trúc + transport spec)
transport/       config, envelope, init_streams, producer, worker, consumer, storage, loadtest
scripts/         run_workers.sh, run_e2e.sh
docker-compose.yml   Redis (+ MinIO local dev fallback)
.env.example     mẫu secrets (copy -> .env)
weights/ data/ samples/   weights + ảnh test (gitignored)
```
