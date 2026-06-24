# Kiến trúc Pipeline Drone Viễn thám → Redis → AI → Backend

> Tài liệu thiết kế cho dự án **triển khai thực tế** (không phải nghiên cứu): drone **viễn thám** bay khảo sát, gửi ảnh + tọa độ, AI phân tích và trả kết quả (chỉ số phổ + bản đồ/diện tích + tọa độ thật) về Backend.
>
> **Phạm vi**: cháy rừng, ngập lụt, hạn hán / sức khỏe cây trồng, sâu bệnh. Hai nhịp vận hành: real-time cảnh báo (cháy) + khảo sát→mosaic→phân tích (nông nghiệp/ngập).
>
> ⚠️ **Lưu ý lịch sử**: phần §6 (tracking) viết khi còn hiểu nhầm đây là bài toán *giám sát vật thể*. Với viễn thám, tracking **gần như bỏ** — xem §1.5 để biết phần nào còn áp dụng. Tầng vận chuyển (Redis/GPU/MinIO/contract) thì tái dùng nguyên.
>
> Cập nhật: 2026-06-24

---

## 0. TL;DR — Nguyên tắc xuyên suốt

- **GPU dư khủng** (2× RTX PRO 6000 Blackwell, 96GB/card) ⇒ mọi tối ưu nhắm vào **latency + IO**, KHÔNG phải throughput inference.
- **Ảnh nặng đi đường ngắn nhất** (drone → Redis → AI). **Metadata nhẹ đi xa** (AI → Redis → BE → client).
- **Metadata trả mọi frame** (vài KB). **Ảnh vẽ box chỉ tạo khi có detection**, lưu MinIO async, truyền `ref` — không nhét base64 hai chiều qua Redis.
- `frame_id` = khóa khớp request–response + idempotent. `drone_id` = tách luồng nhiều drone.
- Worker **stateless** cho detection ⇒ lên edge (Jetson) chỉ việc bê nguyên worker lên, contract không đổi.

---

## 1.5. REFRAME VIỄN THÁM — đọc trước §6

Đây là bài toán **vùng/diện tích**, không phải vật thể rời rạc. Khác về chất so với giám sát:

| Khía cạnh | Giám sát (framing cũ §6) | Viễn thám (đúng đề) |
|---|---|---|
| Tác vụ AI | Detection bbox | **Segmentation** (vùng) là chính; bbox chỉ cho khói / cây bệnh đơn lẻ / hotspot |
| Tracking | Trung tâm | **Gần như bỏ.** Chỉ còn nghĩa hẹp ở lane A (theo khói lan) |
| "Tín hiệu" | Pixel RGB | **Chỉ số phổ (NDVI/NDWI/nhiệt) thường ăn đứt deep model** |
| Định vị | GPS từng detection | Lane A: georef điểm; Lane B: **orthomosaic** (bản đồ là deliverable) |
| Thời gian | Track frame-by-frame | **Change detection** theo ngày/tuần |

### Hai lane chạy song song, chia chung hạ tầng

**Lane A — Real-time cảnh báo (cháy rừng).** Giữ gần nguyên kiến trúc stream §3–§5: drone → Redis `req` → GPU worker → `res` → BE. Sensor nhiệt (ngưỡng hotspot) + RGB (segment/detect khói-lửa). Latency thấp, cảnh báo + GPS ngay. **Ca duy nhất cần real-time.**

**Lane B — Khảo sát → mosaic → phân tích (nông nghiệp / ngập / sâu bệnh).** Nhịp batch, không cần 10fps:
1. Drone bay **lưới**, chụp ảnh **chồng lấn 70–80%** kèm GPS+attitude.
2. **Orthomosaic**: ghép ảnh thành bản đồ định vị (OpenDroneMap / Pix4D / Metashape). Nặng → batch trên **card B**.
3. **Index phổ**: NDVI / NDWI / nhiệt trên mosaic.
4. **Segmentation**: phân vùng (ngập/cạn, khỏe/stress, vùng bệnh) trên mosaic + index.
5. **Change detection**: so chuyến bay trước → xu hướng (hạn nặng dần, bệnh lan).
6. Output: bản đồ phân tích định vị (GeoTIFF/tiles) + thống kê **diện tích** → BE/GIS.

### Chỉ số phổ — đặc trưng quan trọng nhất của viễn thám
- **Sức khỏe cây / sâu bệnh / hạn hán** → **NDVI** = (NIR−Red)/(NIR+Red). Stress hiện ở NDVI *trước* khi mắt thấy. Cần băng **NIR/RedEdge**.
- **Ngập lụt** → **NDWI** (tách nước/đất).
- **Cháy rừng** → **nhiệt** (hotspot) + RGB (khói).
- Với ag, nếu chỉ RGB thì model rất đuối; **giá trị nằm ở cảm biến phổ**.

### Hai thứ hạ tầng MỚI phải thêm
- **Sensor-agnostic ingestion**: envelope mang metadata `bands` (RGB / R,G,B,NIR,RedEdge / thermal). Stage tiền xử lý tính index theo band có sẵn. Không NIR → fallback chỉ số RGB (VARI/ExG, kém hơn). ⇒ "chưa chốt cảm biến" không chặn thiết kế.
- **Temporal geo-store** (PostGIS / tile store): lưu lớp định vị theo thời gian cho change detection. Hoàn toàn mới so với surveillance.

### Phân vai card (cập nhật)
- **Card A** = lane A real-time inference.
- **Card B** = **orthomosaic + segmentation batch + change detection** (lane B). Giờ kín việc, không còn "dự phòng".

---

## 1. Câu hỏi nền tảng (đã chốt)

### Redis có phải lưu base64 không? → KHÔNG
Redis string là **binary-safe** → lưu thẳng bytes JPEG/PNG. Base64 chỉ cần khi ảnh phải nằm trong JSON envelope, nhưng nó **phình ~33%** và Redis là in-memory ⇒ ăn RAM nhanh. **Gửi raw bytes.**

### AI có phải decode không? → CÓ, bắt buộc
YOLO cần numpy array. Chiều vào: Redis → decode → infer. Chiều ra (nếu vẽ box): `cv2.imencode` → bytes → MinIO.

### Redis "dùng chung" có phải cùng máy không? → KHÔNG
Redis là server mạng (client–server qua TCP). "Dùng chung" = AI và BE cùng trỏ tới một `redis://host:6379`, host đặt đâu cũng được. 3 tiến trình độc lập nối qua mạng.
- **Tối ưu vị trí**: đặt Redis **gần AI** (cùng LAN/DC) vì traffic ảnh nặng; BE ở xa hơn được vì chỉ đọc metadata nhẹ.
- Qua mạng ngoài → bật password + TLS.

---

## 2. Phần cứng & hệ quả

| Hạng mục | Chi tiết |
|---|---|
| GPU | 2× **RTX PRO 6000 Blackwell**, 96GB GDDR7/card, Tensor core gen 5 có **FP4 native** |
| So với A100 | Nhanh hơn ~2× ở inference (A100 chỉ hơn về HBM bandwidth/NVLink — không liên quan YOLO) |
| Tải dự kiến | 4-5 drone × 3–10 FPS ≈ tối đa ~50 frame/s tổng |
| Kết luận | **Overkill khổng lồ** cho YOLO. 1 card chạy YOLOv8 hàng trăm–nghìn FPS |

**Tận dụng phần thừa:**
- Chạy model nặng hơn (YOLO26x) để tăng độ chính xác mà vẫn dư tốc độ.
- Có FP4/FP8 native ⇒ **không cần ép INT8 khổ sở**, cứ FP16 thoải mái.
- **Nút thắt thật = decode/encode JPEG + IO**, không phải GPU ⇒ decode JPEG **trên GPU** (nvJPEG/DALI).

**Phân vai card (CHỐT: detection chỉ 1 GPU):**
- ~50 fps với YOLO26x trên **một** RTX PRO 6000 vẫn dư cả trăm fps. Tách detection ra 2 card chỉ thêm phức tạp (đồng bộ cross-GPU) mà không được gì.
- **Card A** = toàn bộ detection real-time. 1 process, hoặc 2–4 worker process **chia chung 1 GPU** qua consumer group — nhiều process chỉ để **giấu độ trễ decode/IO** (process này infer thì process kia decode), KHÔNG phải vì thiếu compute.
- **Card B** = việc nặng *không real-time*: fine-tune/distill cho Jetson ("lò luyện"), VLM mô tả cảnh, hoặc failover. Không xẻ detection.

---

## 3. Kiến trúc tổng thể

```
┌─────────┐  JPEG bytes + GPS + attitude      ┌──────────────────┐
│ Drone   │ ────────── XADD ────────────────> │  Redis (req)     │
│ (n con) │                                   │  Stream ring-buf │
└─────────┘                                   │  MAXLEN ~ N      │
                                              └────────┬─────────┘
                                                       │ XREADGROUP (consumer group)
                                                       ▼
                                          ┌────────────────────────┐
                                          │  Detection workers      │ stateless, batch, GPU
                                          │  (N worker / 2 card+MIG) │ YOLO TensorRT FP16
                                          └────────────┬────────────┘
                                                       │ publish bbox thô (pixel)
                                                       ▼
                                          ┌────────────────────────┐
                                          │  det stream (key=drone) │
                                          └────────────┬────────────┘
                                                       │
                                                       ▼
                                          ┌────────────────────────┐
                                          │  Tracking + Georef      │ stateful theo drone, in-order, CPU
                                          │  gán track_id + lat/lon  │
                                          └────────────┬────────────┘
                                       metadata ┌──────┴───────┐ ảnh box (chỉ khi có detection)
                                                ▼              ▼
                                       ┌──────────────┐  ┌──────────────┐
                                       │ Redis (res)  │  │ MinIO / S3   │ durable, async
                                       └──────┬───────┘  └──────────────┘
                                              │ XREAD
                                              ▼
                                       ┌──────────────┐
                                       │  Backend     │ ──> client (vẽ box từ metadata
                                       └──────────────┘       hoặc lấy annotated_ref)
```

Mỗi mũi tên = một kết nối TCP. Các khối là tiến trình tách rời, đặt máy nào tùy ý.

---

## 4. Pipeline theo từng chặng

**Chặng 1 — Thu ảnh (drone).** Bắt frame → nén JPEG → đính `gps + ts + drone_id + attitude` → `XADD` thẳng bytes vào `req` stream với `MAXLEN ~ N`. Stream tự cắt frame cũ ⇒ RAM có trần, frame mới nhất ưu tiên.

**Chặng 2 — Hàng đợi (Redis).** `req` stream = ring-buffer transient. Consumer group `ai-workers` để nhiều worker chia frame, mỗi frame chỉ 1 worker. **Backpressure = MAXLEN**: AI chậm hơn drone ⇒ frame cũ rớt, không dồn.

**Chặng 3 — Suy luận (AI, 2 card).** Mỗi worker: gom **micro-batch** theo ngân sách ~10–25ms → decode JPEG **trên GPU** → YOLO batch (TensorRT FP16) → bbox pixel thô. Publish vào `det` stream (key theo `drone_id`).

**Chặng 4 — Tracking + Georef (xem §6).** Stateful theo drone, in-order. Gán `track_id`, georeference bbox → tọa độ thật. Metadata → `res` stream. Ảnh box chỉ khi có detection → encode + upload MinIO **async** trên thread pool.

**Chặng 5 — Tiêu thụ (Backend).** Đọc `res` stream, ghép metadata + GPS, đẩy client. Client vẽ box từ metadata, hoặc lấy ảnh annotated qua `annotated_ref` khi cần xem chi tiết / lưu bằng chứng.

---

## 5. Redis Streams — chi tiết vận hành

- `XADD req * ...` với `MAXLEN ~ N` (xấp xỉ, dùng `~` cho rẻ).
  - `N ≈ số_drone × số_giây_giữ × fps`. Đặt nhỏ để drop frame cũ nhanh (realtime ưu tiên frame mới).
- `XREADGROUP GROUP ai-workers <consumer> ...` — load-balance, mỗi frame 1 worker.
- `XACK` sau khi xử lý xong ⇒ không xử lý trùng.
- `XAUTOCLAIM` để nhặt lại frame của worker chết giữa chừng (pending).
- **micro-batch / batch_timeout_ms**: GPU dư ⇒ để timeout thấp (10–25ms) ưu tiên latency; tăng batch nếu muốn vắt throughput.

**Push hay pull? → blocking read (gần như push, không polling).**
- Redis KHÔNG chủ động push. AI worker giữ `XREADGROUP ... BLOCK <ms> COUNT <batch>`; Redis **giao frame ngay khoảnh khắc nó được XADD**. Hiệu ứng = push tức thì, không busy-poll, không tốn CPU chờ.
- **Không cần** pub/sub hay keyspace-notification riêng để "báo có ảnh mới" — Stream + `BLOCK` đã làm việc đó, *cộng* consumer group + ack.
- **Không có bước "id rồi fetch ảnh"** trong hot path: bytes JPEG nằm thẳng trong stream entry, worker nhận trực tiếp khi blocking read trả về. `image_ref`/MinIO chỉ cho bản durable/annotated, không phải đường nóng.
- "Đẩy tức thì" + "gom batch nhỏ" sống chung: `BLOCK` timeout vài ms gom được bao nhiêu frame vừa tới thì infer luôn.

---

## 6. Tracking + Georeferencing (phần nâng "detector" → "hệ giám sát")

> ⚠️ **Áp dụng có giới hạn cho viễn thám (xem §1.5).** Tracking (§6.1–6.3) chỉ còn ý nghĩa ở **lane A** nếu muốn theo dõi khói/đám cháy lan; với nông nghiệp/ngập/sâu bệnh thì **bỏ**. Phần **georef (§6.4)** vẫn dùng cho lane A; lane B thay bằng **orthomosaic**. Phần **dedup (§6.5)** chỉ áp cho lane A real-time.

### 6.1 Vì sao phải TÁCH detection và tracking

- **Detection = stateless** (song song tốt, batch tốt).
- **Tracking = stateful + có thứ tự** (Kalman cần frame liên tiếp đúng thứ tự của *cùng một drone*).
- Consumer group load-balance + micro-batch trộn drone ⇒ **không thể** track trong detection worker.

⇒ Tách **2 stage**: detection (song song, GPU) → tracking (per-drone, in-order, CPU). Tracking nhẹ ⇒ 1 process gánh vài chục drone, mỗi `drone_id` một bộ state riêng. `track_id` toàn cục = prefix `droneX-trk-N`.

### 6.2 Chọn tracker: KHÔNG dùng ByteTrack thuần

Camera bay ⇒ vật đứng yên vẫn "trôi" trong pixel. ByteTrack (giả định camera tĩnh) sẽ đứt track.
- **BoT-SORT + CMC (Camera Motion Compensation)**: bù chuyển động khung bằng optical flow/feature matching. **Lựa chọn đúng cho drone.** (Ultralytics `botsort.yaml`, bật `gmc`.)
- Nếu có IMU/gimbal telemetry → bù bằng số liệu thật (rẻ + chính xác hơn optical flow).

### 6.3 Cú hích khi làm CẢ HAI: georef TRƯỚC, track trong tọa độ mặt đất

> Georeference từng detection thô về **ENU (mét)** *trước*, rồi track trong không gian mặt đất. Chuyển động camera **tự triệt tiêu** (vì tọa độ mặt đất không phụ thuộc camera nhìn từ đâu). CMC trở nên *miễn phí*, và `track_id` mang theo **quỹ đạo GPS thật**.

```
detect (pixel bbox) → georeference (pixel → ENU mét) → track (Kalman trong ENU)
                                                          → track_id + quỹ đạo lat/lon mượt
```

**Tradeoff:** chỉ ăn khi telemetry đủ tốt (RTK + attitude/frame). Telemetry nhiễu → tọa độ nhảy → associate sai ⇒ khi đó track trong pixel (BoT-SORT/CMC) và chỉ *báo cáo* tọa độ thật.

### 6.4 Toán georeferencing (pixel → lat/lon)

**Đầu vào bắt buộc:**
- GPS drone: `lat, lon, alt` (nên là AGL — trên mặt đất)
- Tư thế gimbal/camera: `yaw, pitch, roll` (hệ thế giới)
- Nội tham số camera `fx, fy, cx, cy` (+ hệ số méo) — **calibrate 1 lần, gửi static**, không per-frame
- Giả định mặt đất: phẳng (flat-earth) hoặc DEM nếu địa hình dốc

**Chuỗi biến đổi (pinhole, ENU = East-North-Up):**
1. Pixel → tia camera: `d_cam = normalize([(u−cx)/fx, (v−cy)/fy, 1])`
2. Xoay sang hệ thế giới: `d_world = R(yaw,pitch,roll) · d_cam`
3. Drone ở `P = (0,0,h)`. Cắt mặt đất `z=0`: `t = −h / d_world.z` (cần `d_world.z < 0`)
4. Điểm mặt đất ENU: `G = (t·d_world.x, t·d_world.y, 0)` mét
5. Đổi ra lat/lon: `dlat = North / R_earth`, `dlon = East / (R_earth·cos(lat))`, cộng vào GPS drone

**Độ chính xác thực tế (đừng hứa hão):**
- Sai số **góc gimbal là kẻ thù chính**: 1° lệch ở 100m, góc chếch → vài mét sai.
- **Góc chếch gần phương ngang cực nguy hiểm**: `d_world.z → 0` ⇒ `t → ∞`, sai số nhỏ → hàng chục mét. ⇒ Chỉ tin vùng gần nadir; cảnh báo độ tin cậy thấp khi bbox gần đường chân trời.
- Nadir + telemetry tốt → vài mét. RTK + camera calibrated + IMU đồng bộ → < 1m.
- DJI Enterprise nhúng sẵn gimbal attitude per-frame (SRT/onboard SDK).

### 6.5 Lọc frame trùng / chọn frame tốt nhất — DEDUP Ở ĐẦU RA, không ở đầu vào

**Bẫy cần tránh:** ở 10 fps, GPS các frame gần như giống nhau. Trực giác "cùng tọa độ → drop bớt" **nguy hiểm** vì tọa độ đó là của **drone**, không phải của **cảnh**. Drone đứng yên nhưng người/xe vẫn đi qua khung hình ⇒ drop theo GPS có thể **drop đúng frame mục tiêu xuất hiện**.

**Quy tắc:**
- **Đầu vào**: KHÔNG lọc theo GPS. GPU dư ⇒ detect mọi frame còn sống (sau ring-buffer). RAM Redis đã có trần nhờ `MAXLEN`, ảnh durable nằm MinIO chứ không Redis ⇒ không phình.
- **Đầu ra**: dedup bằng `track_id` + cửa sổ thời gian. Cùng một vật thể qua N frame = **1 sự kiện**, không phải N. Đây là lúc `track_id` trả công.
- **Chọn "frame tốt nhất" trong cửa sổ để lưu bằng chứng**: tiêu chí rẻ + chuẩn = **variance of Laplacian** (độ nét, cao = nét), hoặc frame có conf/số detection cao nhất. Chỉ frame đó mới encode + lên MinIO.
- ⇒ Metadata vẫn ra đều (BE biết vật thể tồn tại liên tục), nhưng ảnh lưu + alert chỉ 1/sự kiện. Không bỏ sót mục tiêu di chuyển, không phình storage.

**Ngoại lệ — cảnh thật sự tĩnh** (treo quan sát 1 điểm): lọc đầu vào hợp lý, nhưng làm bằng **motion-gate ở drone** (đứng yên thì hạ 1–2 fps, có chuyển động mới bắn full) — tối ưu *bandwidth ở drone*, KHÔNG so GPS ở server.

**Lưu ý:** dedup mạnh tay xung đột với độ mượt của tracking (tracking thích frame liên tục). Vì vậy dedup chỉ áp ở **tầng lưu/alert**, không áp ở tầng infer/track.

---

## 7. Thiết kế thích ứng theo telemetry (graceful degradation)

> Drone chưa chốt ⇒ thiết kế để **tự hạ cấp** theo telemetry có sẵn, contract cố định. Khi drone chốt chỉ bật/tắt cờ.

| Telemetry có | Georef | Track ở đâu | Camera motion | `accuracy_m` |
|---|---|---|---|---|
| **RTK + attitude/frame** | đầy đủ (pinhole) | **ENU (mặt đất)** | tự triệt tiêu | ~1m |
| **GPS thường + attitude** | đầy đủ | ENU *hoặc* pixel (tùy nhiễu) | free nếu track ENU | vài m |
| **chỉ GPS** | nadir thô / bỏ qua | **pixel** (BoT-SORT+CMC) | bù optical flow | null |

**3 nguyên tắc giữ độc lập drone:**
1. Envelope luôn có chỗ cho `attitude` + `geo`, chỉ là **nullable**. Tier thấp gửi `null` → stage tự rơi xuống nhánh track-pixel.
2. Hai chiến lược track sau **interface chung** `update(detections) -> tracks`: `WorldTracker` (Kalman ENU) và `PixelTracker` (BoT-SORT/CMC). Một cờ config chọn impl. Detection worker không quan tâm.
3. `geo` luôn trong schema output, không georef được thì `null`. BE viết một lần.

---

## 8. Hợp đồng message (contract)

### Request stream (`req`)
```jsonc
{ "frame_id": "drone1-000123",
  "drone_id": "drone1",
  "ts": 1719000000.123,
  "gps": { "lat": 21.0204, "lon": 105.8412, "alt_agl": 80.0 },
  "attitude": { "yaw": 12.5, "pitch": -78.0, "roll": 0.3 },  // nullable (tier thấp = null)
  "image_ref": "<jpeg bytes trong stream>" }
// camera intrinsics: gửi static 1 lần lúc đăng ký drone, KHÔNG per-frame
```

### Result stream (`res`)
```jsonc
{ "frame_id": "drone1-000123",
  "drone_id": "drone1",
  "gps": { "lat": 21.0204, "lon": 105.8412, "alt_agl": 80.0 },
  "detections": [
    { "track_id": "drone1-trk-42",
      "cls": "person", "conf": 0.91,
      "bbox": [x, y, w, h],
      "geo": { "lat": 21.0207, "lon": 105.8419, "accuracy_m": 4.2 } }  // nullable
  ],
  "annotated_ref": null }   // điền khi có ảnh vẽ box trên MinIO
```

`accuracy_m` đáng trả: vận hành viên cần biết tọa độ tin được bao nhiêu (suy từ góc chếch + độ cao + chất lượng telemetry).

---

## 9. Đường lên Edge (Jetson) sau này

- Worker detection **stateless** + giao tiếp qua contract cố định ⇒ bê nguyên worker lên Jetson, `res` stream + BE **không đổi**.
- Inference làm ngay trên drone, đẩy thẳng metadata (đã rất nhẹ) về.
- Server mạnh = "lò luyện": fine-tune YOLO lớn → quantize/prune (INT8/FP4) → bản nhẹ deploy Jetson.

---

## 10. Việc làm được NGAY (không cần chờ drone chốt)

**Chung 2 lane:**
- [ ] **Calibrate camera** (bảng cờ vua + OpenCV `calibrateCamera`) → `fx,fy,cx,cy` + hệ số méo. Độc lập drone.
- [ ] **Chốt convention hệ tọa độ sớm**: chuẩn yaw/pitch/roll, gốc tọa độ, `alt` là AGL hay MSL.
- [ ] Dựng end-to-end với drone giả lập (publisher đọc ảnh/video → XADD).
- [ ] **Định nghĩa envelope `bands`** (RGB / multispectral / thermal) để sensor-agnostic.

**Lane A (cháy rừng, real-time):**
- [ ] Baseline detect/segment khói-lửa trên RGB; ngưỡng hotspot trên nhiệt (nếu có).
- [ ] **Viết hàm georef thuần** (pixel + gps + attitude + K → lat/lon + accuracy).
- [ ] Export model → TensorRT engine.

**Lane B (nông nghiệp/ngập/sâu bệnh, batch):**
- [ ] **Pipeline orthomosaic** thử nghiệm với OpenDroneMap (ảnh chồng lấn + GPS → bản đồ định vị).
- [ ] **Tính NDVI/NDWI** trên mosaic (cần ảnh multispectral mẫu; chưa có thì test bằng dataset công khai).
- [ ] Baseline **segmentation** (U-Net/DeepLab/SegFormer) cho 1 lớp đơn giản (vd vùng ngập từ RGB).
- [ ] Dựng **temporal geo-store** (PostGIS) lưu lớp định vị theo thời gian.

---

## 11. Còn nợ — sẽ đào tiếp

- **Logic phân tích → cảnh báo/sự kiện**: lane A — khi nào hotspot/khói thành alert (ngưỡng + tồn tại qua N frame, chống spam). Lane B — ngưỡng diện tích/% vùng stress để báo.
- **Change detection chi tiết**: so mosaic/NDVI giữa các chuyến bay (đăng ký ảnh, chuẩn hóa chiếu sáng, ngưỡng biến động).
- **Ground-truthing / nhãn dữ liệu**: viễn thám cần nhãn vùng đúng — sâu bệnh khó nhất, cần khảo sát thực địa đối chứng.
- **Hiệu chuẩn phổ (radiometric calibration)**: tấm phản xạ chuẩn để NDVI nhất quán giữa các chuyến/điều kiện sáng.
- **Ngân sách latency** (lane A) + **thời gian xử lý mosaic** (lane B, có thể hàng chục phút/chuyến).
- **Observability**: metrics throughput, lag consumer (lane A); tiến độ job mosaic/seg (lane B).
