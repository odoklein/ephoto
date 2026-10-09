"""Decoding and encoding helpers shared by the photo and signature processors."""
from __future__ import annotations

import io
import warnings

import cv2
import numpy as np
from PIL import Image

JPEG = ".jpg", "image/jpeg"
PNG = ".png", "image/png"
WEBP = ".webp", "image/webp"

# Leading bytes of the formats the intake accepts, in the order they are tested.
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
)


def sniff_extension(data: bytes, fallback: str = ".jpg") -> str:
    """Extension from the bytes themselves.

    Make.com forwards whatever filename the shop recorded, which is routinely wrong; a
    file stored under the wrong extension would then be served with the wrong
    Content-Type to the reviewer's browser.
    """
    for signature, extension in MAGIC:
        if data.startswith(signature):
            return extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return fallback


class ImageRejected(ValueError):
    """Bytes that are not an image the pipelines may decode."""

    def __init__(self, message: str, too_large: bool = False) -> None:
        super().__init__(message)
        self.too_large = too_large


def inspect(data: bytes, max_pixels: int) -> tuple[int, int]:
    """Width and height read from the header, before a single pixel is allocated.

    Fifteen megabytes of JPEG or PNG can declare a 50 000 × 50 000 canvas; decoding it
    would take the container down with every dossier in it.  Pillow parses only the
    header here, so a decompression bomb is refused for the price of a few bytes.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
    except Image.DecompressionBombError as error:
        raise ImageRejected("Image trop grande (nombre de pixels)", too_large=True) from error
    except Exception as error:  # UnidentifiedImageError, truncated header, …
        raise ImageRejected("Image illisible ou format non supporté") from error
    if width <= 0 or height <= 0:
        raise ImageRejected("Image vide")
    if width * height > max_pixels:
        raise ImageRejected(
            f"Image trop grande : {width}×{height} px (maximum {max_pixels // 1_000_000} Mpx)", too_large=True,
        )
    return width, height


def decode(data: bytes) -> np.ndarray:
    """Bytes to BGR. Raises ValueError rather than returning None like OpenCV does."""
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Image illisible ou format non supporté")
    return image


def limit_size(image: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    """Downscale so the longer side is at most `max_side`; returns the image and the factor."""
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return image, 1.0
    factor = max_side / float(longest)
    resized = cv2.resize(image, (round(width * factor), round(height * factor)), interpolation=cv2.INTER_AREA)
    return resized, factor


def encode_png(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    if not ok:
        raise ValueError("Encodage PNG impossible")
    return buffer.tobytes()


def encode_jpeg(image: np.ndarray, max_bytes: int, minimum_quality: int = 70) -> tuple[bytes, int]:
    """Encode just under `max_bytes`, giving up quality only as far as it takes.

    Identity photographs are small, so the first quality nearly always fits; the loop
    exists so an unusually large source can never produce an oversized export.
    """
    for quality in range(95, minimum_quality - 1, -5):
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise ValueError("Encodage JPEG impossible")
        data = buffer.tobytes()
        if len(data) <= max_bytes:
            return data, quality
    return data, minimum_quality


def laplacian_sharpness(image: np.ndarray) -> float:
    """Variance of the Laplacian: the usual cheap stand-in for focus."""
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())
