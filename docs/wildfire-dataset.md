# Dataset cháy rừng — nguồn, chuẩn hóa, cách tái tạo

Schema nhãn hợp nhất: **`0: smoke, 1: fire`** (file `data/wildfire/wildfire.yaml`).
Lớp `other` (DFS) sẽ thêm sau khi tải được DFS — hiện chống false-positive bằng ảnh âm tính.

## Nguồn đã dùng

| Nguồn | Ảnh gốc | Nhãn | Cách tải |
|---|---|---|---|
| D-Fire (mirror Kaggle `sayedgamal99/smoke-fire-detection-yolo`) | 21.527 | smoke, fire + ~9.8k âm tính | `kagglehub` (đã cache `~/.cache/kagglehub`) |
| WSDY (`ahemateja19bec1025/wildfiresmokedatasetyolo`) | 737 | smoke (mirror HPWREN/Roboflow, license **CC BY-NC-SA** — chú ý nếu thương mại) | `kagglehub` |
| DFS (9.462 ảnh, có lớp `other`: nắng/đèn/vật sáng) | — | **CHƯA TẢI ĐƯỢC** — OneDrive/Baidu chặn CLI (403). Tải tay: https://github.com/siyuanwu/DFS-FIRE-SMOKE-Dataset | trình duyệt |
| Boreal Forest Fire (UAV Phần Lan, smoke bbox+mask) | 4.954 | khuyến nghị thêm cho domain drone: https://www.nature.com/articles/s41597-025-05634-0 | tay |
| **Pyro-SDIS** (HF `pyronear/pyro-sdis`, **Apache-2.0** — thương mại OK) | 33.636 | smoke (mọi class id nguồn → 0); camera tháp canh Pháp SDIS/Force06, khói sớm box rất nhỏ (median 0,14% diện tích ảnh), 1280×720, ~16% âm tính mây/sương. Không có split test → test set giữ nguyên, so sánh các vòng model công bằng | `scripts/download_pyro_sdis.py` |

Nguồn đã cân nhắc nhưng không dùng (2026-07): FASDD 120k (SciDB TQ, chặn tải CLI như DFS),
FIgLib 24.8k (thiên classification chuỗi frame, không bbox chuẩn YOLO), Nemo (nhỏ, trùng AlertWildfire).

## Chuẩn hóa (scripts/prepare_wildfire_data.py)

1. Validate nhãn: parse, class id hợp lệ, clamp [0,1], loại bbox rỗng (loại 11 box hỏng).
2. Re-encode toàn bộ JPEG (q95) — nguồn có ~21 JPEG truncated làm crash scan DDP.
3. **Dedup pHash** hamming≤4 toàn corpus, ưu tiên giữ val/test → loại bản trùng khỏi train
   (chống leakage). D-Fire chứa rất nhiều chuỗi frame camera cố định (đã xác minh bằng mắt:
   cùng camera, cách nhau vài phút) → loại ~34% ảnh trùng là ĐÚNG, không phải mất data.
   Pyro-SDIS cũng là chuỗi frame timestamped → dedup loại mạnh tương tự.
4. Kết quả **v1** (D-Fire+WSDY): train 9.027 (3.425 âm tính) / val 2.675 / test 3.058; boxes train: 5.869 smoke, 8.335 fire.
5. Kết quả **v2-data** (thêm Pyro-SDIS, 2026-07-16): **train 21.563 (5.677 âm tính) / val 4.357
   / test 3.058 (KHÔNG đổi — so sánh model các vòng trên cùng test)**; boxes train: 17.342 smoke, 8.335 fire.
   Đã verify nguồn Pyro-SDIS: 0 box lỗi, 0 ảnh hỏng (sample 300), đồng nhất 1280×720.

Tái tạo: `.venv/bin/python scripts/prepare_wildfire_data.py` (idempotent, xóa dựng lại `data/wildfire/dataset`).

## Train

v1 (from scratch từ COCO-pretrained):
```bash
.venv/bin/yolo detect train model=weights/yolo26x.pt data=data/wildfire/wildfire.yaml \
  epochs=80 imgsz=960 batch=32 device=0 workers=12 hsv_s=0.5 patience=20 \
  project=runs name=wildfire_y26x_960
```

v2 (fine-tune từ checkpoint v1 trên dataset +Pyro-SDIS — đã chạy 2026-07-16, 2.1h/1 GPU):
```bash
.venv/bin/python .venv/bin/yolo detect train model=runs/detect/runs/wildfire_y26x_960/weights/best.pt \
  data=data/wildfire/wildfire.yaml epochs=15 imgsz=960 batch=32 device=0 workers=12 \
  lr0=0.001 hsv_s=0.5 patience=10 project=runs name=wildfire_v2_y26x_960
```
- `epochs=15` fine-tune từ checkpoint hội tụ + dataset 2,4x → 1 epoch mới ≈ 2,4 epoch cũ.
  Thực tế val mAP50 còn tăng đến epoch cuối (0.734) — vòng sau có thể +5 epochs.
- `lr0=0.001` (giảm 10x): không phá đặc trưng v1 đã học. Fire mAP giữ nguyên dù data lệch
  về smoke (67/33) — xác nhận fine-tune lr thấp không gây quên lớp cũ.
- Chạy qua `.venv/bin/python .venv/bin/yolo` (shebang của `.venv/bin/yolo` còn trỏ path cũ
  `Remote-Sensing`); settings ultralytics (`runs_dir`…) đã sửa về `wildfire-remote-sensing`.
- Kết quả test/val chi tiết: xem README.

- `imgsz=960`: khói sớm nhỏ/xa, 640 sẽ miss — khớp `IMGSZ` mặc định của worker.
- `hsv_s=0.5` (hạ từ 0.7): màu là đặc trưng chính của lửa, đừng jitter mạnh.
- **DDP 2 GPU bị SIGILL** trên máy này (ultralytics DDP + torch 2.12; DDP thuần NCCL chạy tốt
  — đã test all_reduce OK). Train 1 GPU, GPU còn lại phục vụ pipeline.

## Sau train

1. Eval test: `yolo detect val model=runs/detect/.../best.pt data=data/wildfire/wildfire.yaml split=test`
2. Export TensorRT FP16: `yolo export model=.../best.pt format=engine half=True imgsz=960 device=0`
3. Trỏ worker qua env: `MODEL_PATH=weights/wildfire_y26x.engine MODEL_VERSION=wildfire-v1
   EVENT_CLASSES=smoke,fire EVENT_CONF_CLS="smoke:0.3,fire:0.5" CONF=0.15`
   - `CONF` thấp để tầng B (`uncertain/`) hứng frame nghi ngờ → mỏ hard-negative/positive
     cho vòng fine-tune kế (active learning từ data thật).
4. Đo **false-alarm rate/giờ** trên video không cháy (mây, hoàng hôn, sương) — metric chính
   của hệ cảnh báo, không chỉ mAP. Luật gom N-frame/T-giây là việc BE.
