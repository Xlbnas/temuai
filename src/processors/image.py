"""Deterministic image processors (no AI cost)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from src.core.config import AppConfig
from src.core.models import PlatformConfig, ProductInfo
from src.utils.paths import sku_path


class DeterministicProcessor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def create_main_image(
        self,
        product: ProductInfo,
        platform_cfg: PlatformConfig,
        dest: Path,
    ) -> Path:
        """Create 01_main from original front image: resize, center, white background."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        front_key = product.images.get("front")
        if not front_key:
            raise ValueError("No front image defined in product.yaml")
        src_path = sku_path(product.sku) / front_key
        if not src_path.exists():
            raise FileNotFoundError(f"Front image not found: {src_path}")

        canvas_w, canvas_h = platform_cfg.width, platform_cfg.height
        canvas = Image.new("RGB", (canvas_w, canvas_h), color="white")

        with Image.open(src_path) as img:
            img = img.convert("RGB")
            src_w, src_h = img.size
            # Fit within 90% of canvas while maintaining aspect ratio
            max_w = int(canvas_w * 0.9)
            max_h = int(canvas_h * 0.9)
            scale = min(max_w / src_w, max_h / src_h, 1.0)
            new_w = int(src_w * scale)
            new_h = int(src_h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            x = (canvas_w - new_w) // 2
            y = (canvas_h - new_h) // 2
            canvas.paste(img, (x, y))

        canvas.save(dest, format="PNG")
        return dest
