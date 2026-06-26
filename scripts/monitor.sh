#!/usr/bin/env bash
# Dashboard nhanh: Redis streams/groups + throughput + GPU + worker. Không đụng MinIO (nhẹ).
# Dùng:  watch -n 2 scripts/monitor.sh        # tự refresh mỗi 2s
#        scripts/monitor.sh                    # 1 lần
cd "$(dirname "$0")/.."
.venv/bin/python - <<'EOF'
import time, redis
from transport import config
r = redis.from_url(config.REDIS_URL)
def xlen(s):
    try: return r.xlen(s)
    except Exception: return 0
print("===== REDIS =====")
print(f"  req(dms:captures:ready)={xlen(config.REQ_STREAM)}  "
      f"res(dms:ai:results)={xlen(config.RES_STREAM)}  dlq={xlen(config.DLQ_STREAM)}")
for s, g in ((config.REQ_STREAM, config.CG_AI), (config.RES_STREAM, config.CG_BE)):
    try:
        info = {gg['name'].decode(): gg for gg in r.xinfo_groups(s)}.get(g, {})
        ncons = info.get('consumers', '?')
        npend = info.get('pending', '?')
        print(f"  {g:6s} pending={npend!s:<5} consumers={ncons}")  # consumers = số worker/BE đang nối
    except Exception as e:
        print(f"  {g:6s} (n/a: {e})")
# throughput: đếm entry res trong 5s gần nhất (theo id = ms timestamp, không lệ thuộc MAXLEN)
win = 5
now_ms = int(time.time() * 1000)
recent = r.xrange(config.RES_STREAM, min=f"{now_ms - win*1000}-0")
ok = sum(1 for _, f in recent if f.get(b'status') == b'ok')
err = len(recent) - ok
print(f"  throughput({win}s): {len(recent)/win:5.1f} fps   (ok={ok} err={err})")
EOF
echo "===== GPU ====="
nvidia-smi --query-gpu=index,utilization.gpu,memory.used,temperature.gpu --format=csv,noheader | sed 's/^/  GPU /'
echo "  workers: $(ps -eo args | grep -c '[t]ransport.worker')"
