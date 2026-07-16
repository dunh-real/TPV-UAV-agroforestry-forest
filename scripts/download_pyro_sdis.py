#!/usr/bin/env python3
"""Tải Pyro-SDIS (pyronear/pyro-sdis, Apache-2.0) từ Hugging Face và xuất ra layout YOLO.

Nguồn: ~33.6k ảnh camera tháp canh Pháp (SDIS/Force06), 1 lớp smoke, nhãn YOLO
trong cột `annotations`. Xuất ra data/wildfire/raw/pyro_sdis/{train,val}/{images,labels}
để prepare_wildfire_data.py hợp nhất (class 0=smoke giữ nguyên id).

Chạy: .venv/bin/python scripts/download_pyro_sdis.py
"""
from pathlib import Path
from datasets import load_dataset

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "wildfire" / "raw" / "pyro_sdis"

ds = load_dataset("pyronear/pyro-sdis")
for split in ds:
    img_dir = OUT / split / "images"
    lbl_dir = OUT / split / "labels"
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for ex in ds[split]:
        name = Path(ex["image_name"]).stem
        ex["image"].convert("RGB").save(img_dir / f"{name}.jpg", quality=95)
        ann = ex.get("annotations") or ""
        (lbl_dir / f"{name}.txt").write_text(ann.strip() + ("\n" if ann.strip() else ""))
        n += 1
    print(f"{split}: {n} ảnh -> {img_dir}")
