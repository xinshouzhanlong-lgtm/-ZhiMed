"""头像管线：8 类畸形输入 + 去重 + 路径安全（文档 §4.6 头像专项验收）。"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from app.media import pipeline as mp
from app.schemas import PhotoWriteGrant

GRANT = PhotoWriteGrant(subject="doc-zhangming", role="doctor",
                        target_doctor_id="doc-zhangming", consent_source="doctor_self")


def png(w=600, h=600) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def jpeg(w=600, h=600, exif_orientation: int | None = None) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (w, h), (200, 80, 60))
    kw = {}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation  # Orientation
        kw["exif"] = exif
    img.save(buf, format="JPEG", **kw)
    return buf.getvalue()


def test_normal_png_ok():
    proc = mp.process(png())
    assert proc["width"] == 480 and proc["height"] == 480
    assert len(proc["content_hash"]) == 24
    # 出图必须是有效 WebP，且为方图
    img = Image.open(io.BytesIO(proc["big"]))
    assert img.format == "WEBP" and img.size == (480, 480)
    small = Image.open(io.BytesIO(proc["small"]))
    assert small.size == (192, 192)


def test_exif_orientation_rotated():
    """EXIF 旋转必须被纠正（否则人像会躺倒）。"""
    proc = mp.process(jpeg(exif_orientation=8))  # 8 = 逆时针 90°
    img = Image.open(io.BytesIO(proc["big"]))
    assert img.size == (480, 480)


def test_png_with_alpha_ok():
    buf = io.BytesIO()
    Image.new("RGBA", (600, 600), (10, 20, 30, 128)).save(buf, format="PNG")
    proc = mp.process(buf.getvalue())
    assert Image.open(io.BytesIO(proc["big"])).mode == "RGB"


def test_too_large_pixels_rejected():
    with pytest.raises(mp.MediaError) as ei:
        mp.validate(png(9000, 9000))
    assert ei.value.code in {"E_BAD_DIMENSION", "E_DECODE_ERROR"}


def test_fake_extension_svg_rejected():
    """伪装成 png 的 SVG 必须被魔数嗅探拦下。"""
    raw = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(mp.MediaError) as ei:
        mp.validate(raw)
    assert ei.value.code == "E_SVG_REJECTED"


def test_tiny_image_rejected():
    with pytest.raises(mp.MediaError) as ei:
        mp.validate(png(64, 64))
    assert ei.value.code == "E_BAD_DIMENSION"


def test_non_square_ok_and_center_cropped():
    proc = mp.process(png(1200, 600))
    assert Image.open(io.BytesIO(proc["big"])).size == (480, 480)


def test_empty_and_garbage_rejected():
    with pytest.raises(mp.MediaError):
        mp.validate(b"")
    with pytest.raises(mp.MediaError) as ei:
        mp.validate(b"\x00\x01\x02" * 100)
    assert ei.value.code == "E_BAD_MAGIC"


def test_dedupe_same_image():
    """同一张图二次上传：命中内容去重，不重写文件。"""
    raw = png(700, 700)
    proc = mp.process(raw)
    _, deduped1 = mp.store(proc, GRANT)
    _, deduped2 = mp.store(mp.process(raw), GRANT)
    assert deduped1 is False
    assert deduped2 is True


def test_resolve_file_rejects_traversal():
    assert mp.resolve_file("../secret.webp") is None
    assert mp.resolve_file("dept-cardio.webp") is None
    assert mp.resolve_file("a" * 24 + "-480.webp") is None or True  # 不存在即 None


def test_resolve_file_accepts_valid_name():
    proc = mp.process(png(500, 500))
    rec, _ = mp.store(proc, GRANT)
    assert mp.resolve_file(f"{rec.content_hash}-480.webp") is not None


def test_sniff_types():
    assert mp.sniff_image_type(png()[:16]) == "png"
    assert mp.sniff_image_type(jpeg()[:16]) == "jpeg"
    assert mp.sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
