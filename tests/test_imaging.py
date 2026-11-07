import io

import pytest
from PIL import Image

from app import imaging
from tests.conftest import make_image


def test_rejects_below_2k():
    with pytest.raises(imaging.ImageRejected, match="abaixo do mínimo"):
        imaging.process_upload(make_image(1920, 1080))


def test_accepts_2k_landscape_and_portrait():
    a = imaging.process_upload(make_image(2560, 1440))
    assert (a.width, a.height, a.ratio_label) == (2560, 1440, "16x9")
    b = imaging.process_upload(make_image(1440, 2560, fmt="PNG"))
    assert b.ratio_label == "9x16" and b.mime == "image/png"


def test_rejects_non_image():
    with pytest.raises(imaging.ImageRejected):
        imaging.process_upload(b"not an image" * 100)


def test_strips_gps_exif():
    exif = Image.Exif()
    exif[0x8825] = {2: (1, 2, 3)}  # GPSInfo
    exif[0x010F] = "CameraCorp"
    raw = make_image(2560, 1440, exif=exif.tobytes())
    assert b"CameraCorp" in raw
    out = imaging.process_upload(raw)
    assert b"CameraCorp" not in out.data
    Image.open(io.BytesIO(out.data)).verify()


def test_colors_map_to_palette():
    out = imaging.process_upload(make_image(2560, 1440, color=(204, 0, 0)))
    assert out.colors and all(h in imaging.PALETTE for h, _ in out.colors)
    assert out.colors[0][0] in ("#cc0000", "#990000")
