#!/usr/bin/env python3
"""Hợp nhất dataset cháy rừng về 1 bộ YOLO chuẩn: 0=smoke, 1=fire.

Nguồn:
  - D-Fire (Kaggle mirror sayedgamal99/smoke-fire-detection-yolo): 0=smoke,1=fire, có ảnh âm tính
  - WSDY (ahemateja19bec1025/wildfiresmokedatasetyolo): 0=smoke (mirror HPWREN 737 ảnh)

Việc làm:
  1. Validate nhãn: parse được, class id hợp lệ, bbox trong [0,1], w*h>0 (clamp nhẹ, loại bbox hỏng).
  2. Validate ảnh: mở được, không truncated.
  3. Dedup perceptual-hash (pHash, hamming<=4) trong toàn corpus; ưu tiên giữ val/test,
     loại bản trùng khỏi train (chống leakage), trong cùng split giữ bản đầu.
  4. Copy về data/wildfire/dataset/{train,val,test}/{images,labels}, prefix theo nguồn.
  5. In báo cáo + ghi data/wildfire/wildfire.yaml.

Chạy: .venv/bin/python scripts/prepare_wildfire_data.py
"""
import os, sys, shutil, json
from pathlib import Path
from multiprocessing import Pool
from PIL import Image
import imagehash

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "wildfire" / "dataset"
CACHE = Path.home() / ".cache" / "kagglehub" / "datasets"

DFIRE = CACHE / "sayedgamal99/smoke-fire-detection-yolo/versions/1/data"
WSDY = CACHE / "ahemateja19bec1025/wildfiresmokedatasetyolo/versions/2/WildFire_Smoke_Dataset_YOLO"

# (tag, src_images_dir, split_out, class_map {src_id: dst_id})
SOURCES = [
    ("dfire", DFIRE / "val" / "images",   "val",   {0: 0, 1: 1}),
    ("dfire", DFIRE / "test" / "images",  "test",  {0: 0, 1: 1}),
    ("wsdy",  WSDY / "valid" / "images",  "val",   {0: 0}),
    ("wsdy",  WSDY / "test" / "images",   "test",  {0: 0}),
    ("dfire", DFIRE / "train" / "images", "train", {0: 0, 1: 1}),
    ("wsdy",  WSDY / "train" / "images",  "train", {0: 0}),
]

HAM_THRESH = 4


def phash_one(path):
    try:
        with Image.open(path) as im:
            im.load()
            return str(path), imagehash.phash(im)
    except Exception:
        return str(path), None


def load_label(lbl_path, class_map):
    """Trả (lines_ok, n_dropped_boxes) hoặc (None, ...) nếu file nhãn hỏng."""
    if not lbl_path.exists():
        return None, 0
    ok, dropped = [], 0
    for raw in lbl_path.read_text().splitlines():
        parts = raw.split()
        if not parts:
            continue
        try:
            cid = int(float(parts[0]))
            x, y, w, h = (float(v) for v in parts[1:5])
        except (ValueError, IndexError):
            dropped += 1
            continue
        if cid not in class_map:
            dropped += 1
            continue
        # clamp nhẹ sai số float; loại bbox rỗng hoặc tràn nặng
        x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        w, h = min(max(w, 0.0), 1.0), min(max(h, 0.0), 1.0)
        if w * h <= 0 or w < 1e-4 or h < 1e-4:
            dropped += 1
            continue
        ok.append(f"{class_map[cid]} {x:.6f} {y:.6f} {w:.6f} {h:.6f}")
    return ok, dropped


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    for s in ("train", "val", "test"):
        (OUT / s / "images").mkdir(parents=True)
        (OUT / s / "labels").mkdir(parents=True)

    # gom danh sách ảnh theo thứ tự ưu tiên (val/test trước để dedup loại khỏi train)
    items = []  # (tag, img_path, split, class_map)
    for tag, img_dir, split, cmap in SOURCES:
        if not img_dir.exists():
            print(f"[WARN] thiếu nguồn: {img_dir}", file=sys.stderr)
            continue
        for p in sorted(img_dir.iterdir()):
            if p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                items.append((tag, p, split, cmap))
    print(f"Tổng ảnh nguồn: {len(items)}")

    with Pool(16) as pool:
        hashes = dict(pool.map(phash_one, [p for _, p, _, _ in items], chunksize=64))

    stats = {s: {"kept": 0, "dup": 0, "bad_img": 0, "no_label": 0, "boxes": [0, 0], "neg": 0, "dropped_boxes": 0}
             for s in ("train", "val", "test")}
    seen = []          # list[(hash, split)] — so hamming
    buckets = {}       # hash-hex-prefix -> indices, tăng tốc so gần đúng
    manifest = []

    def is_dup(h, split):
        for key in (str(h)[:6],):
            for idx in buckets.get(key, []):
                oh, osplit = seen[idx]
                if h - oh <= HAM_THRESH:
                    return True
        return False

    for tag, img, split, cmap in items:
        st = stats[split]
        h = hashes.get(str(img))
        if h is None:
            st["bad_img"] += 1
            continue
        if is_dup(h, split):
            st["dup"] += 1
            continue
        lbl = img.parent.parent / "labels" / (img.stem + ".txt")
        lines, dropped = load_label(lbl, cmap)
        if lines is None:
            st["no_label"] += 1
            continue
        st["dropped_boxes"] += dropped
        name = f"{tag}_{img.stem}"
        shutil.copy2(img, OUT / split / "images" / (name + img.suffix.lower()))
        (OUT / split / "labels" / (name + ".txt")).write_text("\n".join(lines) + ("\n" if lines else ""))
        idx = len(seen)
        seen.append((h, split))
        buckets.setdefault(str(h)[:6], []).append(idx)
        st["kept"] += 1
        if lines:
            for ln in lines:
                st["boxes"][int(ln.split()[0])] += 1
        else:
            st["neg"] += 1
        manifest.append({"name": name, "src": tag, "split": split})

    yaml_path = OUT.parent / "wildfire.yaml"
    yaml_path.write_text(
        f"path: {OUT}\ntrain: train/images\nval: val/images\ntest: test/images\n\nnc: 2\nnames:\n  0: smoke\n  1: fire\n"
    )
    (OUT.parent / "manifest.json").write_text(json.dumps(manifest))

    print(f"\n{'split':6} {'kept':>6} {'dup':>5} {'badimg':>6} {'nolbl':>5} {'neg':>6} {'smoke':>7} {'fire':>7} {'dropbox':>7}")
    for s in ("train", "val", "test"):
        t = stats[s]
        print(f"{s:6} {t['kept']:>6} {t['dup']:>5} {t['bad_img']:>6} {t['no_label']:>5} {t['neg']:>6} "
              f"{t['boxes'][0]:>7} {t['boxes'][1]:>7} {t['dropped_boxes']:>7}")
    print(f"\nĐã ghi {yaml_path}")


if __name__ == "__main__":
    main()
