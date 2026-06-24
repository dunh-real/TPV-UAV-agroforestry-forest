#!/usr/bin/env bash
# Khởi động N worker YOLO26x (TensorRT engine), rải đều 2 GPU. Mặc định N=2 —
# cấu hình khuyến nghị cho ~4 drone (dự phòng + latency thấp, xem transport/README.md).
#
#   scripts/run_workers.sh        # 2 worker, mỗi card 1
#   N=4 scripts/run_workers.sh    # 4 worker (chỉ khi tải > ~100fps)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
N="${N:-2}"
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
