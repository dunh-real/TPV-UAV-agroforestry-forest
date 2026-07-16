#!/usr/bin/env python3
"""Verify model trên bộ ảnh thật VN (data/vn_verify/) — sanity check trước khi đóng gói.

Ảnh do BE/hiện trường cung cấp — bỏ vào data/vn_verify/<nhóm>/ rồi chạy. 4 nhóm:
  fire_smoke : cháy/khói rừng thật    -> PHẢI báo (detection rate)
  burn_field : khói đốt đồng/rơm rạ   -> model báo "smoke" là đúng lớp;
               việc phân biệt cháy rừng vs đốt đồng là của tầng cảnh báo/BE (vị trí camera)
  fog        : sương mù/mây núi       -> KHÔNG được báo (false-alarm)
  sunset     : hoàng hôn/ngược sáng   -> KHÔNG được báo (false-alarm)

Ngưỡng cảnh báo dùng đúng cấu hình prod: smoke>=0.3, fire>=0.5 (EVENT_CONF_CLS).
Ảnh vẽ box lưu vào runs/vn_verify/<tag>_<nhóm>/.

Chạy: .venv/bin/python scripts/verify_vn.py [model.pt|engine] [tag]
Mặc định: weights/wildfire_y26x.pt, tag=v1
"""
import sys
from pathlib import Path
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent.parent
MODEL = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "weights" / "wildfire_y26x.pt")
TAG = sys.argv[2] if len(sys.argv) > 2 else "v1"
ALERT = {"smoke": 0.3, "fire": 0.5}  # khớp EVENT_CONF_CLS prod
EXPECT_ALERT = {"fire_smoke": True, "burn_field": True, "fog": False, "sunset": False}

m = YOLO(MODEL, task="detect")
summary = []
for cat, want in EXPECT_ALERT.items():
    d = ROOT / "data" / "vn_verify" / cat
    imgs = sorted(p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")) if d.exists() else []
    if not imgs:
        continue
    res = m.predict(imgs, imgsz=960, conf=0.15, verbose=False,
                    save=True, project=str(ROOT / "runs" / "vn_verify"), name=f"{TAG}_{cat}", exist_ok=True)
    hits = 0
    print(f"\n== {cat} ({len(imgs)} ảnh) — kỳ vọng {'BÁO' if want else 'KHÔNG báo'}")
    for r in res:
        dets = [(m.names[int(b.cls)], float(b.conf)) for b in r.boxes]
        alert = any(c in ALERT and cf >= ALERT[c] for c, cf in dets)
        hits += alert
        mark = "✓" if alert == want else "✗"
        top = max(dets, key=lambda x: x[1]) if dets else None
        print(f"  {mark} {Path(r.path).name[:60]:60} {f'{top[0]} {top[1]:.2f}' if top else '—'}")
    rate = hits / len(imgs)
    ok = rate if want else 1 - rate
    summary.append((cat, len(imgs), f"{'detect' if want else 'sạch'} {ok:.0%}"))

print(f"\n=== Tổng kết ({TAG}, model={Path(MODEL).name}) ===")
for cat, n, s in summary:
    print(f"  {cat:11} n={n:<3} {s}")
