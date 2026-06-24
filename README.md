# Remote Sensing — Drone AI Pipeline

Dự án triển khai thực tế: drone **viễn thám** (cháy rừng, ngập lụt, hạn hán / sức khỏe cây trồng, sâu bệnh) → Redis → AI → Backend.

## Tài liệu thiết kế

- [Kiến trúc tổng thể](docs/drone-yolo-pipeline-architecture.md) — pipeline 2 lane (real-time cảnh báo + khảo sát/mosaic), GPU, tracking/georef, segmentation + chỉ số phổ. Đọc **§1.5 reframe viễn thám** trước.
- [Đặc tả Giao tiếp & Truyền tải](docs/communication-transport-spec.md) — tầng kết nối Redis Streams giữa các bên, độc lập model. Build/test được ngay với worker echo (xem §11).

## Trạng thái

- [x] Chốt kiến trúc tổng + tầng transport
- [ ] Dựng transport end-to-end với producer giả lập + worker echo + BE consumer
- [ ] Tầng AI (chờ dữ liệu train)
