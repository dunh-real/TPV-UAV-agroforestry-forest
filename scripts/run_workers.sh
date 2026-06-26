#!/usr/bin/env bash
# Khởi động N worker YOLO26x (TensorRT engine), rải đều 2 GPU (mỗi card N/2 worker). Mặc định
# N=4 — trần ~282fps, dự phòng cao; GPU thừa thãi (mỗi worker ~1.2GB / 96GB). Xem transport/README.md.
#
#   scripts/run_workers.sh        # 4 worker (2 mỗi card)
#   N=2 scripts/run_workers.sh    # 2 worker (đủ cho ~4 drone, tiết kiệm hơn)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
N="${N:-4}"
MODEL="${MODEL_PATH:-weights/yolo26x.engine}"

$PY -m transport.init_streams
echo "khởi động $N worker (model=$MODEL) ..."
pids=()
for k in $(seq 1 "$N"); do
  MODEL_PATH="$MODEL" READ_COUNT="${READ_COUNT:-1}" WORKER_NAME="eng-w$k" DEVICE=$(( (k-1) % 2 )) \
    $PY -u -m transport.worker &
  pids+=($!)
  echo "  worker eng-w$k → GPU $(( (k-1) % 2 ))  (pid ${pids[-1]})"
done
trap 'echo; echo "dừng worker..."; kill "${pids[@]}" 2>/dev/null || true' INT TERM
wait
