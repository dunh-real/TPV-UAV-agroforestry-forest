#!/usr/bin/env bash
# Đếm ảnh 3 tầng trên MinIO (events/uncertain/blackbox). List có thể chậm khi nhiều object
# → chạy on-demand hoặc watch chu kỳ DÀI: watch -n 30 scripts/mon_minio.sh
cd "$(dirname "$0")/.."
.venv/bin/python - <<'EOF'
from minio import Minio
from transport import config
c = Minio(config.MINIO_ENDPOINT, access_key=config.MINIO_ACCESS_KEY,
          secret_key=config.MINIO_SECRET_KEY, secure=config.MINIO_SECURE)
print("===== MinIO 3 tầng (bucket=%s) =====" % config.MINIO_BUCKET)
tot = 0
for pref in ('events/', 'uncertain/', 'blackbox/'):
    objs = list(c.list_objects(config.MINIO_BUCKET, prefix=pref, recursive=True))
    sz = sum(o.size for o in objs) / 1e6
    tot += len(objs)
    print(f"  {pref:11s} {len(objs):6d} object   {sz:8.1f} MB")
print(f"  {'TỔNG':11s} {tot:6d} object")
EOF
