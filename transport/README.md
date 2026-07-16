# Transport harness (test trên server)

Hiện thực tầng transport theo `docs/ai-pipeline.md`, **AI worker dùng
model thật YOLO26x** (không phải echo). Chạy được end-to-end ngay trên server này.

## Môi trường (đã dựng sẵn)

- Python **3.12.10** qua `uv`, venv ở `.venv/` (torch 2.12.1+cu130, ultralytics 8.4.75).
- GPU: 2× RTX PRO 6000 Blackwell (sm_120). YOLO26x ~9.6ms/frame @1080p fp32.
- Hạ tầng qua **docker-compose** (`docker compose up -d`):
  - Redis test: container `rs-redis-test` ở **host:6380**, password qua `.env` (`REDIS_PASSWORD`).
  - MinIO: production trỏ endpoint remote qua `.env` (`MINIO_ENDPOINT`, `MINIO_SECURE=1`), bucket
    `images`. Container `rs-minio` local chỉ là dev fallback (giờ thừa). Secrets đều trong `.env`.
- Weights: `weights/yolo26x.pt` + `weights/yolo26x.engine` (TensorRT FP16). Ảnh mẫu: `samples/`.

Cài lại từ đầu nếu cần:
```bash
uv venv --python 3.12.10 .venv
uv pip install --python .venv --index-strategy unsafe-best-match \
  --extra-index-url https://download.pytorch.org/whl/cu128 \
  torch torchvision ultralytics redis msgpack minio
docker compose up -d           # redis + minio
```

## Thành phần

| File | Vai trò | Spec |
|---|---|---|
| `config.py` | Tham số (REDIS_URL, stream, MAXLEN, batch…) qua env | §2, §9 |
| `envelope.py` | Đóng/mở gói msgpack + bytes ảnh | §3, §8 |
| `init_streams.py` | Tạo consumer group `cg:ai`, `cg:be` | §4.1 |
| `producer.py` | Drone giả lập: ảnh → XADD `dms:captures:ready` | §4.2 |
| `worker.py` | **YOLO26x**: XREADGROUP → decode → infer → (vẽ box + upload MinIO async nếu có detection) → XADD `dms:ai:results` → XACK; reclaim XPENDING+XCLAIM | §3, §4.3-4.4 |
| `storage.py` | MinIO/S3: upload ảnh annotated async (thread pool), bucket auto-create | §4 |
| (worker) event-gate + dedup | Lưu ảnh khi có sự kiện; best-in-window: giữ ảnh **nét nhất** /`(drone,lớp,cửa sổ)`, Lua atomic | §6.5, §11 |
| `consumer.py` | BE: XREADGROUP `dms:ai:results`, đo g2g latency, hiện `evidenceKey` | §10, §11 |
| `loadtest.py` | Đội drone (thread) + BE collector: đo throughput/latency/drop dưới tải | §11 |

## Contract I/O của AI server

Luồng giữ nguyên (ảnh bytes đi qua Redis, AI upload MinIO async). Output `dms:ai:results`
chuẩn hóa theo BE — worker chỉ trả kết quả phân tích, **gom nhóm/AlertEvent là việc của BE**.

```jsonc
// NHẬN  dms:captures:ready  (giữ nguyên — bytes trong Redis cho hot-path infer nhanh)
{ "frame_id": "drone1-000123", "drone_id": "drone1", "ts": ...,
  "meta": <msgpack: gps, attitude, bands>, "img": <JPEG bytes> }

// TRẢ  dms:ai:results  (= dms:ai:results, schema theo BE)
{ "captureId": "drone1-000123", "droneId": "drone1", "status": "ok",
  "result": { "hasDetection": true,
              "detections": [ { "type": "blast", "confidence": 0.91, "bbox": [x,y,w,h] } ],
              "model": "yolo26x", "modelVersion": "coco-pretrained" },
  "evidenceKey": "events/2026/.../droneZ/blast/356475310.jpg",  // key ảnh nét nhất cửa sổ (rỗng nếu không lưu)
  "err": "" }
```
`type` = nhãn model (sau fine-tune = mã sâu/bệnh). `MODEL_NAME`/`MODEL_VERSION` qua env.
**Độ tin cậy đã có**: idempotency `done:{captureId}` (chống emit trùng) + dead-letter
`dms:ai:dead-letter` (job giao > `MAX_DELIVERIES` lần) + `XPENDING`+`XCLAIM` reclaim job treo (hợp Redis 6.0).

## Chạy

Một phát end-to-end (worker + BE chạy nền, rồi producer):
```bash
DRONE=drone1 FPS=10 COUNT=100 scripts/run_e2e.sh
```

Hoặc tách tiến trình (mỗi cái 1 terminal — `python -u` để log không bị buffer):
```bash
.venv/bin/python -m transport.init_streams
.venv/bin/python -u -m transport.consumer
.venv/bin/python -u -m transport.worker          # WORKER_NAME=w2 DEVICE=1 cho worker thứ 2
.venv/bin/python -m transport.producer --src samples --drone drone1 --fps 10 --count 100
```

Giám sát Redis:
```bash
docker exec rs-redis-test redis-cli XLEN dms:captures:ready
docker exec rs-redis-test redis-cli XPENDING dms:captures:ready cg:ai
docker exec rs-redis-test redis-cli XINFO GROUPS dms:captures:ready
```

## Load test (ảnh thật COCO val2017, imgsz=960, MinIO bật)

`python -m transport.loadtest --src data/val2017 --drones D --fps F --secs T` (đội drone +
BE collector; worker chạy tách process). Số đo trên server 2× Blackwell:

| Test | Worker | Tải | Drop | Throughput | g2g p50 / p99 |
|---|---|---|---|---|---|
| 1 | 1× **.pt** FP16 | 50 fps | 0% | 50 fps | **722 / 1477 ms** (bão hòa) |
| 2 | 1× **engine** | 50 fps | 0% | 50 fps | **38 / 75 ms** |
| 3 | 1× engine | 250 fps | 55% | ~111 fps | (trần 1 worker) |
| 5 | **4× engine** (2 GPU) | 200 fps | 0% | 199 fps | **17 / 33 ms** |

Kết luận: TensorRT engine hạ latency ~19× ở cùng tải (drain nhanh hơn nhiều → hết dồn hàng);
trần 1 worker (đơn luồng: decode+infer+vẽ box+upload tuần tự) ~111 fps; **scale ngang nhiều
worker share GPU qua consumer group** → 4 worker gánh 200 fps, 0 drop, latency vẫn ~17ms.

### Chạy bằng TensorRT engine + nhiều worker
```bash
scripts/run_workers.sh        # 2 worker engine, mỗi GPU 1 (khuyến nghị ~4 drone)
N=4 scripts/run_workers.sh    # 4 worker (chỉ khi tải > ~100fps)
```

### Cấu hình khuyến nghị: ~4 drone (≈40fps tổng) → **2 worker, mỗi card 1**
Đo thực (4 drone × 10fps): 1 worker g2g p50=31ms; **2 worker p50=18ms** (wait 19.6→8ms), đều 0 drop.
Chọn 2 không vì tốc độ (1 đã thừa, trần ~111fps) mà vì: (a) latency thấp hơn 42% do song song hóa
burst, (b) **dự phòng** — 1 worker chết là đứng pipeline; 2 worker thì cái kia chạy tiếp + reclaim
nhặt frame pending, (c) cách ly 2 GPU. VRAM ~0.4–2GB/worker nên chi phí ~0. Đừng dùng 4 worker cho
4 drone (thừa, tốn VRAM vô ích).
Engine khóa GPU-arch + imgsz + batch ⇒ đổi card/imgsz phải export lại. Engine dynamic batch
(`dynamic=True batch=16`) build nặng, cần `workspace` lớn (≥32GB) — chưa cần vì multi-worker
batch=1 đã đủ. Worker tự dựng lại consumer group khi Redis bị flush/restart (NOGROUP → recreate, spec §5).

## Lưu ảnh: event-gate + dedup best-in-window (chống phình đĩa, giữ ảnh tốt nhất)

Worker chỉ **lưu ảnh** khi có detection là **sự kiện** (`cls ∈ EVENT_CLASSES`, rỗng=mọi lớp;
`conf ≥ EVENT_CONF`). Dedup theo `(drone, lớp-chủ-đạo, cửa-sổ)`:
- Mọi frame event trong cửa sổ trỏ về **cùng một `evidenceKey` ổn định** (`events/.../{window}.jpg`).
- Chỉ ảnh **NÉT NHẤT** (điểm = variance of Laplacian) được upload/ghi đè vào key đó — không giữ
  ảnh ĐẦU TIÊN (có thể mờ) như cách cũ. So sánh điểm bằng **Lua atomic** trên Redis nên nhiều
  worker không ghi đè lẫn nhau sai.
- **Metadata vẫn ra mọi frame** trên `dms:ai:results` (cờ `hasDetection`) → BE gom nhóm, đếm
  `EvidenceCount`, dùng `evidenceKey` làm ảnh đại diện. Chỉ ẢNH bị lọc, không mất metadata.

Verify: gửi mờ→nét→mờ cùng cửa sổ → giữ đúng **1 ảnh nét nhất** (độ nét 3466 vs ảnh mờ 22), cả 3
frame cùng `evidenceKey`. Config: `EVENT_CLASSES`, `EVENT_CONF` (0.5), `DEDUP_WINDOW_S` (5).
Bản đầy đủ dedup theo `track_id` (cùng *vật thể*, cần tầng tracking — chưa có) là cải tiến sau.

## Production trên server này (Redis + MinIO chạy thật)

- **restart: unless-stopped** cả 2 container → sống qua reboot (`docker compose up -d`).
- **MinIO lifecycle TTL**: tự xóa `annotated/` cũ hơn `MINIO_TTL_DAYS` (mặc định **7 ngày**) → đĩa
  không đầy. Set tự động khi worker khởi động (`storage.ensure_lifecycle`).
- **Bound upload queue**: tối đa `MINIO_MAX_PENDING` (4096 ≈ 600MB/worker) ảnh chờ trong RAM; MinIO
  chậm/sập thì bỏ ảnh + log thay vì phình RAM. Data MinIO ở named volume `minio-data` (persist).
- ⚠️ **Bảo mật còn hở**: `rs-redis-test` mở `0.0.0.0:6380` **không password**. Nếu server có mạng
  ngoài: bind `127.0.0.1:6380` (mọi thành phần chạy cùng server) hoặc thêm `requirepass` + sửa
  `REDIS_URL`. MinIO console `:9001` có auth (admin/...) nhưng nên đổi pass mặc định khi public.

## Chưa làm (đúng phạm vi — tầng AI/ngoài transport)

- Tracking + georef (architecture §6, chỉ lane A), MinIO `annotated_ref`, msgpack `bands`
  multispectral, orthomosaic/NDVI (lane B). Worker hiện trả bbox pixel thô, `geo`/`track_id`=null.
