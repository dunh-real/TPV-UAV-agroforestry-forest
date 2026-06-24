# Remote Sensing — Drone AI Pipeline

Pipeline triển khai thực tế: drone **viễn thám** (cháy rừng, ngập lụt, hạn hán / sức khỏe cây
trồng, sâu bệnh) → **Redis Streams** → **AI (YOLO26x)** → **Redis Streams** → **Backend**, ảnh
durable trên **MinIO**.

```
Drone ──XADD──> [Redis: frames:req] ──XREADGROUP──> AI Worker ──XADD──> [Redis: frames:res] ──XREADGROUP──> BE
 (cam)            ring-buffer MAXLEN                 YOLO26x GPU            metadata (msgpack)                  │
                                                        │ ảnh vẽ box (chỉ khi có sự kiện + dedup)               │
                                                        └──────────── PUT ──> MinIO <── GET (annotated_ref) ────┘
```

> Tài liệu thiết kế: [kiến trúc tổng](docs/drone-yolo-pipeline-architecture.md) · [đặc tả transport](docs/communication-transport-spec.md).
> Chi tiết tầng transport + cách chạy: [transport/README.md](transport/README.md).

---

## Trạng thái hiện tại

**Lane A (real-time, drone→detect→alert) — tầng transport ĐÃ XONG & đo kiểm bằng tải thật:**

- [x] Kết nối Redis Streams: consumer group, ack, `XAUTOCLAIM` khôi phục worker chết, tự dựng lại group khi Redis flush/restart.
- [x] Chịu tải: trần ~282 fps / 4 worker; mục tiêu ~40 fps (4 drone) dư ~5×; backpressure `MAXLEN`, 0 drop dưới trần.
- [x] Real-time: g2g **16–18 ms** dưới tải (đã phân rã từng chặng).
- [x] Lưu trữ: MinIO + **event-gate + dedup** (giảm ~190× số ảnh lưu), bound RAM, lifecycle TTL.
- [x] AI: **YOLO26x** thật (TensorRT FP16 engine), 80 lớp COCO để test tốc độ/kết nối.

**Chưa làm** (cần data + domain, hoặc ngoài đường nóng Lane A):
- [ ] Dữ liệu + nhãn viễn thám thật → fine-tune model (thay 80 lớp COCO) + định nghĩa event/ngưỡng.
- [ ] **Georef** pixel→lat/lon (`geo` đang `null`) — cần camera intrinsics + attitude.
- [ ] Tracking (`track_id` đang `null`) + dedup theo track_id thay vì `(drone,cls)`.
- [ ] Ingestion thật (drone/RTSP) thay producer giả lập.
- [ ] **Lane B** (orthomosaic / NDVI / change detection) — pipeline batch riêng.

---

## Phần cứng & môi trường

| | |
|---|---|
| GPU | 2× **RTX PRO 6000 Blackwell** (sm_120, 96GB/card) |
| CPU / RAM | 16 core / 251GB |
| Python | **3.12.10** qua `uv`, venv `.venv/` |
| Stack | torch 2.12.1+cu130, ultralytics 8.4.75, TensorRT 11.1, redis-py, msgpack, minio |
| Hạ tầng | docker-compose: Redis (6380) + MinIO (9000/9001) |

### Cài đặt từ đầu
```bash
# 1) Python deps
uv venv --python 3.12.10 .venv
uv pip install --python .venv --index-strategy unsafe-best-match \
  --extra-index-url https://download.pytorch.org/whl/cu128 \
  torch torchvision ultralytics redis msgpack minio tensorrt onnx onnxslim

# 2) Hạ tầng (Redis + MinIO, có restart policy + healthcheck)
docker compose up -d

# 3) Weights: yolo26x.pt tự tải lần đầu khi chạy worker; export engine:
.venv/bin/python -c "from ultralytics import YOLO; YOLO('weights/yolo26x.pt').export(format='engine', half=True, imgsz=960, device=0)"

# 4) (tùy chọn) data load test
mkdir -p data && cd data && curl -sLO http://images.cocodataset.org/zips/val2017.zip && unzip -q val2017.zip
```

### Kết nối (mặc định)
- Redis: `redis://:admin123456@localhost:6380/0` (password đã bật).
- MinIO: `localhost:9000` (console `:9001`), `admin` / `admin123456`, bucket `images`.

---

## Chạy

```bash
# End-to-end demo (worker + BE nền, rồi producer giả lập)
DRONE=drone1 FPS=10 COUNT=100 scripts/run_e2e.sh

# Production: 2 worker engine, mỗi GPU 1 (khuyến nghị ~4 drone)
scripts/run_workers.sh

# Load test với ảnh thật COCO (đội drone + BE collector)
.venv/bin/python -m transport.loadtest --src data/val2017 --drones 4 --fps 10 --secs 20

# Giám sát Redis
docker exec rs-redis-test redis-cli -a admin123456 XINFO GROUPS frames:req
```

---

## Kết quả đo (server 2× Blackwell)

### Model — TensorRT engine vs PyTorch (YOLO26x, FP16, imgsz 960)
| | inference | end-to-end/frame |
|---|---|---|
| PyTorch `.pt` | 7.74 ms | 10.5 ms |
| **TensorRT `.engine`** | **3.18 ms** | **6.04 ms** |

→ engine nhanh **~2.4×** ở inference. (imgsz 640 nhanh hơn 960 ~1.18× nhưng bắt ít hơn ~10% vật nhỏ → giữ 960 cho viễn thám.)

### Chịu tải (engine, qua Redis đầy đủ, MinIO bật)
| Worker | Tải vào | Drop | Throughput | g2g p50 / p99 |
|---|---|---|---|---|
| 1× `.pt` | 50 fps | 0% | 50 | 722 / 1477 ms (bão hòa) |
| 1× engine | 50 fps | 0% | 50 | **38 / 75 ms** |
| 1× engine | 250 fps | 55% | ~111 | trần 1 worker |
| **4× engine** | 200 fps | 0% | 199 | **17 / 33 ms** |
| 4× engine | 350 fps | 19% | 282 | 1439 ms (quá trần → queue nổ) |

**Cấu hình khuyến nghị ~4 drone (≈40 fps): 2 worker engine, mỗi GPU 1.** Đo: 1 worker g2g p50=31ms, 2 worker p50=18ms, đều 0 drop. Chọn 2 vì latency thấp hơn + **dự phòng** (1 chết thì cái kia gánh), không vì tốc độ.

### Phân rã tổng time pipeline (p50, ms)
| chặng | dưới tải | ghi chú |
|---|---|---|
| xadd req → worker pickup | ~1 | transport Redis |
| decode JPEG | 2 | CPU |
| **infer YOLO26x** | 7–9 | ~50% thời gian |
| vẽ box + encode | 2 | |
| xadd res → BE pickup | ~1.5 | |
| **= G2G hot-path** | **~16** | cái BE thấy |
| upload MinIO | ~9 | **ASYNC, off hot-path** |

Phần xử lý ~**hằng số 13ms** ở mọi tải; vượt trần thì latency nổ do **queue wait** (không phải xử lý chậm), và `MAXLEN` drop frame cũ — đúng thiết kế realtime. *(Chưa gồm mạng drone→server thực địa 5–30ms.)*

### Lưu trữ — event-gate + dedup
120 frame @10fps (window 5s): metadata ra BE **120/120**, ảnh lưu MinIO **6** (giảm **20×**). Tải thật 1 vật dai dẳng → ~1 ảnh/cửa sổ thay vì ~38/s (**~190×**).

---

## Cấu hình ([transport/config.py](transport/config.py), qua env)

| Nhóm | Biến | Mặc định |
|---|---|---|
| Redis | `REDIS_URL` | `redis://:admin123456@localhost:6380/0` |
| Backpressure | `REQ_MAXLEN` · `READ_COUNT` · `READ_BLOCK_MS` | 2000 · 16 · 20 |
| Model | `MODEL_PATH` · `IMGSZ` · `HALF` · `CONF` | `weights/yolo26x.pt` · 960 · FP16 · 0.25 |
| MinIO | `MINIO_ENDPOINT` · `MINIO_BUCKET` | `localhost:9000` · `images` |
| MinIO hardening | `MINIO_TTL_DAYS` · `MINIO_MAX_PENDING` | 7 · 4096 |
| Event/dedup | `EVENT_CLASSES` · `EVENT_CONF` · `DEDUP_WINDOW_S` | (mọi lớp) · 0.5 · 5 |

**Production trên server này**: container `restart: unless-stopped`; MinIO lifecycle TTL tự xóa ảnh cũ; upload queue bound chống phình RAM. ⚠️ Redis 6380 đã có password; nếu BE/worker ở máy khác qua mạng ngoài thì bật TLS (`rediss://`).

---

## Cấu trúc

```
docs/            thiết kế (kiến trúc + transport spec)
transport/       harness: config, envelope, init_streams, producer, worker, consumer, storage, loadtest
scripts/         run_e2e.sh, run_workers.sh
docker-compose.yml   Redis + MinIO
weights/         yolo26x.pt + .engine (gitignored)
data/ samples/   ảnh test (gitignored)
```
