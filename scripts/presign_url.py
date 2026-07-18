"""MinIO Presigned URL + Multipart Upload (đồng bộ quy ước chung của team).

Quy ước: mọi thao tác đọc/ghi MinIO từ bên ngoài (BE/FE/tool) đi qua PRESIGNED URL —
không expose bucket public, không phát creds. File lớn (>100MB, vướng limit Cloudflare)
dùng multipart: chia part 10MB, upload song song, MinIO ghép lại.

Creds đọc từ `.env` gốc dự án qua transport.config (KHÔNG hardcode secret ở đây).

Usage:
    python scripts/presign_url.py <object_path>                     # Presigned GET (download)
    python scripts/presign_url.py --put <object_path>               # Presigned PUT (upload)
    python scripts/presign_url.py --multipart <file> <object_path>  # Upload file lớn (SDK tự chia part)
    python scripts/presign_url.py --multipart-presign <object_path> <file_size>  # URLs cho FE tự upload
    python scripts/presign_url.py --list [prefix]                   # List objects
"""
from __future__ import annotations

import math
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from minio import Minio
from minio.error import S3Error

from transport import config

PRESIGN_EXPIRY = int(os.getenv("PRESIGN_EXPIRY", "3600"))  # giây
PART_SIZE = int(os.getenv("PART_SIZE", str(10 * 1024 * 1024)))  # 10MB/part


def get_client() -> Minio:
    return Minio(
        config.MINIO_ENDPOINT,
        access_key=config.MINIO_ACCESS_KEY,
        secret_key=config.MINIO_SECRET_KEY,
        secure=config.MINIO_SECURE,
    )


def presigned_get(object_name: str, expiry_s: int = PRESIGN_EXPIRY) -> str:
    """Presigned URL để GET (download) object."""
    return get_client().presigned_get_object(
        config.MINIO_BUCKET, object_name, expires=timedelta(seconds=expiry_s)
    )


def presigned_put(object_name: str, expiry_s: int = PRESIGN_EXPIRY) -> str:
    """Presigned URL để PUT (upload) — FE upload thẳng lên MinIO, không qua BE."""
    return get_client().presigned_put_object(
        config.MINIO_BUCKET, object_name, expires=timedelta(seconds=expiry_s)
    )


def list_objects(prefix: str = "") -> list[dict]:
    return [
        {"name": o.object_name, "size": o.size, "last_modified": o.last_modified}
        for o in get_client().list_objects(config.MINIO_BUCKET, prefix=prefix, recursive=True)
    ]


def multipart_upload(file_path: str, object_name: str, part_size: int = PART_SIZE) -> dict:
    """Upload file lớn — fput_object tự chia multipart + retry, mỗi part < limit Cloudflare."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File không tồn tại: {file_path}")
    size = os.path.getsize(file_path)
    print(f"Uploading: {file_path} ({size / (1024*1024):.1f} MB, part {part_size // (1024*1024)}MB)")
    res = get_client().fput_object(
        config.MINIO_BUCKET, object_name, file_path, part_size=part_size
    )
    print(f"✅ OK — {res.bucket_name}/{res.object_name} (etag={res.etag})")
    return {"success": True, "object_name": res.object_name, "etag": res.etag, "size": size}


def multipart_presigned_urls(
    object_name: str, file_size: int, part_size: int = PART_SIZE, expiry_s: int = PRESIGN_EXPIRY
) -> dict:
    """Khởi tạo multipart + trả presigned PUT URL cho TỪNG part — FE upload song song rồi
    gọi complete_multipart() với list ETag. (Dùng API private của SDK vì SDK chưa public.)
    """
    client = get_client()
    total_parts = math.ceil(file_size / part_size)
    upload_id = client._create_multipart_upload(config.MINIO_BUCKET, object_name, headers={})
    urls = [
        {
            "part_number": n,
            "url": client.get_presigned_url(
                "PUT",
                config.MINIO_BUCKET,
                object_name,
                expires=timedelta(seconds=expiry_s),
                extra_query_params={"partNumber": str(n), "uploadId": upload_id},
            ),
        }
        for n in range(1, total_parts + 1)
    ]
    return {
        "upload_id": upload_id,
        "object_name": object_name,
        "total_parts": total_parts,
        "part_size": part_size,
        "presigned_urls": urls,
    }


def complete_multipart(object_name: str, upload_id: str, parts: list[dict]) -> dict:
    """Hoàn tất sau khi FE upload xong: parts = [{"part_number": 1, "etag": "..."}, ...]."""
    get_client()._complete_multipart_upload(config.MINIO_BUCKET, object_name, upload_id, parts)
    return {"success": True, "object_name": object_name, "upload_id": upload_id}


def abort_multipart(object_name: str, upload_id: str) -> None:
    """Hủy multipart dở dang (cleanup khi FE bỏ cuộc — part rác vẫn chiếm chỗ nếu không hủy)."""
    get_client()._abort_multipart_upload(config.MINIO_BUCKET, object_name, upload_id)
    print(f"Aborted: {upload_id}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    try:
        cmd = sys.argv[1]
        if cmd == "--list":
            objs = list_objects(sys.argv[2] if len(sys.argv) > 2 else "")
            if not objs:
                print("Không có object nào.")
                return
            print(f"{'Object':<60} {'Size':<12} {'Last Modified'}")
            print("-" * 100)
            for o in objs:
                mb = o["size"] / (1024 * 1024)
                size_str = f"{o['size'] / 1024:.1f} KB" if mb < 1 else f"{mb:.1f} MB"
                print(f"{o['name']:<60} {size_str:<12} {o['last_modified']}")
        elif cmd == "--put":
            url = presigned_put(sys.argv[2])
            print(f"Presigned UPLOAD URL (hết hạn sau {PRESIGN_EXPIRY}s):\n{url}\n")
            print(f'Upload:  curl -X PUT -T ./file.ext "{url}"')
        elif cmd == "--multipart":
            if not multipart_upload(sys.argv[2], sys.argv[3])["success"]:
                sys.exit(1)
        elif cmd == "--multipart-presign":
            r = multipart_presigned_urls(sys.argv[2], int(sys.argv[3]))
            print(f"Upload ID: {r['upload_id']}")
            print(f"Object: {r['object_name']} — {r['total_parts']} parts x {r['part_size'] // (1024*1024)}MB")
            for p in r["presigned_urls"]:
                print(f"  Part {p['part_number']}: {p['url'][:80]}...")
            print("\nFE upload từng part:  fetch(url, {method: 'PUT', body: partBlob})")
            print("Xong thì gọi complete_multipart(object, upload_id, [{part_number, etag}, ...])")
        else:
            url = presigned_get(cmd)
            print(f"Presigned DOWNLOAD URL (hết hạn sau {PRESIGN_EXPIRY}s):\n{url}")
    except S3Error as e:
        print(f"MinIO Error: {e.message}")
        sys.exit(1)
    except IndexError:
        print("Thiếu tham số — xem usage:\n" + (__doc__ or ""))
        sys.exit(1)


if __name__ == "__main__":
    main()
