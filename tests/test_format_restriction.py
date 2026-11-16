"""Only the allowed input formats may be decoded, whatever the file is called.

Regression test: Image.open() used to auto-detect any Pillow format, so a TIFF/ICO/PPM/TGA/SGI file named
x.jpg was decoded and accepted (unknown formats were relabelled PNG). That exposed Pillow's other decoders
to untrusted uploads."""
import io

import pytest
from PIL import Image

from app.errors import CorruptImageError
from app.image_processor import open_and_validate
from conftest import make_image_bytes


@pytest.mark.parametrize("fmt", ["TIFF", "ICO", "PPM", "TGA", "SGI", "PCX", "MSP"])
def test_disallowed_format_is_rejected_by_content(fmt):
    try:
        data = make_image_bytes(mode="1", fmt=fmt, color=1) if fmt == "MSP" else make_image_bytes(fmt=fmt)
    except (KeyError, ValueError, OSError):
        pytest.skip(f"this Pillow build cannot write {fmt}")
    with pytest.raises(CorruptImageError):
        open_and_validate(data)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "BMP", "GIF"])
def test_allowed_formats_still_open(fmt):
    img = open_and_validate(make_image_bytes(fmt=fmt))
    assert img.format == fmt


def test_mpo_phone_photo_still_opens():
    a, b = Image.new("RGB", (50, 40), (10, 200, 10)), Image.new("RGB", (50, 40), (200, 10, 10))
    buf = io.BytesIO()
    a.save(buf, "MPO", save_all=True, append_images=[b])
    assert open_and_validate(buf.getvalue()).size == (50, 40)


def test_upload_of_disguised_tiff_is_refused(client):
    data = make_image_bytes(fmt="TIFF")
    r = client.post("/api/upload", data={"file": (io.BytesIO(data), "photo.jpg")}, content_type="multipart/form-data")
    assert r.status_code in (400, 415, 422)
