from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import AppConfig
from src.core.models import PlatformConfig, ProductInfo
from src.processors.size_guide import SizeGuideProcessor


def test_cm_to_inch() -> None:
    assert SizeGuideProcessor.cm_to_inch(100) == 39.4
    assert SizeGuideProcessor.cm_to_inch(None) is None
    assert SizeGuideProcessor.cm_to_inch(112) == 44.1


def test_create_size_guide(temp_config: AppConfig, tmp_path: Path) -> None:
    product = ProductInfo(
        sku="TEST-SKU",
        sizes={
            "M": {"chest_cm": 116, "length_cm": 74},
            "L": {"chest_cm": 120, "length_cm": 76},
        },
    )
    platform_cfg = PlatformConfig(
        platform="temu",
        ratio="3:4",
        width=1500,
        height=2000,
    )
    processor = SizeGuideProcessor(temp_config)
    dest = tmp_path / "size_guide.png"
    result = processor.create_size_guide(product, platform_cfg, dest)
    assert result.exists()
    assert result.stat().st_size > 0
    from PIL import Image

    with Image.open(result) as img:
        assert img.size == (1500, 2000)
