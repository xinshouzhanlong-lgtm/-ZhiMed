"""头像处理管线（validate → process → store → 回读校验）。

为什么在服务端做（对应设计文档 §6.2）：
- Pillow 已是主栈依赖，边际成本为 0；
- 不依赖浏览器 OffscreenCanvas 支持（老 kiosk Chromium 可能没有）；
- 可防 decompression bomb（Image.MAX_IMAGE_PIXELS）。

必守细节：`Image.verify()` 之后**必须重新 open**，同一对象已不可再用。
"""
from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

from PIL import Image, ImageOps

from .. import state
from ..schemas import DoctorPhotoRecord, PhotoWriteGrant
from ..settings import settings

MAX_PIXELS = 40_000_000
ALLOWED = {"JPEG", "PNG", "WEBP"}
MIN_SIDE = 240


class MediaError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sniff_image_type(head: bytes) -> str:
    """按文件头魔数判真实类型。**不信任扩展名与 Content-Type**。"""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    low = head[:512].lower()
    if b"<svg" in low or (low.startswith(b"<?xml") and b"svg" in low):
        return "svg"
    return "unknown"


def validate(raw: bytes) -> dict:
    """格式/尺寸/体积三重校验，全部通过才返回报告。"""
    if not raw:
        raise MediaError("E_BAD_MAGIC", "空文件")
    max_bytes = int(settings.photo_cfg.get("max_bytes", 10 * 1024 * 1024))
    if len(raw) > max_bytes:
        raise MediaError("E_TOO_LARGE", f"原始文件超过 {max_bytes // 1024 // 1024}MB")

    kind = sniff_image_type(raw[:512])
    if kind == "svg":
        raise MediaError("E_SVG_REJECTED", "不接受 SVG（可内嵌脚本）")
    if kind == "unknown":
        raise MediaError("E_BAD_MAGIC", "无法识别的图片格式（仅支持 JPEG/PNG/WebP）")

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format
            w, h = probe.size
    except Image.DecompressionBombError:
        raise MediaError("E_BAD_DIMENSION", "图片像素过大") from None
    except Exception as exc:  # noqa: BLE001
        raise MediaError("E_DECODE_ERROR", f"图片解码失败：{type(exc).__name__}") from None

    if fmt not in ALLOWED:
        raise MediaError("E_BAD_MAGIC", f"不支持的格式 {fmt}")
    if min(w, h) < MIN_SIDE:
        raise MediaError("E_BAD_DIMENSION", f"图片短边需 ≥ {MIN_SIDE}px，当前 {w}×{h}")
    return {"type": kind, "format": fmt, "width": w, "height": h, "bytes": len(raw)}


def _encode(img: Image.Image, size: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=86, method=6)
    return buf.getvalue()


def process(raw: bytes) -> dict:
    """EXIF 纠正方向 → 1:1 居中裁切（人像中心偏上）→ 出 480 与 192 两档 WebP。"""
    validate(raw)
    size = int(settings.photo_cfg.get("size", 480))
    thumb = int(settings.photo_cfg.get("thumb", 192))

    # verify() 之后必须重新 open
    with Image.open(io.BytesIO(raw)) as probe:
        probe.verify()
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        big = ImageOps.fit(img, (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.35))
        small = ImageOps.fit(img, (thumb, thumb), method=Image.Resampling.LANCZOS, centering=(0.5, 0.35))
        big_bytes = _encode(big, size)
        small_bytes = _encode(small, thumb)

    return {
        "big": big_bytes,
        "small": small_bytes,
        "content_hash": hashlib.sha256(big_bytes).hexdigest()[:24],
        "width": size,
        "height": size,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _target_dir(content_hash: str) -> Path:
    d = settings.media_dir / content_hash[:2]
    d.mkdir(parents=True, exist_ok=True)
    return d


_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows 必须显式二进制，否则 \n 被转成 \r\n


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC | _O_BINARY)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def store(proc: dict, grant: PhotoWriteGrant) -> tuple[DoctorPhotoRecord, bool]:
    """落盘 + 登记 + 回读校验。返回 (记录, 是否命中去重)。"""
    ch = proc["content_hash"]
    d = _target_dir(ch)
    big_path = d / f"{ch}-480.webp"
    small_path = d / f"{ch}-192.webp"

    deduped = big_path.is_file() and small_path.is_file()
    if not deduped:
        _atomic_write(big_path, proc["big"])
        _atomic_write(small_path, proc["small"])

    # 回读校验：不信任写入返回值
    for p, expect in ((big_path, proc["big"]), (small_path, proc["small"])):
        if hashlib.sha256(p.read_bytes()).digest() != hashlib.sha256(expect).digest():
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            raise MediaError("E_VERIFY_FAILED", "落盘校验失败，已清理")

    rec = DoctorPhotoRecord(
        doctor_id=grant.target_doctor_id,
        content_hash=ch,
        ext="webp",
        width=proc["width"],
        height=proc["height"],
        bytes=len(proc["big"]),
        source_sha256=proc.get("source_sha256", ""),
        uploaded_by=grant.subject,
        uploader_role=grant.role,
        consent_source=grant.consent_source,
        uploaded_at=state.now_ms(),
        status="active",
    )
    try:
        state.upsert_photo(rec.model_dump())
    except Exception:
        # DB 写失败 → 清理孤儿文件（best-effort）
        if not deduped:
            for p in (big_path, small_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
        raise
    return rec, deduped


def ref_for(record: dict) -> dict:
    ch = record["content_hash"]
    return {
        "url_480": f"/api/media/doctors/{ch}-480.webp",
        "url_192": f"/api/media/doctors/{ch}-192.webp",
        "width": record["width"],
        "height": record["height"],
        "bytes_480": record["bytes"],
        "updated_at": record["uploaded_at"],
    }


def resolve_file(filename: str) -> Path | None:
    """只接受 `{24位hex}-{480|192}.webp`，杜绝路径穿越与目录遍历。"""
    import re

    if not re.fullmatch(r"[0-9a-f]{24}-(480|192)\.webp", filename):
        return None
    path = settings.media_dir / filename[:2] / filename
    return path if path.is_file() else None


def photo_ref_for(doctor_id: str) -> dict | None:
    rec = state.active_photo(doctor_id)
    return ref_for(rec) if rec else None


def remove(doctor_id: str, actor: str) -> None:
    state.suspend_photo(doctor_id)
    state.execute(
        "INSERT INTO photo_audit(doctor_id, action, content_hash, actor, actor_role, consent_source,"
        " ip_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (doctor_id, "remove", "", actor, "", "", "", state.now_ms()),
    )


def ensure_dirs() -> None:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("tmp",):
        (settings.media_dir / sub).mkdir(parents=True, exist_ok=True)
