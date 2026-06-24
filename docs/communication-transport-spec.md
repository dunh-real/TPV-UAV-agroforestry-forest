# Đặc tả Giao tiếp & Truyền tải (Transport Layer Spec)

> Tài liệu kỹ thuật cho **tầng kết nối giữa các bên** — độc lập với model AI. AI worker được coi là **hộp đen**: nuốt một frame + metadata → nhả một result. Vì vậy phần này build/test được NGAY, không cần dữ liệu train.
>
> Doc kiến trúc tổng: `drone-yolo-pipeline-architecture.md`. Cập nhật: 2026-06-24

---

## 0. Phạm vi

Tài liệu này CHỈ đặc tả:
- Ai nói chuyện với ai, bằng giao thức gì.
- Định dạng message (envelope) + cách đóng gói ảnh.
- Cơ chế Redis Streams: lệnh, consumer group, ack, backpressure.
- Kết nối: pool, reconnect, auth/TLS.
- Ngân sách tốc độ/độ trễ + chính sách drop.
- Độ tin cậy & idempotency.

KHÔNG đụng tới: model, input/output AI, segmentation, NDVI, orthomosaic (xem doc tổng).

---

## 1. Các bên & giao thức

| Bên | Vai trò | Nói với | Giao thức |
|---|---|---|---|
| **Drone / Gateway** | Producer ảnh + metadata | Redis | TCP (redis client) → `XADD` |
| **Redis** | Message bus (stream) | tất cả | RESP3 over TCP (+TLS nếu qua mạng ngoài) |
| **AI Worker** | Consumer ảnh → Producer result | Redis, MinIO | Redis (`XREADGROUP`/`XADD`), S3 API (MinIO) |
| **Backend (BE)** | Consumer result | Redis, MinIO | Redis (`XREADGROUP`), S3 API (lấy ảnh annotated) |
| **MinIO/S3** | Lưu ảnh durable (chỉ khi cần) | AI, BE | S3 API (HTTP) |

**Nguyên tắc:** mọi bên là tiến trình độc lập nối qua TCP. "Dùng chung Redis" = cùng trỏ một endpoint `redis://host:6379`, KHÔNG cần cùng máy. Đặt Redis gần AI (traffic ảnh nặng); BE ở xa được (chỉ đọc metadata nhẹ).

```
Drone ──XADD──> [Redis: stream req] ──XREADGROUP──> AI Worker ──XADD──> [Redis: stream res] ──XREADGROUP──> BE
                                                        │                                                     │
                                                        └────────── PUT (ảnh) ──> MinIO <── GET (ảnh) ────────┘
```

---

## 2. Quy ước key / stream

| Key | Kiểu | Ý nghĩa |
|---|---|---|
| `frames:req` | Stream | Frame thô từ drone chờ AI xử lý |
| `frames:res` | Stream | Kết quả AI cho BE |
| `cg:ai` | Consumer group trên `frames:req` | Nhóm AI worker chia frame |
| `cg:be` | Consumer group trên `frames:res` | Nhóm BE consumer |

- Nhiều drone → vẫn dùng **một** `frames:req` chung (đơn giản, consumer group tự load-balance). Chỉ tách stream theo drone khi cần **thứ tự per-drone** (vd stage tracking — không thuộc tầng này).
- Consumer name nên ổn định + định danh máy: `ai-worker-{host}-{pid}` để `XAUTOCLAIM` truy được.

---

## 3. Message envelope (đóng gói)

Redis stream entry là tập **field–value**, value **binary-safe** ⇒ nhét thẳng bytes ảnh, không cần base64.

### `frames:req` — một entry
| Field | Kiểu | Nội dung |
|---|---|---|
| `frame_id` | string | `{drone_id}-{seq}`, duy nhất toàn cục, khóa idempotent |
| `drone_id` | string | định danh drone |
| `ts` | string/float | epoch giây lúc bắt frame (đồng bộ NTP!) |
| `meta` | bytes (msgpack) | gps, attitude, bands, … (gói nhị phân, gọn hơn JSON) |
| `img` | bytes | **raw JPEG/PNG bytes** (KHÔNG base64) |

> Tách `meta` (msgpack) khỏi `img` (bytes) cho gọn. Dùng msgpack thay JSON: nhỏ hơn ~20–30%, parse nhanh, vẫn giữ kiểu số. JSON chỉ dùng khi bắt buộc liên thông hệ ngoài.

### `frames:res` — một entry
| Field | Kiểu | Nội dung |
|---|---|---|
| `frame_id` | string | khớp với req (idempotent) |
| `drone_id` | string | |
| `result` | bytes (msgpack) | output AI (detections/mask refs/index…) — schema do tầng AI định, tầng này không quan tâm |
| `annotated_ref` | string \| empty | key MinIO của ảnh đã xử lý (rỗng nếu không tạo) |
| `status` | string | `ok` \| `error` |
| `err` | string | thông điệp lỗi nếu `status=error` |

**Quan trọng:** kết quả KHÔNG nhét ảnh nặng vào `frames:res`. Ảnh đi MinIO, stream chỉ mang `annotated_ref`.

---

## 4. Redis Streams — lệnh & vòng đời

### 4.1 Khởi tạo (một lần)
```bash
# Tạo consumer group, đọc từ cuối stream ($) — bỏ qua frame cũ tồn đọng
XGROUP CREATE frames:req cg:ai $ MKSTREAM
XGROUP CREATE frames:res cg:be $ MKSTREAM
```

### 4.2 Producer (drone) — đẩy frame
```bash
# MAXLEN ~ N : ring-buffer, tự cắt frame cũ. Dấu ~ = cắt xấp xỉ (rẻ hơn cắt chính xác)
XADD frames:req MAXLEN ~ 500 * \
  frame_id "drone1-000123" drone_id "drone1" ts "1719..." \
  meta <msgpack-bytes> img <jpeg-bytes>
```

### 4.3 Consumer (AI worker) — đọc, xử lý, ack
```bash
# Blocking read: thức dậy NGAY khi có frame mới; COUNT = batch tối đa; BLOCK ms = ngân sách chờ
XREADGROUP GROUP cg:ai ai-worker-h1-42 COUNT 16 BLOCK 20 STREAMS frames:req >
# ... xử lý batch ...
XACK frames:req cg:ai <id1> <id2> ...     # báo đã xong, gỡ khỏi pending
```

### 4.4 Khôi phục frame của worker chết
```bash
# Nhặt lại entry pending quá idle 30s mà worker khác chưa ack
XAUTOCLAIM frames:req cg:ai ai-worker-h1-42 30000 0 COUNT 32
```

### 4.5 Giám sát tồn đọng
```bash
XLEN frames:req                  # số entry đang giữ
XPENDING frames:req cg:ai        # số đã giao nhưng chưa ack (lag thực)
XINFO GROUPS frames:req          # lag từng group
```

### Vòng đời một frame
```
drone XADD ──> entry vào frames:req (id = ts-seq)
   │
   ▼ (BLOCK trả về ngay)
AI worker nhận ──> [pending list của cg:ai] ──> xử lý ──> XADD frames:res ──> XACK frames:req
   │                                                                            │
   └── nếu worker chết trước XACK: entry kẹt pending ──> XAUTOCLAIM nhặt lại ───┘
```

---

## 5. Kết nối: pool, reconnect, auth

- **Connection pool**: mỗi process giữ pool (vd redis-py `ConnectionPool`, 4–16 conn). KHÔNG mở/đóng conn mỗi lệnh.
- **Blocking read dùng conn riêng**: lệnh `XREADGROUP ... BLOCK` chiếm conn suốt thời gian block ⇒ tách khỏi pool dùng cho lệnh ngắn (ack, info), tránh cạn pool.
- **Reconnect**: bắt lỗi `ConnectionError`/`TimeoutError` → backoff (vd 50ms→1s, jitter) → reconnect → tiếp tục từ `XREADGROUP ... >` (frame đang dở vẫn nằm pending, không mất).
- **Health**: `PING` định kỳ; `socket_keepalive=True`.
- **Auth/TLS**: qua mạng ngoài bật `requirepass` + TLS (`rediss://`). Cùng LAN/VPC kín thì có thể bỏ TLS để giảm latency.
- **RESP3** (`HELLO 3`): hỗ trợ kiểu dữ liệu tốt hơn, nên bật nếu client hỗ trợ.

---

## 6. Backpressure & chính sách drop

Realtime ⇒ **frame mới quý hơn frame cũ**. Backpressure xử lý bằng chính ring-buffer, KHÔNG để dồn vô hạn.

- **MAXLEN ~ N** trên `frames:req`: AI chậm hơn drone ⇒ entry cũ nhất bị đẩy ra (drop), độ trễ không tích lũy.
- Chọn `N ≈ Σ(drone) × fps × giây_giữ`. Ví dụ 5 drone × 10fps × 2s ≈ 100. Đặt nhỏ để drop nhanh.
- **Không** dùng list `LPUSH/BRPOP` không giới hạn → dễ nổ RAM khi AI tụt lại.
- Theo dõi `XPENDING` + `XLEN`: nếu pending tăng đều ⇒ AI không kịp ⇒ thêm worker hoặc giảm fps drone.
- Tùy chọn: drone tự **adaptive fps** khi phát hiện lag (đo qua `frames:res` trễ).

---

## 7. Ngân sách tốc độ & độ trễ

### Kích thước payload (ước lượng)
| Thứ | Cỡ |
|---|---|
| 1 frame JPEG 1080p chất lượng ~80 | ~150–400 KB |
| meta (msgpack) | < 1 KB |
| result metadata | vài KB |
| ảnh annotated (MinIO) | tương đương ảnh gốc |

### Throughput vào Redis
- 5 drone × 10 fps × ~300 KB ≈ **15 MB/s**, ~50 entry/s. Redis xử lý **hàng trăm nghìn op/s** ⇒ thừa sức; nút thắt là **băng thông mạng + RAM**, không phải Redis CPU.
- RAM trần ≈ `N × cỡ_frame`. N=500 × 300KB ≈ **150 MB** — bounded, an toàn.

### Ngân sách độ trễ (glass-to-glass, ước lượng)
| Chặng | Thời gian điển hình |
|---|---|
| Drone nén + gửi mạng | 5–30 ms (tùy mạng/WiFi/4G) |
| `XADD` → worker nhận (BLOCK) | < 1–2 ms |
| Gom micro-batch | 0–20 ms (= BLOCK timeout) |
| Decode JPEG (GPU) | 1–5 ms |
| Infer | (tầng AI) |
| `XADD` res → BE nhận | < 1–2 ms |
| **Tổng phần transport** | **~10–60 ms** (chưa tính infer + mạng drone) |

Kết luận: transport **không phải nút thắt**; phần ăn thời gian thật là mạng drone→Redis và decode/infer. Tối ưu transport = giữ batch timeout thấp + decode GPU + Redis gần AI.

---

## 8. Độ tin cậy & idempotency

- **At-least-once**: consumer group đảm bảo mỗi entry giao tới khi có `XACK`. Worker chết → `XAUTOCLAIM` giao lại ⇒ một frame **có thể xử lý 2 lần**.
- **Idempotent bằng `frame_id`**: trước khi `XADD` result, có thể `SET nx:done:{frame_id} 1 EX 60` để chặn xử lý trùng (nếu BE nhạy với trùng). BE cũng nên dedup theo `frame_id`.
- **Không mất frame đang xử lý**: entry nằm pending cho tới khi ack; reconnect không làm mất.
- **Frame bị drop bởi MAXLEN** thì coi như mất *có chủ đích* (realtime chấp nhận) — KHÁC với mất do lỗi.

---

## 9. Bảng tham số cấu hình

| Tham số | Vị trí | Giá trị khởi điểm | Ghi chú |
|---|---|---|---|
| `MAXLEN ~` | XADD req | `Σdrone × fps × 2s` | Trần RAM + độ tươi frame |
| `COUNT` | XREADGROUP | 8–16 | Batch tối đa mỗi lần đọc |
| `BLOCK` | XREADGROUP | 10–25 ms | Ngân sách gom batch (latency-first) |
| `XAUTOCLAIM min-idle` | recover | 30000 ms | Coi worker là chết sau 30s không ack |
| Pool size | mỗi process | 4–16 | + 1 conn riêng cho blocking read |
| Reconnect backoff | client | 50ms→1s, jitter | |
| `done:{frame_id}` TTL | idempotency | 60 s | Chỉ cần đủ dài hơn thời gian retry |

---

## 10. Phần BE tiêu thụ

- BE đọc `frames:res` qua group `cg:be` (nhiều instance BE chia tải), `XACK` sau khi đẩy client/lưu DB.
- BE **không** cần biết Redis lưu ảnh — chỉ đọc `result` + lấy ảnh từ MinIO qua `annotated_ref` khi cần.
- Nếu nhiều client cùng xem realtime: BE fan-out qua WebSocket/SSE, KHÔNG cho client đọc Redis trực tiếp.

---

## 11. Build & test NGAY (không cần model)

- [ ] Dựng Redis (docker) + tạo group `cg:ai`, `cg:be`.
- [ ] **Producer giả lập**: script đọc ảnh từ thư mục/video → `XADD frames:req` ở fps cấu hình.
- [ ] **AI worker giả lập (echo)**: `XREADGROUP` → sleep mô phỏng infer → `XADD frames:res` (copy frame_id) → `XACK`. *Thay sleep bằng model thật sau.*
- [ ] **BE giả lập**: `XREADGROUP frames:res` → in ra / đẩy WebSocket.
- [ ] Test tải: bơm 50–100 fps, đo `XLEN`/`XPENDING`, xác nhận MAXLEN giữ RAM bounded.
- [ ] Test chịu lỗi: kill worker giữa chừng → xác nhận `XAUTOCLAIM` nhặt lại, không mất frame.
- [ ] Test reconnect: restart Redis → client tự nối lại, tiếp tục.
- [ ] Đo độ trễ end-to-end (gắn `ts` lúc XADD, tính hiệu ở BE).

---

## 12. Observability tối thiểu

- Metrics: `XLEN frames:req`, `XPENDING` (lag), throughput in/out (entry/s), độ trễ p50/p95 (từ `ts`), tỉ lệ drop (suy từ seq gap của `frame_id`).
- Cảnh báo khi: pending tăng đều (AI không kịp), drop rate cao, không có entry mới (drone mất kết nối).
