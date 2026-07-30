"""Size guide generator (no AI)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.core.config import AppConfig
from src.core.models import PlatformConfig, ProductInfo, SizeInfo


class SizeGuideProcessor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    @staticmethod
    def cm_to_inch(cm: float | None) -> float | None:
        if cm is None:
            return None
        return round(cm / 2.54, 1)

    def create_size_guide(
        self,
        product: ProductInfo,
        platform_cfg: PlatformConfig,
        dest: Path,
    ) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        width, height = platform_cfg.width, platform_cfg.height
        img = Image.new("RGB", (width, height), color="white")
        draw = ImageDraw.Draw(img)

        try:
            title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 72)
            header_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
            body_font = ImageFont.truetype("DejaVuSans.ttf", 42)
        except Exception:
            title_font = ImageFont.load_default()
            header_font = ImageFont.load_default()
            body_font = ImageFont.load_default()

        title = f"{product.sku} Size Guide"
        draw.text((width // 2, 80), title, fill="black", font=title_font, anchor="mt")

        headers = ["Size", "Chest (cm)", "Chest (in)", "Length (cm)", "Length (in)"]
        col_widths = [180, 220, 220, 220, 220]
        start_x = (width - sum(col_widths)) // 2
        start_y = 220
        row_height = 90

        # Header row
        x = start_x
        for h, cw in zip(headers, col_widths):
            draw.text((x + cw // 2, start_y), h, fill="black", font=header_font, anchor="mt")
            x += cw

        y = start_y + row_height
        for size_name, size_info in product.sizes.items():
            x = start_x
            values = [
                size_name,
                self._fmt(size_info.chest_cm),
                self._fmt(self.cm_to_inch(size_info.chest_cm)),
                self._fmt(size_info.length_cm),
                self._fmt(self.cm_to_inch(size_info.length_cm)),
            ]
            for v, cw in zip(values, col_widths):
                draw.text((x + cw // 2, y), v, fill="black", font=body_font, anchor="mt")
                x += cw
            y += row_height

        img.save(dest, format="PNG")
        return dest

    @staticmethod
    def _fmt(value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.1f}" if value != int(value) else str(int(value))
