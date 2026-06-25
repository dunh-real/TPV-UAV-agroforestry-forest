"""Đóng/mở gói message envelope.

Redis stream entry là field–value, value binary-safe ⇒ nhét thẳng bytes ảnh,
KHÔNG base64. `meta`/`result` gói msgpack (gọn hơn JSON ~20-30%, giữ kiểu số).
Tham chiếu: communication-transport-spec.md §3; architecture §8 (contract).
"""
from __future__ import annotations

from typing import Any

import msgpack


def pack(obj: Any) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def unpack(blob: bytes) -> Any:
    return msgpack.unpackb(blob, raw=False)


def build_req(
    frame_id: str,
    drone_id: str,
    ts: float,
    img_bytes: bytes,
    gps: dict | None = None,
    attitude: dict | None = None,
    bands: str = "RGB",
) -> dict[bytes, bytes]:
    """Tạo dict field→value cho XADD frames:req (spec §3.1).

    `attitude` nullable: tier telemetry thấp gửi None (architecture §7).
    """
    meta = {"gps": gps, "attitude": attitude, "bands": bands}
    return {
        b"frame_id": frame_id.encode(),
        b"drone_id": drone_id.encode(),
        b"ts": repr(ts).encode(),
        b"meta": pack(meta),
        b"img": img_bytes,
    }


def parse_req(fields: dict[bytes, bytes]) -> dict[str, Any]:
    return {
        "frame_id": fields[b"frame_id"].decode(),
        "drone_id": fields[b"drone_id"].decode(),
        "ts": float(fields[b"ts"]),
        "meta": unpack(fields[b"meta"]),
        "img": fields[b"img"],
    }


def build_res(
    capture_id: str,
    result: Any,
    evidence_key: str = "",
    status: str = "ok",
    err: str = "",
    drone_id: str = "",
) -> dict[bytes, bytes]:
    """Tạo dict field→value cho XADD frames:res (dms:ai:results) — schema theo BE.

    `result` (msgpack): hasDetection, detections[{type,confidence,bbox}], model, modelVersion.
    `evidence_key` = key ảnh bằng chứng trên MinIO (rỗng nếu không lưu). KHÔNG nhét ảnh nặng.
    BE gom nhóm → AlertEvent (dedup/severity là việc của BE, không phải worker).
    """
    return {
        b"captureId": capture_id.encode(),
        b"droneId": drone_id.encode(),
        b"status": status.encode(),
        b"result": pack(result),
        b"evidenceKey": evidence_key.encode(),
        b"err": err.encode(),
    }


def parse_res(fields: dict[bytes, bytes]) -> dict[str, Any]:
    return {
        "captureId": fields[b"captureId"].decode(),
        "droneId": fields.get(b"droneId", b"").decode(),
        "status": fields[b"status"].decode(),
        "result": unpack(fields[b"result"]),
        "evidenceKey": fields[b"evidenceKey"].decode(),
        "err": fields[b"err"].decode(),
    }
