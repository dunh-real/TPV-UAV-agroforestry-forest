# BE Integration Test Guide

Tài liệu này mô tả những việc Backend (BE) cần làm để test hoàn chỉnh luồng tích hợp với AI server qua Redis Streams và MinIO.

---

## 0. Kết nối Redis

BE và AI dùng chung một Redis.

```env
REDIS_HOST=100.104.42.121
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=<trong .env>
```

Redis đang chạy qua Tailscale.

BE **không cần tạo stream/group** vì AI đã tạo sẵn:

| Stream | Consumer group | Mục đích |
|---|---:|---|
| `dms:captures:ready` | `cg:ai` | BE đẩy frame input cho AI |
| `dms:ai:results` | `cg:be` | BE nhận kết quả AI |

---

## 1. Đẩy input vào AI

BE đẩy mỗi frame bằng lệnh:

```redis
XADD dms:captures:ready MAXLEN ~ 2000 * ...
```

Khuyến nghị luôn dùng `MAXLEN ~ 2000` để Redis không phình bộ nhớ.

Mỗi frame là **một entry** gồm đúng **5 field**. Value trên wire là bytes nhị phân, **không bọc base64**, **không bọc JSON**.

| Field | Định dạng trên wire | Bắt buộc | Ghi chú |
|---|---|---:|---|
| `frame_id` | Chuỗi UTF-8, duy nhất mỗi frame | ✅ | Đây sẽ thành `captureId` ở output; dùng cho idempotency |
| `drone_id` | Chuỗi UTF-8 | ✅ | ID drone gửi frame |
| `ts` | Chuỗi số thập phân epoch giây, ví dụ `"1719374400.123456"` | ✅ | Không phải int ms, không phải số nhị phân. AI parse sang `float` để đo latency |
| `meta` | MessagePack của object `{"gps": {...}, "attitude": {...} hoặc null, "bands": "RGB"}` | ✅ | `gps`/`attitude` có thể rỗng nhưng field phải có |
| `img` | Bytes JPEG nguyên gốc | ✅ | Không re-encode, không base64 |

Ví dụ object `meta` trước khi pack bằng MessagePack:

```json
{
  "gps": {
    "lat": 21.0278,
    "lon": 105.8342,
    "alt": 120.5
  },
  "attitude": null,
  "bands": "RGB"
}
```

Sau đó BE cần pack object này bằng MessagePack trước khi đưa vào field `meta`.

---

## 2. Ba lỗi BE hay mắc cần tránh

### Lỗi 1: Gửi `ts` sai định dạng

Sai:

```text
1719374400123
```

hoặc gửi dạng binary number.

Đúng:

```text
"1719374400.123456"
```

`ts` phải là string epoch giây có phần thập phân. Nếu gửi int milliseconds hoặc số binary thì latency sẽ sai hoặc AI parse lỗi.

---

### Lỗi 2: Gửi `img` dạng base64

Sai:

```text
/9j/4AAQSkZJRgABAQ...
```

Đúng:

```text
<raw JPEG bytes>
```

`img` phải là bytes JPEG nguyên gốc. Nếu base64 ảnh, AI sẽ decode hỏng và frame có thể bị đưa vào dead-letter.

---

### Lỗi 3: Gửi `meta` dạng JSON string

Sai:

```json
"{\"gps\": {}, \"attitude\": null, \"bands\": \"RGB\"}"
```

Đúng:

```text
<msgpack bytes>
```

`meta` bắt buộc phải là MessagePack, không phải JSON string.

---

## 3. Nhận output từ AI

BE đọc kết quả bằng:

```redis
XREADGROUP GROUP cg:be <ten-consumer-BE> COUNT <n> BLOCK <ms> STREAMS dms:ai:results >
```

Ví dụ:

```redis
XREADGROUP GROUP cg:be be-worker-01 COUNT 10 BLOCK 5000 STREAMS dms:ai:results >
```

Mỗi entry trả về các field sau:

| Field | Định dạng | Nội dung |
|---|---|---|
| `captureId` | String | Bằng `frame_id` BE đã gửi, dùng để khớp request |
| `droneId` | String | ID drone |
| `status` | String | `ok` hoặc `error` |
| `result` | MessagePack | Object kết quả AI |
| `evidenceKey` | String | Key ảnh annotated trên MinIO, hoặc `""` nếu không có sự kiện |
| `err` | String | Lý do lỗi khi `status=error` |

---

## 4. Format `result`

Khi `status = ok`, field `result` sau khi unpack MessagePack có dạng:

```json
{
  "hasDetection": true,
  "detections": [
    {
      "type": "person",
      "confidence": 0.91,
      "bbox": [120, 80, 240, 360]
    }
  ],
  "model": "yolo26x",
  "modelVersion": "coco-pretrained",
  "ts": 1719374400.123456
}
```

Ý nghĩa các field:

| Field | Kiểu dữ liệu | Ý nghĩa |
|---|---|---|
| `hasDetection` | `bool` | Có phát hiện sự kiện/object hay không |
| `detections` | `array` | Danh sách object được phát hiện |
| `detections[].type` | `string` | Tên lớp phát hiện |
| `detections[].confidence` | `float` | Độ tin cậy |
| `detections[].bbox` | `[x, y, w, h]` | Bounding box theo pixel, tính từ góc trên-trái, kèm chiều rộng/cao |
| `model` | `string` | Tên model, hiện tại là `"yolo26x"` |
| `modelVersion` | `string` | Phiên bản model, hiện tại là `"coco-pretrained"` |
| `ts` | `float` | Bằng `ts` BE đã gửi, dùng để đối chiếu và đo độ trễ |

---

## 5. XACK sau khi xử lý

Sau khi xử lý xong từng entry từ `dms:ai:results`, BE **bắt buộc** gọi:

```redis
XACK dms:ai:results cg:be <entry-id>
```

Nếu không `XACK`, entry sẽ bị kẹt ở pending list, sau đó có thể bị reclaim và bị tính là chưa xử lý.

Quy tắc xử lý khuyến nghị:

1. `XREADGROUP` lấy entry.
2. Unpack field `result`.
3. Log hoặc lưu database.
4. Nếu cần, lấy ảnh annotated từ MinIO bằng `evidenceKey`.
5. Sau khi xử lý thành công, gọi `XACK`.

---

## 6. Lấy ảnh bằng chứng từ MinIO

Ảnh annotated **không nằm trong Redis**.

Nếu cần hiển thị ảnh bằng chứng, BE dùng `evidenceKey` để GET object từ MinIO.

```env
MINIO_ENDPOINT=https://s3.blogscloud.click
MINIO_BUCKET=images
MINIO_ACCESS_KEY=<trong .env>
MINIO_SECRET_KEY=<trong .env>
```

Quy tắc:

- Chỉ frame có sự kiện, tức `hasDetection = true`, mới có `evidenceKey`.
- Nếu không có sự kiện, `evidenceKey` sẽ là chuỗi rỗng `""`.
- Nhiều frame cùng `(drone, lớp)` trong cửa sổ dedup có thể trỏ về cùng một key ảnh nét nhất.
- Việc nhiều frame cùng trỏ một `evidenceKey` là bình thường, không phải lỗi.

---

## 7. Checklist test hoàn chỉnh

### Bước 1: Kiểm tra kết nối Redis

- Kết nối Redis prod.
- Kiểm tra `PING` trả về `PONG`.

---

### Bước 2: Chạy vòng đọc kết quả nền

BE cần chạy consumer đọc kết quả:

```redis
XREADGROUP GROUP cg:be <ten-consumer-BE> COUNT <n> BLOCK <ms> STREAMS dms:ai:results >
```

Sau khi đọc:

- Unpack `result`.
- Log hoặc lưu kết quả.
- Gọi `XACK`.

---

### Bước 3: Đẩy N frame input

Đẩy N frame vào stream:

```redis
XADD dms:captures:ready MAXLEN ~ 2000 * frame_id <id> drone_id <drone> ts <epoch_seconds_string> meta <msgpack_bytes> img <jpeg_bytes>
```

Yêu cầu:

- Mỗi frame có `frame_id` khác nhau.
- `ts` là thời điểm gửi, định dạng epoch giây dạng string có phần thập phân.
- `meta` là MessagePack.
- `img` là JPEG bytes nguyên gốc.

---

### Bước 4: Kiểm tra kết quả trả về

Với mỗi `frame_id` đã gửi, BE cần nhận đúng một entry ở `dms:ai:results` có:

```text
captureId = frame_id
```

Nếu `status = ok`, kiểm tra field `result`.

Nếu `status = error`, đọc field `err` để xác định nguyên nhân.

---

### Bước 5: Kiểm tra ảnh evidence

Nếu:

```json
"hasDetection": true
```

thì kiểm tra:

```text
evidenceKey != ""
```

Sau đó BE dùng `evidenceKey` để GET ảnh annotated từ MinIO bucket `images`.

---

### Bước 6: Đo độ trễ

Độ trễ glass-to-glass có thể đo bằng:

```text
now - result.ts
```

Trong đó:

- `now` là thời điểm BE nhận và xử lý output.
- `result.ts` là timestamp BE đã gửi trong input.

Kỳ vọng hiện tại:

```text
p50 ~ 20ms ở tải 4 drone
```

---

## 8. Cách BE tự xác minh đường ống thông

Không cần đợi AI trả lời logic đầy đủ, BE có thể kiểm tra pipeline bằng Redis Stream length.

Sau khi `XADD` vào input stream:

```redis
XLEN dms:captures:ready
```

Kỳ vọng:

- `XLEN dms:captures:ready` tăng sau khi BE đẩy frame.
- Sau đó giảm về `0` hoặc gần `0` khi AI worker đã nuốt frame.

Kiểm tra output stream:

```redis
XLEN dms:ai:results
```

Kỳ vọng:

- `XLEN dms:ai:results` tăng tương ứng với số frame AI đã xử lý.

Lưu ý: nếu BE dùng consumer group và đã `XACK`, stream length vẫn có thể còn entry tuỳ chính sách trim. Vì vậy nên kiểm tra thêm việc BE có nhận đủ `captureId` tương ứng với các `frame_id` đã gửi hay không.

---

## 9. Luồng tổng thể

```text
BE
 │
 │ XADD dms:captures:ready
 ▼
Redis Stream input
 │
 │ AI consumer group cg:ai đọc frame
 ▼
AI Worker
 │
 │ xử lý ảnh + ghi ảnh annotated vào MinIO nếu có detection
 │
 │ XADD dms:ai:results
 ▼
Redis Stream output
 │
 │ BE consumer group cg:be đọc kết quả
 ▼
BE
 │
 │ unpack result + lưu/log + lấy evidenceKey nếu cần
 │
 │ XACK dms:ai:results cg:be <entry-id>
 ▼
Done
```

---

## 10. Tóm tắt yêu cầu bắt buộc

BE cần đảm bảo:

1. Dùng đúng Redis host, port, db và password trong `.env`.
2. Không tự tạo stream/group vì AI đã tạo sẵn.
3. Đẩy input vào `dms:captures:ready` với đúng 5 field.
4. `frame_id` là duy nhất mỗi frame.
5. `ts` là string epoch giây dạng thập phân.
6. `meta` là MessagePack.
7. `img` là JPEG raw bytes.
8. Đọc output từ `dms:ai:results` bằng `XREADGROUP GROUP cg:be`.
9. Unpack `result` bằng MessagePack.
10. Sau khi xử lý xong, bắt buộc `XACK`.
11. Nếu có `evidenceKey`, lấy ảnh annotated từ MinIO bucket `images`.
12. Với mỗi frame đã gửi, cần nhận đúng một output có `captureId = frame_id`.

---

## 11. Ghi chú trạng thái hiện tại

AI server đang chạy idle và sẵn sàng nhận frame test.

BE có thể test bằng một trong hai cách:

1. Tự đẩy frame thật từ pipeline BE.
2. Dùng producer mô phỏng để bắn vài frame mẫu vào `dms:captures:ready`, sau đó soi format output thật trong `dms:ai:results`.
