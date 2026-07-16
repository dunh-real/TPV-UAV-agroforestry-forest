# AI Pipeline — Kiến trúc, Transport & Tích hợp BE

> Gộp từ 3 doc cũ (`drone-yolo-pipeline-architecture.md`, `communication-transport-spec.md`,
> `be-integration.md`) và cập nhật theo hiện trạng production. Dataset/train model: xem
> [wildfire-dataset.md](wildfire-dataset.md). Kết quả đo + vận hành: xem [README](../README.md).
>
> Cập nhật: 2026-07-16 — model **wildfire-v2**, stream `dms:*`, MinIO `s3.lamhai.net`, lưu 3 tầng.

---

## 1. Tổng quan & nguyên tắc

```
Drone ─XADD─> [Redis: dms:captures:ready] ─XREADGROUP─> AI Worker ─XADD─> [Redis: dms:ai:results] ─XREADGROUP─> BE
 (cam)         ring-buffer MAXLEN (bytes ảnh)            YOLO26x GPU         metadata msgpack                     │
                                                            │ lưu ảnh 3 TẦNG (events/uncertain/blackbox)          │
                                                            └─ PUT async ─> MinIO ─── GET (evidenceKey, tầng A) ──┘
                          job lỗi quá N lần ──> [Redis: dms:ai:dead-letter]
```

- **Ảnh nặng đi đường ngắn nhất** (drone → Redis → AI), **metadata nhẹ đi xa** (AI → BE → client).
  Bytes JPEG nằm **thẳng trong stream entry** (Redis binary-safe, KHÔNG base64 — phình 33%);
  MinIO chỉ giữ bản durable/annotated, không nằm trên hot-path (GET-per-frame remote đo được ~66ms).
- **Metadata trả MỌI frame**; ảnh annotated chỉ tạo khi có detection, upload **async** off hot-path.
- `frame_id`/`captureId` = khóa khớp request–response + idempotency. Worker **stateless** →
  scale ngang bằng consumer group. Toàn bộ inference chạy trên server (2 GPU).
- GPU dư khủng (2× RTX PRO 6000 Blackwell 96GB) ⇒ tối ưu nhắm **latency + IO**, không phải
  throughput inference. Nút thắt thật = decode/encode JPEG + mạng.
- Mọi bên là tiến trình độc lập nối TCP qua Redis ("dùng chung Redis" = cùng trỏ một endpoint,
  không cần cùng máy). Redis đặt **gần AI** (traffic ảnh nặng); BE ở xa được. Hiện Redis prod
  qua Tailscale (`100.x:6379`), MinIO remote `s3.lamhai.net` — cấu hình trong `.env`.

**Model đang serve** (nguồn sự thật là `.env`, `transport/config.py` tự nạp):
`wildfire_v2_y26x.engine` (TensorRT FP16, imgsz 960) · `MODEL_VERSION=wildfire-v2` ·
lớp `smoke`/`fire` · `CONF=0.2` (ngưỡng thô, hứng tầng B) · ngưỡng sự kiện theo lớp
`EVENT_CONF_CLS="smoke:0.3,fire:0.5"` (khói sớm ưu tiên recall, BE gom N-frame lọc nhiễu).

---

## 2. Các bên & giao thức

| Bên | Vai trò | Nói với | Giao thức |
|---|---|---|---|
| Drone / Gateway / BE producer | Producer ảnh + metadata | Redis | `XADD` |
| Redis | Message bus (streams) | tất cả | RESP over TCP (+TLS nếu mạng ngoài) |
| AI Worker (`transport/worker.py`) | Consumer ảnh → Producer result | Redis, MinIO | `XREADGROUP`/`XADD`, S3 API |
| Backend (BE) | Consumer result | Redis, MinIO | `XREADGROUP`, S3 GET (`evidenceKey`) |
| MinIO | Ảnh durable 3 tầng | AI (PUT), BE (GET) | S3 API |

### Key / stream (BE không cần tự tạo — AI `init_streams` tạo sẵn, idempotent)

| Key | Kiểu | Ý nghĩa |
|---|---|---|
| `dms:captures:ready` | Stream | Frame input chờ AI (ring-buffer `MAXLEN ~ 2000`) |
| `dms:ai:results` | Stream | Kết quả AI cho BE (`MAXLEN ~ 4000`) |
| `dms:ai:dead-letter` | Stream | Job giao > `MAX_DELIVERIES`(5) lần — độc/treo, thôi retry |
| `cg:ai` / `cg:be` | Consumer group | Nhóm AI worker / nhóm BE consumer |

Nhiều drone vẫn dùng **một** stream chung — consumer group tự load-balance. Chỉ tách theo drone
khi cần thứ tự per-drone (stage tracking, chưa làm).

---

## 3. Contract I/O (chi tiết cho BE)

### 3.1 Input `dms:captures:ready` — một entry, đúng 5 field

```redis
XADD dms:captures:ready MAXLEN ~ 2000 * frame_id <id> drone_id <drone> ts <epoch_string> meta <msgpack_bytes> img <jpeg_bytes>
```

| Field | Định dạng trên wire | Ghi chú |
|---|---|---|
| `frame_id` | string UTF-8, **duy nhất** | thành `captureId` ở output; khóa idempotency |
| `drone_id` | string UTF-8 | |
| `ts` | **string** epoch **giây** có phần thập phân, vd `"1719374400.123456"` | KHÔNG phải int ms / số binary |
| `meta` | **MessagePack** của `{"gps": {...}, "attitude": {...}\|null, "bands": "RGB"}` | field phải có, giá trị được phép null |
| `img` | **raw JPEG bytes** | KHÔNG base64, không re-encode |

**3 lỗi BE hay mắc:** (1) `ts` gửi int milliseconds → latency sai/parse lỗi;
(2) `img` base64 → decode hỏng, frame rơi vào dead-letter; (3) `meta` là JSON string → phải là msgpack bytes.

### 3.2 Output `dms:ai:results`

| Field | Định dạng | Nội dung |
|---|---|---|
| `captureId` | string | = `frame_id` đã gửi |
| `droneId` | string | |
| `status` | string | `ok` \| `error` (`err` chứa lý do) |
| `result` | MessagePack | xem dưới |
| `evidenceKey` | string | key MinIO ảnh annotated tầng A; `""` nếu không có sự kiện |
| `err` | string | |

`result` unpack:

```json
{
  "hasDetection": true,
  "detections": [
    { "type": "smoke", "confidence": 0.91, "bbox": [120, 80, 240, 360] }
  ],
  "model": "wildfire_v2_y26x",
  "modelVersion": "wildfire-v2",
  "ts": 1719374400.123456
}
```

- `type`: nhãn model — hiện `smoke` | `fire` (wildfire-v2). Domain khác sau này thêm lớp.
- `bbox` = `[x, y, w, h]` **pixel trên ảnh gốc**, gốc trên-trái — BE tự vẽ overlay khi hiển thị.
- `ts` = echo `ts` BE gửi → BE đo g2g bằng **đồng hồ của chính BE** (`BE_now − ts`, một đồng hồ
  duy nhất, không dính clock skew giữa 2 máy). Kỳ vọng p50 ~20ms ở tải 4 drone.
- Metadata ra **mọi frame** (kể cả 0 detection); cảnh báo/gom nhóm/`AlertEvent`/severity là việc BE.
- Nhiều frame cùng `(drone, lớp)` trong cửa sổ dedup trỏ **cùng một** `evidenceKey` (ảnh nét nhất) —
  bình thường, không phải lỗi.

### 3.3 Quy trình BE tiêu thụ

```redis
XREADGROUP GROUP cg:be be-worker-01 COUNT 10 BLOCK 5000 STREAMS dms:ai:results >
# unpack result → lưu/log → (cần thì GET MinIO bằng evidenceKey) →
XACK dms:ai:results cg:be <entry-id>     # BẮT BUỘC, không ack = kẹt pending
```

MinIO cho BE: endpoint + creds trong `.env` (`s3.lamhai.net`, bucket `images`). Chỉ tầng A
(`events/…`) ra BE; `uncertain/`/`blackbox/` là nội bộ AI.

---

## 4. Redis Streams — vận hành & độ tin cậy

### 4.1 Vòng đời frame

```
producer XADD ─> dms:captures:ready ─(BLOCK trả ngay)─> worker nhận [pending cg:ai]
   ─> decode → infer → XADD dms:ai:results → XACK
   └─ worker chết trước XACK: entry kẹt pending ─> XAUTOCLAIM (idle > 30s) worker khác nhặt lại
   └─ giao quá MAX_DELIVERIES(5) lần: đẩy dms:ai:dead-letter + ack (chống loop frame độc)
```

- **Blocking read = push không polling**: `XREADGROUP ... COUNT 16 BLOCK 20` — Redis giao frame
  ngay khoảnh khắc XADD; `BLOCK` vài ms cũng là ngân sách gom micro-batch (latency-first).
- **Backpressure = MAXLEN**: realtime ưu tiên frame mới — AI chậm hơn drone thì frame cũ nhất
  tự rớt, độ trễ không tích lũy. `RES_MAXLEN=2×REQ_MAXLEN` vì XACK **không** xóa entry,
  chỉ MAXLEN mới xóa (thiếu trần → `dms:ai:results` phình vô hạn).
- **Idempotency**: `SET done:{captureId} NX EX 120` trước khi emit — frame redeliver chỉ ack,
  không emit trùng. BE cũng nên dedup theo `captureId`.
- **Kết nối**: pool riêng cho lệnh ngắn, conn riêng cho blocking read; reconnect backoff + jitter;
  `socket_keepalive`. Qua mạng ngoài bật password + TLS (`rediss://`).
- **Giám sát**: `XLEN` (tồn đọng), `XPENDING` (lag thật), `XINFO GROUPS`; hoặc `scripts/monitor.sh`
  (dashboard 2s: stream/pending/fps/GPU/worker) + `scripts/mon_minio.sh` (3 tầng).
  Pending tăng đều = AI không kịp → thêm worker hoặc giảm fps. BE tự xác minh đường ống:
  `XLEN dms:captures:ready` tăng khi đẩy rồi tụt về ~0 khi AI nuốt.

### 4.2 Tham số (env / `.env` — bảng đầy đủ trong README)

| Tham số | Default | Ghi chú |
|---|---|---|
| `REQ_MAXLEN` | 2000 | `N ≈ Σdrone × fps × giây_giữ`; trần RAM ≈ N × cỡ frame (~150MB @ 300KB) |
| `READ_COUNT` / `READ_BLOCK_MS` | 16 / 20 | micro-batch, latency-first |
| `CLAIM_MIN_IDLE_MS` | 30000 | worker coi là chết sau 30s không ack |
| `DONE_TTL_S` / `MAX_DELIVERIES` | 120 / 5 | idempotency / dead-letter |

Payload tham khảo: JPEG 1080p q~80 ≈ 150–400KB; meta msgpack <1KB; result vài KB.
4 drone × 7fps × 300KB ≈ 8.4 MB/s — Redis thừa sức, nút thắt là băng thông + RAM.

---

## 5. AI worker & lưu trữ 3 tầng

Worker (`transport/worker.py`): `XREADGROUP` → decode JPEG (CPU ~2ms) → YOLO26x TensorRT FP16
batch → gate sự kiện theo `EVENT_CLASSES`/`EVENT_CONF_CLS` → `XADD` result → `XACK`; ảnh upload
MinIO **async** (queue bound `MINIO_MAX_PENDING`, MinIO sập thì bỏ ảnh + log, không phình RAM).
Mặc định 4 worker (2/GPU, `scripts/run_workers.sh`) — trần ~282fps, tải thật ~28fps dùng ~10%.

**Lưu 3 tầng** (chống mất bằng chứng khi model bỏ sót — chỉ lưu ảnh có detection thì mất
false-negative; lưu full mọi frame ~trăm GB/ngày bất khả thi):

| Tầng | Prefix | Lưu gì | Chất lượng / TTL | Ra BE? |
|---|---|---|---|---|
| A | `events/` | frame **có sự kiện** (≥ ngưỡng lớp) | full annotated, dedup best-in-window / 7d | ✅ `evidenceKey` |
| B | `uncertain/` | có detection thô (≥`CONF` 0.2) **chưa đạt sự kiện** — ứng viên false-negative + **mỏ data fine-tune vòng sau** | annotated q70 / 2d | ❌ |
| C | `blackbox/` | MỌI frame, thumbnail 640 lấy mẫu 1/s/drone (`SET NX` đa-worker an toàn) | q60 / 1d | ❌ |

**Dedup best-in-window (tầng A)**: mỗi `(drone, lớp, cửa sổ 5s)` → 1 `evidenceKey` ổn định;
chỉ ảnh **nét nhất** (variance of Laplacian, so điểm Lua atomic) được ghi đè. Lifecycle TTL
đặt **theo từng prefix**. Footprint cuộn ~15–20GB.

> Dedup ở **đầu ra** (lưu/alert), KHÔNG lọc đầu vào theo GPS — tọa độ là của drone, không phải
> của cảnh; GPU dư nên detect mọi frame còn sống sau ring-buffer.

---

## 6. Thiết kế cho tương lai (chưa làm — giữ lại từ doc gốc)

- **Hai lane**: Lane A real-time cảnh báo cháy (đã chạy — toàn bộ doc này). **Lane B** khảo sát
  batch (nông nghiệp/ngập/sâu bệnh): bay lưới chồng lấn 70–80% → orthomosaic (OpenDroneMap) →
  NDVI/NDWI → segmentation → change detection → GeoTIFF/PostGIS. Card B hiện rảnh, dành cho lane B + train.
- **Georef pixel→lat/lon** (lane A): cần camera intrinsics (calibrate 1 lần, gửi static) + GPS AGL
  + attitude gimbal per-frame. Pinhole: pixel → tia camera → xoay hệ thế giới → cắt mặt đất z=0 →
  ENU → lat/lon. Sai số chính = góc gimbal (1° @100m chếch → vài m); góc gần phương ngang
  `d_world.z→0` → sai hàng chục m, chỉ tin vùng gần nadir, trả kèm `accuracy_m`. Envelope đã
  chừa chỗ (`attitude` nullable) → tier thấp tự degrade, contract không đổi.
- **Tracking** (`track_id`, chỉ lane A nếu cần theo khói lan): tách stage riêng per-drone in-order
  (detection stateless không track được). Camera bay → BoT-SORT + CMC, hoặc georef trước rồi
  track trong ENU (chuyển động camera tự triệt tiêu, cần telemetry tốt). Với viễn thám vùng/diện
  tích thì tracking gần như bỏ — dedup theo cửa sổ thời gian hiện tại là đủ.
- **Sensor-agnostic**: field `bands` trong meta (RGB / NIR/RedEdge / thermal) → stage tiền xử lý
  tính index theo band có sẵn; không NIR thì fallback VARI/ExG. Ag mà chỉ RGB thì model đuối —
  giá trị nằm ở cảm biến phổ.
- Còn nợ: luật alert N-frame/T-giây (BE), đo false-alarm rate/giờ, radiometric calibration,
  ground-truth sâu bệnh, observability lane B.

---

## 7. Checklist test tích hợp BE (tóm tắt)

1. `PING` Redis prod (Tailscale) → `PONG`.
2. Chạy consumer nền: `XREADGROUP GROUP cg:be ... dms:ai:results >` → unpack → **XACK**.
3. Đẩy N frame đúng 5 field (mục 3.1) — `frame_id` duy nhất, `ts` string giây, `meta` msgpack, `img` raw JPEG.
4. Mỗi `frame_id` nhận đúng **một** entry `captureId = frame_id`; `status=error` thì đọc `err`.
5. `hasDetection=true` → `evidenceKey != ""` → GET MinIO bucket `images` ra ảnh annotated.
6. Đo g2g = `BE_now − result.ts` (kỳ vọng p50 ~20ms @ 4 drone).
7. Không có AI trả lời vẫn xác minh được ống: `XLEN dms:captures:ready` tăng rồi tụt về ~0.
