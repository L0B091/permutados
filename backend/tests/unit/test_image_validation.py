import io

import pytest
from PIL import Image

from app.services.publications.image_validation import ImageRejected, sniff_mime, validate_image, validate_images
from tests.conftest import make_image


def reason(exc):
    return exc.value.details["reason"]


@pytest.mark.parametrize("fmt,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")])
def test_valid_formats(fmt, mime):
    v = validate_image(make_image(fmt))
    assert v.mime_type == mime and v.width == 800 and v.height == 600 and len(v.sha256) == 64


def test_exactly_one_image_required():
    with pytest.raises(ImageRejected) as e:
        validate_images([])
    assert reason(e) == "NO_IMAGE"
    with pytest.raises(ImageRejected) as e:
        validate_images([make_image(seed=1), make_image(seed=2)])
    assert reason(e) == "TOO_MANY_IMAGES"


def test_unsupported_format_gif_and_text():
    buf = io.BytesIO()
    Image.effect_noise((800, 600), 64).convert("P").save(buf, format="GIF")
    with pytest.raises(ImageRejected) as e:
        validate_image(buf.getvalue())
    assert reason(e) == "UNSUPPORTED_FORMAT"
    with pytest.raises(ImageRejected) as e:
        validate_image(b"<html>not an image</html>" * 200)
    assert reason(e) == "UNSUPPORTED_FORMAT"


def test_corrupt_truncated_jpeg():
    data = make_image()
    with pytest.raises(ImageRejected) as e:
        validate_image(data[: len(data) // 2])
    assert reason(e) == "CORRUPT"


def test_corrupt_garbage_with_valid_magic():
    with pytest.raises(ImageRejected) as e:
        validate_image(b"\x89PNG\r\n\x1a\n" + b"\x00garbage" * 1000)
    assert reason(e) == "CORRUPT"


def test_oversized_rejected_before_decoding():
    data = b"\xff\xd8\xff" + b"\x00" * (10 * 1024 * 1024 + 1)
    with pytest.raises(ImageRejected) as e:
        validate_image(data)
    assert reason(e) == "TOO_LARGE"


def test_low_resolution():
    with pytest.raises(ImageRejected) as e:
        validate_image(make_image(size=(200, 150)))
    assert reason(e) == "LOW_RESOLUTION"


def test_uniform_image_is_low_quality():
    with pytest.raises(ImageRejected) as e:
        validate_image(make_image(noise=False))
    assert reason(e) == "LOW_QUALITY"
    assert "no tiene suficiente calidad" in e.value.message


def test_real_mime_wins_over_extension():
    # A PNG "renamed" to .jpg is detected as PNG by its magic bytes.
    assert sniff_mime(make_image("PNG")) == "image/png"
