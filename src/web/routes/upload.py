"""New product / SKU upload route."""
from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from PIL import Image as PILImage
from pydantic import BaseModel

from src.core.config import AppConfig
from src.utils.paths import safe_filename
from src.web.auth import get_current_username

router = APIRouter()

VALID_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _config(request: Request) -> AppConfig:
    return request.app.state.config


def validate_sku_name(sku: str) -> str:
    sku = sku.strip()
    if not sku:
        raise HTTPException(status_code=400, detail="SKU cannot be empty")
    if len(sku) > 128:
        raise HTTPException(status_code=400, detail="SKU too long")
    if re.search(r"[\/\\\x00\x1f\x7f]", sku) or ".." in sku:
        raise HTTPException(status_code=400, detail="SKU contains invalid characters")
    return safe_filename(sku)


def validate_image(file: UploadFile, max_bytes: int) -> tuple[bytes, str]:
    if file.content_type not in VALID_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid image type: {file.content_type}")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VALID_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Invalid image extension: {ext}")
    data = file.file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=400, detail="Image exceeds maximum upload size")
    try:
        with PILImage.open(BytesIO(data)) as img:
            img.verify()
    except Exception:
        raise HTTPException(status_code=400, detail="File is not a valid image")
    return data, ext


@router.post("/new-product")
async def new_product(
    request: Request,
    sku: str = Form(...),
    category: str = Form(...),
    color: str = Form(...),
    fabric_name: str = Form(...),
    front: UploadFile = File(None),
    back: UploadFile = File(None),
    pocket: UploadFile = File(None),
    fabric: UploadFile = File(None),
    username: str = Depends(get_current_username),
) -> dict:
    config = _config(request)
    max_mb = int(config.safe_env("MAX_UPLOAD_MB", "30") or "30")
    max_bytes = max_mb * 1024 * 1024

    sku_name = validate_sku_name(sku)
    target_dir = config.input_dir / sku_name
    originals_dir = target_dir / "originals"
    originals_dir.mkdir(parents=True, exist_ok=True)

    if (target_dir / "product.yaml").exists():
        raise HTTPException(status_code=409, detail="SKU already exists")

    uploaded_images: dict[str, str] = {}
    image_fields = {
        "front": front,
        "back": back,
        "pocket": pocket,
        "fabric": fabric,
    }
    for key, upload in image_fields.items():
        if upload and upload.filename:
            data, ext = validate_image(upload, max_bytes)
            dest = originals_dir / f"{key}{ext}"
            dest.write_bytes(data)
            uploaded_images[key] = f"originals/{key}{ext}"

    product_yaml = (
        f"sku: {sku_name}\n"
        f"product:\n"
        f"  category: {category}\n"
        f"  color: {color}\n"
        f"fabric:\n"
        f"  name: {fabric_name}\n"
        f"features: []\n"
        f"images:\n"
    )
    for key, rel_path in uploaded_images.items():
        product_yaml += f"  {key}: {rel_path}\n"
    product_yaml += "sizes: {}\n"

    (target_dir / "product.yaml").write_text(product_yaml, encoding="utf-8")
    return {"success": True, "sku": sku_name, "images": uploaded_images}
