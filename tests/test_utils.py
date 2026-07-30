from __future__ import annotations

from pathlib import Path

from src.utils.paths import safe_filename
from src.utils.secrets import mask_message, mask_string
from src.utils.image import detect_image_format_from_bytes


def test_safe_filename() -> None:
    assert safe_filename("hello world") == "hello_world"
    assert safe_filename("../etc/passwd") == "etcpasswd"
    assert safe_filename("F116-Black") == "F116-Black"
    assert safe_filename("a  b___c") == "a_b_c"


def test_mask_string() -> None:
    assert mask_string("secret1234", 4) == "******1234"
    assert mask_string("short", 4) == "*hort"
    assert mask_string("abc", 4) == "***"
    assert mask_string(None) == ""


def test_mask_message() -> None:
    msg = "api_key=sk-1234567890abcdef password_hash=abc123"
    masked = mask_message(msg)
    assert "sk-1234567890abcdef" not in masked
    assert "sk-1234" not in masked
    assert "abc" not in masked


def test_detect_image_format() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    webp = b"RIFF\x10\x00\x00\x00WEBP" + b"\x00" * 20
    assert detect_image_format_from_bytes(png) == "png"
    assert detect_image_format_from_bytes(jpeg) == "jpeg"
    assert detect_image_format_from_bytes(webp) == "webp"
