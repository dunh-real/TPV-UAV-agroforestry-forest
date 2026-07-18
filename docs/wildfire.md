# Wildfire — dataset, phương pháp huấn luyện, cách tái tạo

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
6. Thí nghiệm **v3-data** (2026-07-18, cân bằng lớp): thêm subset có-fire của Kaggle
   roscoekerby/firesmoke-detection-yolo-v9 (lọc bỏ 2.504 ảnh bật lửa indoor sai domain,
   remap 0=fire,1=smoke → chuẩn ta) → train 28.464, smoke/fire 18.528/18.264 (~50/50).
   Train v3 (fine-tune từ v2, SGD lr0=0.0005, 10 epochs — LƯU Ý: `optimizer=auto` sẽ
   override lr thành AdamW 0.00167 làm mAP sụp, phải chỉ định SGD tường minh):
   test đóng băng all 0.741 / smoke 0.806 / fire 0.677 — **thua v1** (0.752/0.818/0.686)
   ~1 điểm mọi lớp; frame drone thực tế conf thấp hơn hẳn (fire 0.20 vs 0.46).
   Trên cùng test, v2 fire ≈ v1 fire (0.682 vs 0.686). KẾT LUẬN: **prod dùng v1**
   (fire event conf hạ 0.5→0.4); checkpoint v2/v3 lưu `runs/detect/runs/`. Ngõ cụt đã
   thử: FLAME/FLAME2/FLAME3 (không có bbox — pseudo-label bằng model đang yếu fire là
   phản tác dụng), FlamEye (dataset gộp, 99% trùng nguồn đã có). Data CCTV/cận cảnh kéo
   model lệch domain drone — vòng sau ưu tiên DATA THỰC TẾ từ pipeline (uncertain/ +
   blackbox/ trên MinIO, gắn nhãn người duyệt, trộn replay 20-30% data cũ) hoặc FASDD aerial.
   `SOURCES` trong prepare_wildfire_data.py đã comment pyro/flameye/roscoe — dataset hiện
   hành = cấu hình v1; raw đã xử lý còn ở `data/wildfire/raw/` để bật lại khi cần.

Tái tạo: `.venv/bin/python scripts/prepare_wildfire_data.py` (idempotent, xóa dựng lại `data/wildfire/dataset`).

## Phương pháp huấn luyện — kỹ thuật đã dùng và hiệu quả (v1 + v2)

Các kỹ thuật xuyên suốt cả hai vòng, đã kiểm chứng bằng số liệu:

1. **Transfer learning bậc thang**: COCO-pretrained → v1 (60 epochs, học domain khói/lửa)
   → v2 (fine-tune 15 epochs trên data mở rộng). Mỗi vòng xuất phát từ checkpoint hội tụ
   của vòng trước, không train from scratch → rẻ và ổn định.
2. **lr thấp khi fine-tune tiếp** (`lr0=0.001`, giảm 10x ở v2): chống catastrophic forgetting.
   Bằng chứng: data v2 lệch mạnh về smoke (67/33 box) nhưng fire mAP50 test chỉ −0.4 điểm,
   fire trên val mới còn +0.7 — đặc trưng fire của v1 được bảo toàn.
3. **`imgsz=960`** (thay 640 mặc định): khói sớm nhỏ/xa là mục tiêu chính — box median
   Pyro-SDIS chỉ 0,14% diện tích ảnh, 640 sẽ miss. Khớp `IMGSZ` của worker (train-serve
   cùng resolution). Đo được 640 nhanh hơn ~1.18× nhưng mất ~10% vật nhỏ → không đáng.
4. **`hsv_s=0.5`** (hạ từ 0.7 mặc định): màu là đặc trưng phân lớp chính của lửa —
   jitter saturation mạnh phá tín hiệu học.
5. **Ảnh âm tính nhiều chủ đích** (v1: 38%, v2: 26% train): mây/sương/cảnh trống dạy model
   "im lặng", chống false-positive từ data thay vì chỉ dựa ngưỡng lúc serve.
6. **Dedup pHash chống leakage** (hamming≤4, ưu tiên giữ val/test, loại trùng khỏi train):
   nguồn toàn chuỗi frame camera cố định, không dedup thì val/test "nhìn thấy" train →
   số ảo. Bằng chứng sạch: v1 test ≈ val (0.752/0.756).
7. **Test set đóng băng** (3.058 ảnh, không đổi từ v1): mọi vòng model so trên cùng thước —
   nguồn mới (Pyro-SDIS không có split test) chỉ vào train/val.
8. **Scale epochs theo effective data**: v2 chỉ 15 epochs vì dataset 2,4x → 1 epoch mới
   ≈ 2,4 epoch cũ về số gradient step; kèm `patience` làm lưới an toàn. Thực tế val mAP50
   còn tăng đến epoch cuối (0.734) — vòng sau có thể +5 epochs.
9. **Đánh giá đúng cái mình mua**: thêm data domain mới thì đo cả trên **test cũ** (kiểm
   không thoái lui) lẫn **val chứa domain mới** (kiểm phần được thêm) — v2 test cũ đi ngang
   nhưng smoke trên val Pyro-SDIS +22,5 điểm (0.532 → 0.757); chỉ nhìn test cũ sẽ kết luận
   sai là "data mới vô dụng".
10. **Mosaic tự tắt 10 epochs cuối** (ultralytics `close_mosaic=10` mặc định — giữ nguyên):
    quan sát cú bật mAP rõ khi mosaic tắt (v2 epoch 11: +3.8 điểm).

Hạn chế phần cứng: **DDP 2 GPU bị SIGILL** trên máy này (ultralytics DDP + torch 2.12;
NCCL thuần all_reduce OK) → train 1 GPU, GPU còn lại phục vụ pipeline.

## Lệnh train

v1 (từ COCO-pretrained, 2026-07-04, ~3h):
```bash
.venv/bin/python .venv/bin/yolo detect train model=weights/yolo26x.pt data=data/wildfire/wildfire.yaml \
  epochs=60 imgsz=960 batch=32 device=0 workers=12 hsv_s=0.5 patience=20 \
  project=runs name=wildfire_y26x_960
```

v2 (fine-tune từ checkpoint v1 trên dataset +Pyro-SDIS, 2026-07-16, ~2h):
```bash
.venv/bin/python .venv/bin/yolo detect train model=runs/detect/runs/wildfire_y26x_960/weights/best.pt \
  data=data/wildfire/wildfire.yaml epochs=15 imgsz=960 batch=32 device=0 workers=12 \
  lr0=0.001 hsv_s=0.5 patience=10 project=runs name=wildfire_v2_y26x_960
```

Chạy qua `.venv/bin/python .venv/bin/yolo` (shebang `.venv/bin/yolo` còn trỏ path cũ
`Remote-Sensing`); settings ultralytics (`runs_dir`…) đã sửa về `wildfire-remote-sensing`.
Kết quả test/val chi tiết: xem README.

## Sau train

1. Eval test: `yolo detect val model=runs/detect/.../best.pt data=data/wildfire/wildfire.yaml split=test`
   — so per-class với vòng trước trên test đóng băng.
2. Export TensorRT FP16: `yolo export model=.../best.pt format=engine half=True imgsz=960 device=0`
   → copy vào `weights/`, smoke-test engine trên `samples/vn_fire_test/`.
3. Trỏ worker qua **`.env`** (prod hiện tại): `MODEL_PATH=weights/wildfire_v2_y26x.engine
   MODEL_VERSION=wildfire-v2 CONF=0.2 EVENT_CLASSES=smoke,fire EVENT_CONF_CLS="smoke:0.3,fire:0.5"`
   - `CONF` thấp hơn ngưỡng sự kiện để tầng B (`uncertain/`) hứng frame nghi ngờ → mỏ
     hard-negative/positive cho vòng fine-tune kế (active learning từ data thật).
4. Verify ảnh thật: `scripts/verify_vn.py <model> <tag>` (ảnh BE gửi vào `data/vn_verify/<nhóm>/`).
5. Đo **false-alarm rate/giờ** trên video không cháy (mây, hoàng hôn, sương) — metric chính
   của hệ cảnh báo, không chỉ mAP. Luật gom N-frame/T-giây là việc BE.
