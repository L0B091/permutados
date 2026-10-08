"""Strict validation for the single publication photo (section 12).

Checks: count, size, real MIME (magic bytes, not the declared type), decodability/corruption,
resolution, decompression bombs and a minimal quality heuristic (contrast / sharpness).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageFile, ImageFilter, ImageStat, UnidentifiedImageError

from app.core.config import get_settings
from app.core.errors import ValidationFailed
from app.services.notifications import templates

ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = 40_000_000  # PIL raises DecompressionBombError above 2x this

ALLOWED = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

MIN_CONTRAST_STDDEV = 8.0  # near-uniform images (blank, black, white) fail
MIN_EDGE_MEAN = 1.5  # extremely blurry images fail


class ImageRejected(ValidationFailed):
    code = "IMAGE_REJECTED"


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    mime_type: str
    extension: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    quality_score: float


def sniff_mime(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _reject(reason: str, message: str = templates.IMAGE_REJECTED) -> ImageRejected:
    return ImageRejected(message, code="IMAGE_REJECTED", details={"reason": reason})


def validate_images(images: list[bytes]) -> ValidatedImage:
    """Validate the list of images received for one publication. MVP: exactly one."""
    settings = get_settings()
    if len(images) == 0:
        raise _reject("NO_IMAGE", templates.PHOTO_REQUEST)
    if len(images) > settings.max_images_per_item:
        raise _reject("TOO_MANY_IMAGES", templates.ONLY_ONE_IMAGE)
    return validate_image(images[0])


def validate_image(data: bytes) -> ValidatedImage:
    s = get_settings()
    size = len(data)
    if size > s.max_image_bytes:
        raise _reject("TOO_LARGE")
    if size < s.min_image_bytes:
        raise _reject("TOO_SMALL")

    mime = sniff_mime(data)
    if mime not in ALLOWED:
        raise _reject("UNSUPPORTED_FORMAT")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            probe.verify()  # structural check
        with Image.open(io.BytesIO(data)) as img:
            img.load()  # full decode: catches truncated / corrupt pixel data
            width, height = img.size
            gray = img.convert("L")
            gray.thumbnail((512, 512))
            contrast = ImageStat.Stat(gray).stddev[0]
            edges = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0]
    except Image.DecompressionBombError as e:
        raise _reject("DECOMPRESSION_BOMB") from e
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as e:
        raise _reject("CORRUPT") from e

    if fmt != ALLOWED[mime]:
        raise _reject("MIME_MISMATCH")
    if min(width, height) < s.min_image_side_px:
        raise _reject("LOW_RESOLUTION")
    if max(width, height) > s.max_image_side_px:
        raise _reject("RESOLUTION_TOO_HIGH")
    if contrast < MIN_CONTRAST_STDDEV or edges < MIN_EDGE_MEAN:
        raise _reject("LOW_QUALITY")

    quality = round(min(100.0, contrast * 1.2 + edges * 4.0), 2)
    return ValidatedImage(
        data=data,
        mime_type=mime,
        extension=EXTENSIONS[mime],
        width=width,
        height=height,
        size_bytes=size,
        sha256=hashlib.sha256(data).hexdigest(),
        quality_score=quality,
    )
