"""Image validation and conversion utilities."""
from __future__ import annotations

import base64
import binascii
import io
import mimetypes
from pathlib import Path
from typing import Any

from PIL import Image

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def is_valid_image(path: Path) -> bool:
    """Verify file exists, non-empty, and can be opened by Pillow."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def get_image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def detect_image_format_from_bytes(data: bytes) -> str:
    """Detect image format (png/jpeg/webp) from byte header."""
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpeg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:12]:
        return "webp"
    raise ValueError("Unsupported or unknown image format")


def save_base64_image(b64_data: str, dest: Path) -> Path:
    """Decode base64 image and save to dest. Auto-detect format."""
    try:
        raw = base64.b64decode(b64_data, validate=True)
    except binascii.Error as e:
        raise ValueError(f"Invalid base64 image data: {e}")
    fmt = detect_image_format_from_bytes(raw)
    dest = dest.with_suffix(f".{fmt}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(raw)
    return dest


def download_image(url: str, client: Any, dest: Path) -> Path:
    """Download image from URL and save to dest."""
    response = client.get(url, timeout=120, follow_redirects=True)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise ValueError(f"URL did not return image content-type: {content_type}")
    fmt = mimetypes.guess_extension(content_type) or ".png"
    if fmt == ".jpe":
        fmt = ".jpg"
    dest = dest.with_suffix(fmt)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(response.content)
    return dest


def validate_saved_image(path: Path) -> bool:
    """Validate a saved image file thoroughly."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with Image.open(path) as img:
            width, height = img.size
            if width <= 0 or height <= 0:
                return False
            img.load()
    except Exception:
        return False
    return True


def create_placeholder_image(dest: Path, width: int, height: int, label: str) -> Path:
    """Create a placeholder image for dry-runs / mocks."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=(240, 240, 240))
    try:
        from PIL import ImageDraw, ImageFont

        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 32)
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        draw.text(
            ((width - text_w) // 2, (height - text_h) // 2),
            label,
            fill=(80, 80, 80),
            font=font,
        )
    except Exception:
        pass
    img.save(dest, format="PNG")
    return dest
