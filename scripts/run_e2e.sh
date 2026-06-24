#!/usr/bin/env bash
# Chạy end-to-end smoke test: init streams → worker (YOLO26x) + BE chạy nền → producer.
# Mỗi tiến trình độc lập, nối Redis qua TCP (architecture §3).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
DRONE="${DRONE:-drone1}"
FPS="${FPS:-10}"
COUNT="${COUNT:-100}"

echo "== init streams =="
$PY -m transport.init_streams

echo "== start BE consumer (bg) =="
$PY -m transport.consumer & BE_PID=$!

echo "== start AI worker YOLO26x (bg) =="
$PY -m transport.worker & W_PID=$!

cleanup() { kill $BE_PID $W_PID 2>/dev/null || true; }
trap cleanup EXIT

sleep 6   # chờ model load + warmup

echo "== run producer: $DRONE @ ${FPS}fps x $COUNT =="
$PY -m transport.producer --src samples --drone "$DRONE" --fps "$FPS" --count "$COUNT"

sleep 2
echo "== done. stopping bg procs =="
