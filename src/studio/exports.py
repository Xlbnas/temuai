"""Deterministic, secret-free derived image export primitives for Studio."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from src.studio.models import DerivedExport, ExportTransformStep

EXPORT_MANIFEST_VERSION = "studio-derived-export-v1"
PIPELINE_VERSION = "fit-pad-rgb-jpeg-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_metadata(path: Path) -> dict[str, Any]:
    with Image.open(path) as opened:
        opened.verify()
    with Image.open(path) as image:
        return {
            "width": image.width,
            "height": image.height,
            "mime_type": Image.MIME.get(image.format or "", "application/octet-stream"),
            "image_format": image.format,
            "color_mode": image.mode,
        }


def fit_pad_recipe(source_width: int, source_height: int, target_width: int, target_height: int) -> list[ExportTransformStep]:
    scale = min(target_width / source_width, target_height / source_height)
    resized_width = round(source_width * scale)
    resized_height = round(source_height * scale)
    left = (target_width - resized_width) // 2
    top = (target_height - resized_height) // 2
    return [
        ExportTransformStep(
            order=1,
            operation="resize_fit",
            parameters={"width": resized_width, "height": resized_height, "resampling": "lanczos", "crop": "none", "stretch": "none"},
        ),
        ExportTransformStep(
            order=2,
            operation="pad_canvas",
            parameters={"width": target_width, "height": target_height, "background": "#ffffff", "left": left, "right": target_width - resized_width - left, "top": top, "bottom": target_height - resized_height - top},
        ),
        ExportTransformStep(order=3, operation="convert_color_mode", parameters={"mode": "RGB"}),
        ExportTransformStep(order=4, operation="encode", parameters={"format": "JPEG", "quality": 95}),
    ]


def render_fit_pad(source: Path, destination: Path, transforms: list[ExportTransformStep]) -> None:
    resize = transforms[0].parameters
    canvas = transforms[1].parameters
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    resized = image.resize((_int_parameter(resize, "width"), _int_parameter(resize, "height")), Image.Resampling.LANCZOS)
    rendered = Image.new("RGB", (_int_parameter(canvas, "width"), _int_parameter(canvas, "height")), "white")
    rendered.paste(resized, (_int_parameter(canvas, "left"), _int_parameter(canvas, "top")))
    buffer = BytesIO()
    rendered.save(buffer, format="JPEG", quality=_int_parameter(transforms[3].parameters, "quality"))
    atomic_write_bytes(destination, buffer.getvalue())


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def manifest_payload(export: DerivedExport, rejected_predecessors: list[dict[str, str | None]]) -> dict[str, Any]:
    """Only structured Studio facts are emitted; secrets and network details have no input path."""
    return {
        "schema_version": EXPORT_MANIFEST_VERSION,
        "export": export.model_dump(mode="json"),
        "rejected_predecessors": rejected_predecessors,
        "security": {
            "contains_api_key": False,
            "contains_authorization": False,
            "contains_cookie": False,
            "contains_provider_headers": False,
            "contains_internal_network": False,
        },
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _int_parameter(parameters: dict[str, object], name: str) -> int:
    value = parameters.get(name)
    if not isinstance(value, int):
        raise TypeError(f"Export transform parameter {name!r} must be an integer")
    return value
